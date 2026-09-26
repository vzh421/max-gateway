"""Импорт телефонов должников и белого списка."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from .db import Database, utcnow
from .db.models import DebtorPhone, WhitelistEntry
from .pii import mask_phone, normalize_phone
from .transport.base import Contact

STATE_ADDRESS_BOOK_IMPORTED = "whitelist_address_book_imported"


@dataclass
class ImportReport:
    total_rows: int = 0
    added: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"строк: {self.total_rows}, добавлено: {self.added}, уже были: {self.duplicates}, ошибок: {len(self.errors)}"]
        lines += [f"  {e}" for e in self.errors[:50]]
        if len(self.errors) > 50:
            lines.append(f"  … ещё {len(self.errors) - 50}")
        return "\n".join(lines)


def _read_csv(path: Path) -> list[dict[str, str]]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("Кодировка CSV не распознана (ожидается UTF-8 или Windows-1251)")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in reader]


@dataclass(frozen=True)
class PhoneRow:
    case_id: str
    phone: str
    note: str | None


def parse_debtor_phones(rows: list[dict[str, str]], report: ImportReport) -> list[PhoneRow]:
    """Колонки: case_id, phone; необязательно note. Номер нормализуется в 7XXXXXXXXXX."""
    result: list[PhoneRow] = []
    for i, row in enumerate(rows, start=2):  # строка 1 — заголовок
        report.total_rows += 1
        case_id = row.get("case_id", "")
        raw_phone = row.get("phone", "")
        phone = normalize_phone(raw_phone)
        if not case_id:
            report.errors.append(f"строка {i}: пустой case_id")
            continue
        if not phone:
            report.errors.append(f"строка {i}: номер не распознан ({mask_phone(raw_phone) if raw_phone else 'пусто'})")
            continue
        result.append(PhoneRow(case_id=case_id, phone=phone, note=row.get("note") or None))
    return result


async def add_debtor_phones(db: Database, rows: list[PhoneRow], source: str, report: ImportReport) -> None:
    async with db.session() as s, s.begin():
        existing = {
            (c, p) for c, p in (await s.execute(select(DebtorPhone.case_id, DebtorPhone.phone))).all()
        }
        for r in rows:
            key = (r.case_id, r.phone)
            if key in existing:
                report.duplicates += 1
                continue
            existing.add(key)
            s.add(DebtorPhone(case_id=r.case_id, phone=r.phone, source=source, note=r.note, created_at=utcnow()))
            report.added += 1


async def import_debtor_phones_csv(db: Database, path: Path, source: str | None = None) -> ImportReport:
    report = ImportReport()
    rows = parse_debtor_phones(_read_csv(path), report)
    await add_debtor_phones(db, rows, source or f"csv:{path.name}", report)
    return report


async def add_whitelist(
    db: Database, *, phone: str | None = None, max_user_id: int | None = None, source: str = "manual", note: str | None = None
) -> bool:
    """False — запись уже есть (по телефону или id MAX)."""
    if phone is None and max_user_id is None:
        raise ValueError("Нужен телефон или id MAX")
    async with db.session() as s, s.begin():
        if phone and await s.scalar(select(WhitelistEntry.id).where(WhitelistEntry.phone == phone)):
            return False
        if max_user_id is not None and await s.scalar(
            select(WhitelistEntry.id).where(WhitelistEntry.max_user_id == max_user_id)
        ):
            return False
        s.add(WhitelistEntry(phone=phone, max_user_id=max_user_id, source=source, note=note, created_at=utcnow()))
    return True


async def import_whitelist_from_contacts(db: Database, contacts: list[Contact], *, force: bool = False) -> ImportReport:
    """Белый список из адресной книги — один раз, при первом запуске (правило 3).

    Пустой список не считается импортом: при восстановленной сессии сервер MAX отдаёт
    только изменения контактов (журнал этапа 0), и пустота не значит «контактов нет».
    """
    report = ImportReport()
    done = await db.get_state(STATE_ADDRESS_BOOK_IMPORTED)
    if done and not force:
        report.errors.append(f"адресная книга уже импортирована {done.get('at')} — повтор только с --force")
        return report
    if not contacts:
        report.errors.append("MAX не вернул ни одного контакта — импорт не отмечен как выполненный")
        return report
    for c in contacts:
        report.total_rows += 1
        if await add_whitelist(db, phone=c.phone, max_user_id=c.user_id, source="address_book"):
            report.added += 1
        else:
            report.duplicates += 1
    await db.set_state(
        STATE_ADDRESS_BOOK_IMPORTED,
        {"at": utcnow().isoformat(), "contacts": len(contacts), "with_phone": sum(1 for c in contacts if c.phone)},
    )
    return report


async def is_whitelisted(db: Database, *, phone: str | None, max_user_id: int | None) -> bool:
    async with db.session() as s:
        if phone and await s.scalar(select(WhitelistEntry.id).where(WhitelistEntry.phone == phone)):
            return True
        if max_user_id is not None and await s.scalar(
            select(WhitelistEntry.id).where(WhitelistEntry.max_user_id == max_user_id)
        ):
            return True
    return False
