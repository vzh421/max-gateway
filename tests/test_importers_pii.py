"""Импорт телефонов, белый список (правило 3), маскировка ПДн в логах."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy import select

from gateway.db.models import DebtorPhone
from gateway.importers import (
    STATE_ADDRESS_BOOK_IMPORTED,
    add_whitelist,
    import_debtor_phones_csv,
    import_whitelist_from_contacts,
    is_whitelisted,
)
from gateway.pii import mask_phone, mask_text, normalize_phone, setup_logging
from gateway.transport.base import Contact


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+7 (916) 123-45-67", "79161234567"),
        ("89161234567", "79161234567"),
        ("9161234567", "79161234567"),
        (79161234567, "79161234567"),  # так User.phone приходит из PyMax (int)
        ("12345", None),
        ("", None),
        (None, None),
        ("+1 202 555 0100", None),
    ],
)
def test_normalize_phone(raw, expected) -> None:
    assert normalize_phone(raw) == expected


def test_mask_text() -> None:
    s = mask_text("тел +7 916 123-45-67, ИНН 500100732259, СНИЛС 112-233-445 95, id 117336221156998923")
    assert "123-45-67" not in s and "500100732259" not in s and "112-233-445" not in s
    assert "117336221156998923" in s  # длинные id сообщений не трогаем
    assert mask_phone("79161234567") == "7********67"


def test_log_filter_masks_own_and_pymax_logs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO")
    from pymax import Client, ExtraConfig

    # Клиент в сеть не ходит до start(); создаём, чтобы PyMax применил свою настройку логов.
    Client(phone="+70000000000", work_dir=str(tmp_path), extra_config=ExtraConfig(telemetry=False))
    logging.getLogger("gateway.x").warning("номер %s", "+79161234567")
    logging.getLogger("pymax.app").warning("phone=79161234567 inn=7707083893")
    err = capsys.readouterr().err
    assert "79161234567" not in err and "7707083893" not in err
    assert "7********67" in err


def _write(path: Path, text: str, enc: str = "utf-8") -> Path:
    path.write_bytes(text.encode(enc))
    return path


async def test_import_phones_csv(db, tmp_path: Path) -> None:
    f = _write(tmp_path / "p.csv", "case_id;phone;note\n101;+7 916 123-45-67;моб\n101;89161234567;\n102;abc;\n;79160000000;\n")
    report = await import_debtor_phones_csv(db, f)
    assert (report.total_rows, report.added, report.duplicates, len(report.errors)) == (4, 1, 1, 2)
    assert "abc" not in report.summary()
    async with db.session() as s:
        rows = (await s.execute(select(DebtorPhone))).scalars().all()
    assert [(r.case_id, r.phone, r.source) for r in rows] == [("101", "79161234567", "csv:p.csv")]


async def test_import_phones_cp1251_and_repeat(db, tmp_path: Path) -> None:
    f = _write(tmp_path / "p.csv", "case_id,phone,note\n7,9161234567,Иванов\n", enc="cp1251")
    assert (await import_debtor_phones_csv(db, f)).added == 1
    assert (await import_debtor_phones_csv(db, f)).duplicates == 1


async def test_whitelist_from_contacts_once(db) -> None:
    contacts = [Contact(114281389, "79161234567"), Contact(543835, None)]
    r1 = await import_whitelist_from_contacts(db, contacts)
    assert r1.added == 2
    assert await db.get_state(STATE_ADDRESS_BOOK_IMPORTED)
    r2 = await import_whitelist_from_contacts(db, [Contact(1, "79990000000")])
    assert r2.added == 0 and r2.errors
    assert await is_whitelisted(db, phone="79161234567", max_user_id=None)
    assert await is_whitelisted(db, phone=None, max_user_id=543835)
    assert not await is_whitelisted(db, phone="79990000000", max_user_id=1)


async def test_empty_contacts_not_marked_imported(db) -> None:
    report = await import_whitelist_from_contacts(db, [])
    assert report.errors and await db.get_state(STATE_ADDRESS_BOOK_IMPORTED) is None


async def test_manual_whitelist(db) -> None:
    assert await add_whitelist(db, phone="79161234567", note="жена")
    assert not await add_whitelist(db, phone="79161234567")
    with pytest.raises(ValueError):
        await add_whitelist(db)
