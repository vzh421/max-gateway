"""Контакты телефона: сопоставление ФИО, синхронизация, проверка спорных, Google, S1."""

from __future__ import annotations

from pathlib import Path

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
from gateway.google_csv import GoogleCsvError, parse_google_csv
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
    assert dp == [("C1", "79161111111", "google_csv")]


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
    r3 = await sync_contacts(db, contacts[:1])  # частичный файл ничего не удаляет
    assert r3.removed == 0 and "79162222222" in await _statuses(db)
    r4 = await sync_contacts(db, contacts[:1], remove_missing=True)  # полный список (--replace)
    assert r4.removed == 1 and "79162222222" not in await _statuses(db)


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


# --- CSV-экспорт Google Контактов ---

# Набор и порядок колонок — как в реальном экспорте управляющего (без данных).
HEADERS = (
    "First Name,Middle Name,Last Name,Phonetic First Name,Phonetic Middle Name,Phonetic Last Name,"
    "Name Prefix,Name Suffix,Nickname,File As,Organization Name,Organization Title,Organization Department,"
    "Birthday,Notes,Photo,Labels,E-mail 1 - Label,E-mail 1 - Value,E-mail 2 - Label,E-mail 2 - Value,"
    + ",".join(f"Phone {i} - Label,Phone {i} - Value" for i in range(1, 8))
)


def _row(first="", middle="", last="", phones=("", "")) -> str:
    cols = [""] * len(HEADERS.split(","))
    idx = {h: i for i, h in enumerate(HEADERS.split(","))}
    cols[idx["First Name"]], cols[idx["Middle Name"]], cols[idx["Last Name"]] = first, middle, last
    cols[idx["Labels"]] = "* myContacts"
    cols[idx["Phone 1 - Value"]], cols[idx["Phone 2 - Value"]] = phones
    return ",".join(f'"{c}"' for c in cols)


def test_parse_google_csv_real_layout() -> None:
    text = "\n".join([
        HEADERS,
        _row("Иван", "Иванович", "Иванов", ("+7 916 111-11-11 ::: 8\u00a0(916)\u00a0222-22-22", "")),
        _row("Иван Иванов", "", "", ("+79161111111", "")),  # тот же номер — имена объединяются
        _row("Такси", "", "", ("", "")),  # без телефона
        _row("Мастер", "", "", ("123-45", "+1 202 555 0100")),  # короткий и иностранный
    ])
    contacts, bad = parse_google_csv(text)
    by = {c.phones[0]: c.names for c in contacts}
    assert by["+79161111111"] == ("Иванов Иван Иванович", "Иван Иванов")
    assert by["+79162222222"] == ("Иванов Иван Иванович",)
    assert bad == 2 and len(contacts) == 2


def test_parse_google_csv_unknown_layout_refused() -> None:
    with pytest.raises(GoogleCsvError, match="Phone 1 - Value"):
        parse_google_csv("Имя,Телефон\nИван,+79161111111\n")


async def test_csv_import_end_to_end(db, tmp_path: Path) -> None:
    from gateway.google_csv import load_google_csv

    await _debtors(db, ("C1", "Иванов Иван Иванович"))
    f = tmp_path / "contacts.csv"
    f.write_text("\n".join([HEADERS, _row("Иван", "Иванович", "Иванов", ("+7 916 111-11-11", "")),
                             _row("Жена", "", "", ("+7 916 333-33-33", ""))]), encoding="utf-8")
    contacts, _ = load_google_csv(f)
    await sync_contacts(db, contacts)
    st = await _statuses(db)
    assert st["79161111111"][0] == DEBTOR and st["79163333333"][0] == OTHER
