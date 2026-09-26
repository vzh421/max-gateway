"""Контакты телефона: кто должник (направлять в бот), кто нет (не отвечать никогда).

Классификация каждого номера из контактов:
- **debtor** — номер уже есть в debtor_phones ИЛИ ФИО контакта точно совпало ровно с
  одним должником. Такие номера добавляются в debtor_phones и получают перенаправление.
- **review** — сомнительное совпадение (частичное ФИО, инициалы, несколько должников).
  До решения управляющего номер защищён, как «другой».
- **other** — остальные контакты: жена, коллеги, суды… Им шлюз не отвечает никогда.
Решения управляющего (decided_by='manager') повторная синхронизация не меняет.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select

from .db import Database, utcnow
from .db.models import Debtor, DebtorPhone, PhoneContact, WhitelistEntry
from .importers import ImportReport, _read_csv
from .names import Match, match, tokens
from .pii import normalize_phone

DEBTOR, REVIEW, OTHER = "debtor", "review", "other"
MAX_CANDIDATES = 5


@dataclass(frozen=True)
class RawContact:
    resource_name: str
    names: tuple[str, ...]  # варианты имени (displayName, unstructuredName, …)
    phones: tuple[str, ...]  # как пришли из источника


@dataclass(frozen=True)
class Classified:
    status: str
    case_ids: tuple[str, ...] = ()
    reason: str = ""


@dataclass
class SyncReport:
    contacts: int = 0
    phones: int = 0
    by_status: dict[str, int] = field(default_factory=lambda: {DEBTOR: 0, REVIEW: 0, OTHER: 0})
    new_review: int = 0
    new_debtor_phones: int = 0
    kept_manager_decisions: int = 0
    removed: int = 0
    bad_phones: int = 0

    def summary(self) -> str:
        return (
            f"контактов: {self.contacts}, номеров: {self.phones} (нераспознанных: {self.bad_phones}); "
            f"должники: {self.by_status[DEBTOR]}, на проверке: {self.by_status[REVIEW]} "
            f"(новых: {self.new_review}), остальные: {self.by_status[OTHER]}; "
            f"добавлено телефонов должников: {self.new_debtor_phones}; "
            f"решений управляющего сохранено: {self.kept_manager_decisions}; удалено исчезнувших: {self.removed}"
        )


class DebtorIndex:
    def __init__(self, debtors: list[tuple[str, str]], debtor_phones: dict[str, set[str]]) -> None:
        self.debtors = debtors  # (case_id, debtor_name)
        self.by_phone = debtor_phones
        self._by_word: dict[str, list[int]] = defaultdict(list)
        for i, (_, name) in enumerate(debtors):
            for w in set(t for t in tokens(name) if len(t) > 1):
                self._by_word[w].append(i)

    @classmethod
    async def load(cls, db: Database) -> DebtorIndex:
        async with db.session() as s:
            debtors = [(r.case_id, r.debtor_name) for r in (await s.execute(select(Debtor))).scalars()]
            phones: dict[str, set[str]] = defaultdict(set)
            for case_id, phone in (await s.execute(select(DebtorPhone.case_id, DebtorPhone.phone))).all():
                phones[phone].add(case_id)
        return cls(debtors, phones)

    def classify(self, names: tuple[str, ...], phone: str) -> Classified:
        if phone in self.by_phone:
            return Classified(DEBTOR, tuple(sorted(self.by_phone[phone])), "телефон уже в реестре должников")
        exact: set[str] = set()
        partial: set[str] = set()
        for name in names:
            candidates = {i for w in set(tokens(name)) for i in self._by_word.get(w, ())}
            for i in candidates:
                case_id, debtor_name = self.debtors[i]
                m = match(name, debtor_name)
                if m is Match.EXACT:
                    exact.add(case_id)
                elif m is Match.PARTIAL:
                    partial.add(case_id)
        if len(exact) == 1 and not (partial - exact):
            return Classified(DEBTOR, tuple(exact), "ФИО совпало полностью")
        if exact or partial:
            cands = sorted(exact) + sorted(partial - exact)
            reason = "ФИО совпало с несколькими должниками" if len(exact) > 1 else (
                "ФИО совпало полностью, но есть и частичные совпадения" if exact else "ФИО совпало частично"
            )
            return Classified(REVIEW, tuple(cands[:MAX_CANDIDATES]), reason)
        return Classified(OTHER)


async def sync_contacts(db: Database, contacts: list[RawContact], *, source: str = "google_contacts") -> SyncReport:
    """Полная синхронизация: список contacts — все контакты телефона на сейчас."""
    index = await DebtorIndex.load(db)
    report = SyncReport(contacts=len(contacts))
    now = utcnow()
    seen: set[tuple[str, str]] = set()
    async with db.session() as s, s.begin():
        existing = {(r.resource_name, r.phone): r for r in (await s.execute(select(PhoneContact))).scalars()}
        known_debtor_phones = {
            (c, p) for c, p in (await s.execute(select(DebtorPhone.case_id, DebtorPhone.phone))).all()
        }
        for contact in contacts:
            display = next((n for n in contact.names if n), None)
            for raw_phone in contact.phones:
                phone = normalize_phone(raw_phone)
                if not phone:
                    report.bad_phones += 1
                    continue
                key = (contact.resource_name, phone)
                if key in seen:
                    continue
                seen.add(key)
                report.phones += 1
                row = existing.get(key)
                if row is not None and row.decided_by == "manager":
                    row.seen_at = now
                    if row.status != OTHER:
                        row.display_name = display
                    report.kept_manager_decisions += 1
                    report.by_status[row.status] += 1
                    continue
                c = index.classify(contact.names, phone)
                if row is None:
                    row = PhoneContact(resource_name=contact.resource_name, phone=phone, created_at=now, decided_by="auto")
                    s.add(row)
                    if c.status == REVIEW:
                        report.new_review += 1
                elif c.status == REVIEW and row.status != REVIEW:
                    report.new_review += 1
                row.status = c.status
                row.case_ids = list(c.case_ids) or None
                row.match_reason = c.reason or None
                row.display_name = display if c.status != OTHER else None  # минимум ПДн для «других»
                row.seen_at = now
                report.by_status[c.status] += 1
                if c.status == DEBTOR:
                    for case_id in c.case_ids:
                        if (case_id, phone) not in known_debtor_phones:
                            s.add(DebtorPhone(case_id=case_id, phone=phone, source=source, created_at=now))
                            known_debtor_phones.add((case_id, phone))
                            report.new_debtor_phones += 1
        # Контакты, удалённые из телефона, больше не защищаются и не считаются должниками.
        gone = [r.id for k, r in existing.items() if k not in seen]
        if gone:
            await s.execute(delete(PhoneContact).where(PhoneContact.id.in_(gone)))
        report.removed = len(gone)
    return report


async def protection_reason(db: Database, *, phone: str | None, max_user_id: int | None) -> str | None:
    """Почему этому собеседнику нельзя отвечать; None — можно.

    1. Ручной белый список — всегда.
    2. Контакты телефона: номер должника (все записи — debtor) → можно; иначе
       (другой / на проверке) → нельзя.
    3. Адресная книга MAX (импорт при login) — нельзя, если номер не подтверждён
       как должник по контактам телефона.
    """
    async with db.session() as s:
        manual = select(WhitelistEntry.id).where(WhitelistEntry.source == "manual")
        if phone and await s.scalar(manual.where(WhitelistEntry.phone == phone)):
            return "ручной белый список"
        if max_user_id is not None and await s.scalar(manual.where(WhitelistEntry.max_user_id == max_user_id)):
            return "ручной белый список"
        if phone:
            statuses = set((await s.execute(select(PhoneContact.status).where(PhoneContact.phone == phone))).scalars())
            if statuses:
                if statuses == {DEBTOR}:
                    return None
                return "контакт телефона на проверке" if REVIEW in statuses else "контакт телефона (не должник)"
        book = select(WhitelistEntry.id).where(WhitelistEntry.source == "address_book")
        if phone and await s.scalar(book.where(WhitelistEntry.phone == phone)):
            return "адресная книга MAX"
        if max_user_id is not None and await s.scalar(book.where(WhitelistEntry.max_user_id == max_user_id)):
            return "адресная книга MAX"
    return None


# --- должники из CSV ---

async def import_debtors_csv(db: Database, path: Path, source: str | None = None) -> ImportReport:
    """Колонки: case_id, debtor_name; необязательно case_number. Повтор — обновление."""
    report = ImportReport()
    rows = _read_csv(path)
    now = utcnow()
    async with db.session() as s, s.begin():
        for i, row in enumerate(rows, start=2):
            report.total_rows += 1
            case_id, name = row.get("case_id", ""), row.get("debtor_name", "")
            if not case_id or not tokens(name):
                report.errors.append(f"строка {i}: нет case_id или debtor_name")
                continue
            existing = await s.get(Debtor, case_id)
            if existing:
                existing.debtor_name, existing.case_number = name, row.get("case_number") or existing.case_number
                existing.updated_at = now
                report.duplicates += 1
            else:
                s.add(Debtor(case_id=case_id, case_number=row.get("case_number") or None, debtor_name=name,
                             source=source or f"csv:{path.name}", updated_at=now))
                report.added += 1
    return report


# --- проверка спорных управляющим (CSV, открывается в Excel) ---

REVIEW_COLUMNS = ["id", "имя_в_контактах", "телефон", "кандидаты", "причина", "решение", "case_id"]
DECISION_DEBTOR = {"должник", "да", "д"}
DECISION_OTHER = {"не должник", "нет", "н"}


async def export_review(db: Database, path: Path) -> int:
    async with db.session() as s:
        rows = (await s.execute(select(PhoneContact).where(PhoneContact.status == REVIEW).order_by(PhoneContact.id))).scalars().all()
        debtors = {d.case_id: d for d in (await s.execute(select(Debtor))).scalars()}
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(REVIEW_COLUMNS)
    for r in rows:
        cands = []
        for cid in r.case_ids or []:
            d = debtors.get(cid)
            cands.append(f"{cid}: {d.debtor_name}{' (' + d.case_number + ')' if d and d.case_number else ''}" if d else cid)
        w.writerow([r.id, r.display_name or "", "+" + r.phone, " | ".join(cands), r.match_reason or "", "", ""])
    # UTF-8 с BOM и «;» — Excel открывает без мастера импорта.
    path.write_text(buf.getvalue(), encoding="utf-8-sig")
    return len(rows)


async def import_review(db: Database, path: Path) -> ImportReport:
    """Колонка «решение»: «должник» или «не должник» (пусто — пропуск).

    Для «должник» при нескольких кандидатах нужен case_id; при одном — берётся он.
    """
    report = ImportReport()
    now = utcnow()
    async with db.session() as s, s.begin():
        for i, row in enumerate(_read_csv(path), start=2):
            decision = row.get("решение", "").strip().lower()
            if not decision:
                continue
            report.total_rows += 1
            try:
                pc = await s.get(PhoneContact, int(row.get("id", "")))
            except ValueError:
                pc = None
            if pc is None:
                report.errors.append(f"строка {i}: нет записи с таким id")
                continue
            if decision in DECISION_OTHER:
                pc.status, pc.case_ids = OTHER, None
            elif decision in DECISION_DEBTOR:
                case_id = row.get("case_id", "").strip()
                cands = pc.case_ids or []
                if not case_id and len(cands) == 1:
                    case_id = cands[0]
                if not case_id or await s.get(Debtor, case_id) is None and case_id not in cands:
                    report.errors.append(f"строка {i}: для «должник» укажите case_id одного из кандидатов")
                    continue
                pc.status, pc.case_ids = DEBTOR, [case_id]
                exists = await s.scalar(
                    select(DebtorPhone.id).where(DebtorPhone.case_id == case_id, DebtorPhone.phone == pc.phone)
                )
                if not exists:
                    s.add(DebtorPhone(case_id=case_id, phone=pc.phone, source="manager_review", created_at=now))
            else:
                report.errors.append(f"строка {i}: решение «{decision}» не распознано (должник / не должник)")
                continue
            pc.decided_by, pc.reviewed_at = "manager", now
            if pc.status == OTHER:
                pc.display_name = None
            report.added += 1
    return report


async def contact_stats(db: Database) -> dict[str, int]:
    async with db.session() as s:
        rows = (await s.execute(select(PhoneContact.status, PhoneContact.decided_by))).all()
    stats: dict[str, int] = defaultdict(int)
    for status, by in rows:
        stats[status] += 1
        if by == "manager":
            stats["решено_управляющим"] += 1
    return dict(stats)
