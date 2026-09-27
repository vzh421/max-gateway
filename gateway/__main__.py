"""Команды: python -m gateway <команда>.

  login                   вход по SMS (управляющий лично) + белый список из адресной книги
  contacts-sync           повторная попытка получить адресную книгу (без SMS) для белого списка
  run                     запуск сервиса
  db upgrade              применить миграции
  import-phones FILE      импорт телефонов должников из CSV (case_id, phone[, note])
  whitelist add PHONE     добавить номер в белый список
  whitelist list          показать белый список (номера маскированы)
  import-debtors FILE     список должников: case-map.json или CSV (case_id, debtor_name[, case_number])
  contacts import FILE [--replace]   контакты телефона из CSV-экспорта Google Контактов:
                          разделить на должников / остальных / спорных
  contacts review-export FILE   спорные — в CSV для проверки (открывается в Excel)
  contacts review-import FILE   загрузить решения («должник» / «не должник»)
  contacts stats          сколько контактов в каждой группе
  history-export FILE     выгрузить обезличенную переписку с должниками (только чтение; сервис остановить)
  kill on|off|status      аварийный выключатель отправок
  sends status|resume     блокировка отправок мониторингом
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

from .config import get_settings
from .db import Database
from .db.models import WhitelistEntry
from .importers import add_whitelist, import_debtor_phones_csv, import_whitelist_from_contacts
from .pii import mask_phone, normalize_phone, setup_logging
from .safety import STATE_SENDS_BLOCKED, KillSwitch

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def db_upgrade(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


async def cmd_login(args: argparse.Namespace, *, full_contacts_sync: bool) -> int:
    from .transport.pymax_transport import SESSION_NAME, PyMaxTransport

    settings = get_settings()
    if settings.max_phone is None:
        print("MAX_PHONE не задан в .env")
        return 2
    session_file = settings.session_dir / SESSION_NAME
    if not full_contacts_sync and session_file.exists():
        print(f"Сессия уже есть ({session_file}). Для белого списка используйте: python -m gateway contacts-sync")
        return 1
    if full_contacts_sync and not session_file.exists():
        print("Сессии нет. Сначала: python -m gateway login")
        return 1
    transport = PyMaxTransport(settings.max_phone.get_secret_value(), settings.session_dir)
    print("Подключение к MAX…" + (" Придёт SMS, код вводится здесь." if not full_contacts_sync else ""))
    contacts = await transport.login_interactive(full_contacts_sync=full_contacts_sync)
    print(f"Вход выполнен. Контактов получено: {len(contacts)}, с телефоном: {sum(1 for c in contacts if c.phone)}")
    db = Database(settings.database_url)
    try:
        report = await import_whitelist_from_contacts(db, contacts, force=args.force)
    finally:
        await db.dispose()
    print("Белый список из адресной книги:", report.summary())
    return 0


async def cmd_import_phones(args: argparse.Namespace) -> int:
    db = Database(get_settings().database_url)
    try:
        report = await import_debtor_phones_csv(db, Path(args.file), args.source)
    finally:
        await db.dispose()
    print(report.summary())
    return 0 if not report.errors else 1


async def cmd_whitelist(args: argparse.Namespace) -> int:
    db = Database(get_settings().database_url)
    try:
        if args.action == "add":
            phone = normalize_phone(args.phone)
            if not phone:
                print("Номер не распознан")
                return 2
            added = await add_whitelist(db, phone=phone, source="manual", note=args.note)
            print("Добавлен" if added else "Уже есть в белом списке", mask_phone(phone))
        else:
            async with db.session() as s:
                rows = (await s.execute(select(WhitelistEntry).order_by(WhitelistEntry.id))).scalars().all()
            for r in rows:
                print(r.id, mask_phone(r.phone) if r.phone else "-", r.max_user_id or "-", r.source, r.note or "")
            print(f"Всего: {len(rows)}")
    finally:
        await db.dispose()
    return 0


async def cmd_import_debtors(args: argparse.Namespace) -> int:
    from .contacts import import_case_map, import_debtors_csv

    db = Database(get_settings().database_url)
    path = Path(args.file)
    try:
        if path.suffix.lower() == ".json":
            try:
                report, skipped = await import_case_map(db, path)
            except ValueError as e:
                print(f"Файл не распознан: {e}")
                return 2
            print("Пропущено:", ", ".join(f"{k}: {v}" for k, v in skipped.items()))
        else:
            report = await import_debtors_csv(db, path, args.source)
    finally:
        await db.dispose()
    print("Должники:", report.summary().replace("уже были", "обновлено"))
    print("После обновления списка должников загрузите контакты заново: contacts import <файл>")
    return 0 if not report.errors else 1


async def cmd_contacts(args: argparse.Namespace) -> int:
    from .contacts import contact_stats, export_review, import_review, sync_contacts

    settings = get_settings()
    db = Database(settings.database_url)
    try:
        if args.action == "import":
            from .google_csv import GoogleCsvError, load_google_csv

            if not args.file:
                print("Укажите файл CSV")
                return 2
            try:
                contacts, bad = load_google_csv(Path(args.file))
            except GoogleCsvError as e:
                print(f"Файл не распознан: {e}")
                return 2
            report = await sync_contacts(db, contacts, remove_missing=args.replace)
            report.bad_phones += bad
            print("Контакты:", report.summary())
            if report.by_status["review"]:
                print("Спорные: python -m gateway contacts review-export review.csv")
        elif args.action == "review-export":
            if not args.file:
                print("Укажите файл")
                return 2
            n = await export_review(db, Path(args.file))
            print(f"Спорных: {n}. Файл: {args.file}. Заполните колонку «решение»: должник / не должник")
        elif args.action == "review-import":
            if not args.file:
                print("Укажите файл")
                return 2
            report = await import_review(db, Path(args.file))
            print("Решения:", report.summary().replace("добавлено", "принято"))
        else:
            print(await contact_stats(db))
    finally:
        await db.dispose()
    return 0


async def cmd_history_export(args: argparse.Namespace) -> int:
    from collections import defaultdict

    from sqlalchemy import select as sa_select

    from .db.models import Debtor, DebtorPhone
    from .history_export import export_history
    from .transport.pymax_transport import run_readonly

    settings = get_settings()
    if settings.max_phone is None:
        print("MAX_PHONE не задан в .env")
        return 2
    db = Database(settings.database_url)
    try:
        async with db.session() as s:
            phones: dict[str, list[str]] = defaultdict(list)
            for case_id, phone in (await s.execute(sa_select(DebtorPhone.case_id, DebtorPhone.phone))).all():
                phones[phone].append(case_id)
            names = {d.case_id: d.debtor_name for d in (await s.execute(sa_select(Debtor))).scalars()}
    finally:
        await db.dispose()
    if not phones:
        print("Реестр телефонов должников пуст — сначала import-debtors и contacts import")
        return 2
    out = Path(args.file)

    async def job(reader):  # type: ignore[no-untyped-def]
        return await export_history(
            reader, dict(phones), names, out,
            manager_names=args.manager_name or [], per_chat=args.per_chat, max_chats=args.max_chats,
        )

    report = await run_readonly(settings.max_phone.get_secret_value(), settings.session_dir, job)
    import os

    os.chmod(out, 0o600)
    print("Выгрузка:", report.summary())
    print(f"Файл: {out} (персональные данные — только управляющему)")
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    settings = get_settings()
    ks = KillSwitch(settings.kill_switch, settings.kill_switch_file)
    if args.action == "on":
        ks.enable(args.note or "")
    elif args.action == "off":
        ks.disable()
        if settings.kill_switch:
            print("Внимание: KILL_SWITCH=true в .env — выключатель остаётся включённым")
    print("Выключатель:", "ВКЛЮЧЁН — отправки запрещены" if ks.active else "выключен", ks.reason())
    return 0


async def cmd_sends(args: argparse.Namespace) -> int:
    db = Database(get_settings().database_url)
    try:
        if args.action == "resume":
            await db.set_state(STATE_SENDS_BLOCKED, None)
            print("Блокировка отправок снята")
        else:
            state = await db.get_state(STATE_SENDS_BLOCKED)
            print("Отправки заблокированы:", state["reason"], state["at"]) if state else print("Блокировки нет")
    finally:
        await db.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gateway", description="Шлюз MAX")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("login", help="вход по SMS (управляющий лично)")
    p.add_argument("--force", action="store_true", help="переимпортировать адресную книгу в белый список")
    p = sub.add_parser("contacts-sync", help="получить адресную книгу по существующей сессии")
    p.add_argument("--force", action="store_true")
    sub.add_parser("run", help="запуск сервиса")
    p = sub.add_parser("db")
    p.add_argument("action", choices=["upgrade"])
    p = sub.add_parser("import-phones")
    p.add_argument("file")
    p.add_argument("--source", default=None)
    p = sub.add_parser("whitelist")
    p.add_argument("action", choices=["add", "list"])
    p.add_argument("phone", nargs="?")
    p.add_argument("--note", default=None)
    p = sub.add_parser("import-debtors")
    p.add_argument("file")
    p.add_argument("--source", default=None)
    p = sub.add_parser("contacts")
    p.add_argument("action", choices=["import", "review-export", "review-import", "stats"])
    p.add_argument("file", nargs="?")
    p.add_argument("--replace", action="store_true", help="файл — полный список: удалить номера, которых в нём нет")
    p = sub.add_parser("history-export")
    p.add_argument("file")
    p.add_argument("--per-chat", type=int, default=200)
    p.add_argument("--max-chats", type=int, default=300)
    p.add_argument("--manager-name", action="append", help="ФИО управляющего для замены в текстах (можно несколько)")
    p = sub.add_parser("kill")
    p.add_argument("action", choices=["on", "off", "status"])
    p.add_argument("--note", default=None)
    p = sub.add_parser("sends")
    p.add_argument("action", choices=["status", "resume"])
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level)  # до создания клиента PyMax — чтобы его логи шли через маскировку

    if args.cmd == "login":
        return asyncio.run(cmd_login(args, full_contacts_sync=False))
    if args.cmd == "contacts-sync":
        return asyncio.run(cmd_login(args, full_contacts_sync=True))
    if args.cmd == "run":
        from .app import run_service

        asyncio.run(run_service(settings))
        return 0
    if args.cmd == "db":
        db_upgrade(settings.database_url)
        print("Миграции применены")
        return 0
    if args.cmd == "import-phones":
        return asyncio.run(cmd_import_phones(args))
    if args.cmd == "whitelist":
        if args.action == "add" and not args.phone:
            parser.error("whitelist add: нужен номер")
        return asyncio.run(cmd_whitelist(args))
    if args.cmd == "import-debtors":
        return asyncio.run(cmd_import_debtors(args))
    if args.cmd == "contacts":
        return asyncio.run(cmd_contacts(args))
    if args.cmd == "history-export":
        return asyncio.run(cmd_history_export(args))
    if args.cmd == "kill":
        return cmd_kill(args)
    if args.cmd == "sends":
        return asyncio.run(cmd_sends(args))
    return 2


if __name__ == "__main__":
    sys.exit(main())
