"""Контакты телефона: сопоставление ФИО, синхронизация, проверка спорных, Google, S1."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from gateway.contacts import (
    DEBTOR,
    OTHER,
    REVIEW,
    RawContact,
    export_review,
    import_debtors_csv,
    import_review,
    protection_reason,
    sync_contacts,
)
from gateway.db import utcnow
from gateway.db.models import Debtor, DebtorPhone, PhoneContact
from gateway.google_contacts import (
    GoogleAuthError,
    exchange_code,
    fetch_contacts,
    make_auth_request,
    parse_redirect,
    person_to_contact,
    save_refresh_token,
)
from gateway.importers import add_whitelist
from gateway.names import Match, match

from .test_core import CASE, PHONE, Env


# --- сопоставление ФИО ---

@pytest.mark.parametrize(
    ("contact", "debtor", "expected"),
    [
        ("Иванов Иван Иванович", "ИВАНОВ Иван Иванович", Match.EXACT),
        ("Иван Иванович Иванов", "Иванов Иван Иванович", Match.EXACT),  # порядок не важен
        ("Семёнов Пётр Ильич", "Семенов Петр Ильич", Match.EXACT),  # ё = е
        ("Иванов Иван", "Иванов Иван Иванович", Match.PARTIAL),
        ("Иванов И.И.", "Иванов Иван Иванович", Match.PARTIAL),
        ("Иванов Иван Иванович банкрот", "Иванов Иван Иванович", Match.PARTIAL),
        ("Иванов Иван", "Иванов Иван", Match.PARTIAL),  # у должника без отчества — только проверка
        ("Иванов", "Иванов Иван Иванович", Match.NONE),  # одна фамилия — не совпадение
        ("Иван сантехник", "Иванов Иван Иванович", Match.NONE),
        ("Иванов П.И.", "Иванов Иван Иванович", Match.NONE),  # инициалы не те
        ("Петров Пётр Петрович", "Иванов Иван Иванович", Match.NONE),
        ("", "Иванов Иван Иванович", Match.NONE),
    ],
)
def test_match(contact, debtor, expected) -> None:
    assert match(contact, debtor) is expected


# --- синхронизация ---

async def _debtors(db, *rows) -> None:
    async with db.session() as s, s.begin():
        for case_id, name in rows:
            s.add(Debtor(case_id=case_id, debtor_name=name, source="test", updated_at=utcnow()))


def rc(res: str, name: str, *phones: str) -> RawContact:
    return RawContact(res, (name,), phones)


async def _statuses(db) -> dict[str, tuple[str, str | None, list | None]]:
    async with db.session() as s:
        rows = (await s.execute(select(PhoneContact))).scalars().all()
    return {r.phone: (r.status, r.display_name, r.case_ids) for r in rows}


async def test_sync_classifies_and_minimizes_pii(db) -> None:
    await _debtors(db, ("C1", "Иванов Иван Иванович"), ("C2", "Петров Пётр Петрович"), ("C3", "Петров Пётр Петрович"))
    report = await sync_contacts(db, [
        rc("people/1", "Иванов Иван Иванович", "+7 916 111-11-11"),
        rc("people/2", "Петров Пётр Петрович", "89162222222"),  # тёзки-должники → проверка
        rc("people/3", "Жена", "+79163333333"),
        rc("people/4", "Иванов И.И.", "+79164444444"),
        rc("people/5", "Без телефона"),
        rc("people/6", "Кривой", "12345"),
    ])
    st = await _statuses(db)
    assert st["79161111111"] == (DEBTOR, "Иванов Иван Иванович", ["C1"])
    assert st["79162222222"][0] == REVIEW and st["79162222222"][2] == ["C2", "C3"]
    assert st["79163333333"] == (OTHER, None, None)  # имя «других» не храним
    assert st["79164444444"][0] == REVIEW
    assert report.bad_phones == 1 and report.new_debtor_phones == 1 and report.new_review == 2
    async with db.session() as s:
        dp = (await s.execute(select(DebtorPhone.case_id, DebtorPhone.phone, DebtorPhone.source))).all()
    assert dp == [("C1", "79161111111", "google_contacts")]


async def test_phone_already_in_registry_is_debtor(db) -> None:
    async with db.session() as s, s.begin():
        s.add(DebtorPhone(case_id="C9", phone="79165555555", source="csv", created_at=utcnow()))
    await sync_contacts(db, [rc("people/1", "Вася работа", "+79165555555")])
    assert (await _statuses(db))["79165555555"][0] == DEBTOR


async def test_manager_decision_survives_resync_and_removal(db, tmp_path: Path) -> None:
    await _debtors(db, ("C1", "Иванов Иван Иванович"))
    contacts = [rc("people/1", "Иванов Иван", "+79161111111"), rc("people/2", "Иванов И.И.", "+79162222222")]
    await sync_contacts(db, contacts)
    f = tmp_path / "review.csv"
    assert await export_review(db, f) == 2
    text = f.read_text(encoding="utf-8-sig")
    assert "C1: Иванов Иван Иванович" in text and "+79161111111" in text
    lines = text.splitlines()
    out = [lines[0]]
    for line in lines[1:]:
        cols = line.split(";")
        cols[5] = "должник" if "79161111111" in line else "не должник"
        out.append(";".join(cols))
    f.write_text("\n".join(out), encoding="utf-8-sig")
    report = await import_review(db, f)
    assert report.added == 2 and not report.errors
    st = await _statuses(db)
    assert st["79161111111"][0] == DEBTOR and st["79162222222"] == (OTHER, None, None)
    # повторная синхронизация решения не меняет
    r2 = await sync_contacts(db, contacts)
    assert r2.kept_manager_decisions == 2
    assert (await _statuses(db))["79161111111"][0] == DEBTOR
    # контакт удалили из телефона — запись уходит
    r3 = await sync_contacts(db, contacts[:1])
    assert r3.removed == 1 and "79162222222" not in await _statuses(db)


async def test_review_import_requires_case_for_several_candidates(db, tmp_path: Path) -> None:
    await _debtors(db, ("C1", "Петров Пётр Петрович"), ("C2", "Петров Пётр Петрович"))
    await sync_contacts(db, [rc("people/1", "Петров Пётр Петрович", "+79161111111")])
    f = tmp_path / "r.csv"
    await export_review(db, f)
    header, row = f.read_text(encoding="utf-8-sig").splitlines()[:2]
    cols = row.split(";")
    cols[5] = "должник"
    f.write_text(header + "\n" + ";".join(cols), encoding="utf-8-sig")
    assert (await import_review(db, f)).errors  # case_id не указан
    cols[6] = "C2"
    f.write_text(header + "\n" + ";".join(cols), encoding="utf-8-sig")
    assert not (await import_review(db, f)).errors
    assert (await _statuses(db))["79161111111"][2] == ["C2"]


async def test_import_debtors_csv(db, tmp_path: Path) -> None:
    f = tmp_path / "d.csv"
    f.write_text("case_id;case_number;debtor_name\n1;А40-1/2025;Иванов Иван Иванович\n2;;\n", encoding="utf-8")
    r = await import_debtors_csv(db, f)
    assert (r.added, len(r.errors)) == (1, 1)
    f.write_text("case_id;debtor_name\n1;Иванов Иван Петрович\n", encoding="utf-8")
    assert (await import_debtors_csv(db, f)).duplicates == 1
    async with db.session() as s:
        d = await s.get(Debtor, "1")
    assert d.debtor_name == "Иванов Иван Петрович" and d.case_number == "А40-1/2025"


# --- защита (правило 3 в новой редакции) ---

async def test_protection_order(db) -> None:
    await _debtors(db, ("C1", "Иванов Иван Иванович"))
    await sync_contacts(db, [
        rc("people/1", "Иванов Иван Иванович", "+79161111111"),
        rc("people/2", "Жена", "+79162222222"),
        rc("people/3", "Иванов И.И.", "+79163333333"),
    ])
    # MAX-адресная книга содержит всех троих
    for p in ("79161111111", "79162222222", "79163333333", "79164444444"):
        await add_whitelist(db, phone=p, source="address_book")
    assert await protection_reason(db, phone="79161111111", max_user_id=None) is None  # должник из контактов
    assert "не должник" in await protection_reason(db, phone="79162222222", max_user_id=None)
    assert "проверке" in await protection_reason(db, phone="79163333333", max_user_id=None)
    assert "MAX" in await protection_reason(db, phone="79164444444", max_user_id=None)
    assert await protection_reason(db, phone="79165555555", max_user_id=None) is None
    # ручной белый список сильнее всего
    await add_whitelist(db, max_user_id=777, source="manual")
    assert await protection_reason(db, phone="79165555555", max_user_id=777) == "ручной белый список"
    # ручное добавление номера из адресной книги MAX повышает запись до «manual»
    assert await add_whitelist(db, phone="79161111111", source="manual", note="не трогать") is True
    assert await protection_reason(db, phone="79161111111", max_user_id=None) == "ручной белый список"


# --- S1 с контактами ---

async def test_s1_debtor_from_contacts_gets_redirect(db, tmp_path: Path) -> None:
    e = Env(db, tmp_path, bot_username="test_bot", dry_run=False,
            redirect_first_template="Бот: {link}", redirect_reminder_template="Бот: {link}")
    await _debtors(db, (CASE, "Сидоров Сидор Сидорович"))
    await add_whitelist(db, phone=PHONE, source="address_book")  # был в адресной книге MAX
    await sync_contacts(db, [rc("people/1", "Сидоров Сидор Сидорович", "+" + PHONE)])
    d = await e.msg()
    assert d.action == "redirect_first" and d.case_ids == (CASE,)
    assert len(e.transport.sent) == 1


@pytest.mark.parametrize("name", ["Жена", "Сидоров Сидор"])  # другой / спорный
async def test_s1_other_or_review_contact_no_reply(db, tmp_path: Path, name) -> None:
    e = Env(db, tmp_path, bot_username="test_bot", dry_run=False,
            redirect_first_template="Бот: {link}", redirect_reminder_template="Бот: {link}")
    await _debtors(db, (CASE, "Сидоров Сидор Сидорович"))
    await e.debtor()  # номер к тому же есть в реестре должников из CSV
    await sync_contacts(db, [rc("people/1", name, "+" + PHONE)])
    # номер есть в реестре (CSV), поэтому синхронизация сочла его должником; управляющий
    # на проверке решил иначе — «не должник» / оставить спорным:
    async with db.session() as s, s.begin():
        row = (await s.execute(select(PhoneContact))).scalar_one()
        row.status, row.decided_by = (OTHER if name == "Жена" else REVIEW), "manager"
    assert (await e.msg()).action == "whitelist"
    assert e.transport.sent == [] and "whitelist_match" in e.notifier.kinds()


# --- Google ---

def test_auth_request_has_pkce_and_readonly_scope() -> None:
    url, verifier, state = make_auth_request("cid")
    q = parse_qs(urlparse(url).query)
    assert q["scope"] == ["https://www.googleapis.com/auth/contacts.readonly"]
    assert q["code_challenge_method"] == ["S256"] and q["state"] == [state] and q["access_type"] == ["offline"]
    assert len(verifier) >= 43


def test_parse_redirect() -> None:
    assert parse_redirect("http://127.0.0.1:8765/?state=s&code=abc&scope=x", "s") == "abc"
    with pytest.raises(GoogleAuthError):
        parse_redirect("http://127.0.0.1:8765/?state=other&code=abc", "s")
    with pytest.raises(GoogleAuthError):
        parse_redirect("http://127.0.0.1:8765/?error=access_denied&state=s", "s")


async def test_exchange_without_refresh_token_fails() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"access_token": "a"})))
    with pytest.raises(GoogleAuthError, match="refresh_token"):
        await exchange_code(client, "id", "secret", "code", "verifier")


def test_person_to_contact() -> None:
    p = {
        "resourceName": "people/c1",
        "names": [{"displayName": "Иван Иванов", "familyName": "Иванов", "givenName": "Иван", "middleName": "Иванович"}],
        "phoneNumbers": [{"value": "8 (916) 111-11-11", "canonicalForm": "+79161111111"}, {"value": "+7 916 222 22 22"}],
    }
    c = person_to_contact(p)
    assert c.names == ("Иван Иванов", "Иванов Иван Иванович")
    assert c.phones == ("+79161111111", "+7 916 222 22 22")
    assert person_to_contact({"resourceName": "people/c2", "names": [{"displayName": "X"}]}) is None


async def test_fetch_contacts_paginates() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        assert request.headers["Authorization"] == "Bearer tok"
        if "pageToken" not in request.url.params:
            return httpx.Response(200, json={"connections": [
                {"resourceName": "people/1", "names": [{"displayName": "А"}], "phoneNumbers": [{"value": "+79161111111"}]}
            ], "nextPageToken": "p2"})
        return httpx.Response(200, json={"connections": [
            {"resourceName": "people/2", "names": [{"displayName": "Б"}], "phoneNumbers": [{"value": "+79162222222"}]}
        ]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    contacts = await fetch_contacts(client, "tok")
    assert [c.resource_name for c in contacts] == ["people/1", "people/2"]
    assert calls[0]["personFields"] == "names,phoneNumbers" and calls[0]["pageSize"] == "1000"
    assert calls[1]["pageToken"] == "p2"


def test_refresh_token_file_is_private(tmp_path: Path) -> None:
    f = tmp_path / "g" / "token.json"
    save_refresh_token(f, "rt")
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert json.loads(f.read_text())["refresh_token"] == "rt"
