"""Команды: python -m gateway <команда>.

  login                   вход по SMS (управляющий лично) + белый список из адресной книги
  contacts-sync           повторная попытка получить адресную книгу (без SMS) для белого списка
  run                     запуск сервиса
  db upgrade              применить миграции
  import-phones FILE      импорт телефонов должников из CSV (case_id, phone[, note])
  whitelist add PHONE     добавить номер в белый список
  whitelist list          показать белый список (номера маскированы)
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
    if args.cmd == "kill":
        return cmd_kill(args)
    if args.cmd == "sends":
        return asyncio.run(cmd_sends(args))
    return 2


if __name__ == "__main__":
    sys.exit(main())
