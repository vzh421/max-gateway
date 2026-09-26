"""Прототип этапа 0: только ПЕЧАТАЕТ входящие события личного аккаунта MAX.

Ничего не отправляет, не отмечает прочитанным, не отвечает. Библиотека —
maxapi-python==2.4.1 (PyMax). Все параметры сверены с исходниками этой версии,
см. docs/stage0.md.

Команды:
    python tools/stage0_probe.py login  --phone +7XXXXXXXXXX
    python tools/stage0_probe.py listen --phone +7XXXXXXXXXX [--lookup] [--minutes 60]

login  — интерактивный вход по SMS-коду (вводит управляющий лично), сохраняет
         сессию в --work-dir и печатает сводку по профилю и контактам.
listen — подключается по сохранённой сессии (SMS НЕ запрашивает никогда) и пишет
         входящие события в JSONL с маскировкой персональных данных.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymax import Client, ExtraConfig, Message
from pymax.auth.models import AuthResult

SESSION_NAME = "session.db"

# Ключи, значения которых в raw-кадрах маскируются целиком или частично.
_PHONE_KEYS = {"phone", "phones"}
_NAME_KEYS = {"name", "names", "firstName", "lastName", "first_name", "last_name", "description"}
_TEXT_KEYS = {"text", "caption", "title"}
_SECRET_KEYS = {"token", "loginToken", "login_token", "verifyToken", "hash"}
_URL_KEYS = {"url", "baseUrl", "baseRawUrl", "base_url", "base_raw_url", "link", "previewData"}
_PHONE_RE = re.compile(r"\+?\d{10,12}")


def mask_phone(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value))
    if len(digits) < 5:
        return "<телефон>"
    return f"{digits[0]}{'*' * (len(digits) - 3)}{digits[-2:]}"


def mask_text(value: Any) -> str:
    return f"<текст, {len(str(value))} симв.>"


def mask(obj: Any, key: str | None = None) -> Any:
    """Рекурсивно маскирует ПДн в произвольной структуре, сохраняя форму."""
    if key in _SECRET_KEYS:
        return "<секрет>"
    if key in _PHONE_KEYS:
        if isinstance(obj, list):
            return [mask_phone(v) for v in obj]
        return None if obj is None else mask_phone(obj)
    if key in _NAME_KEYS and obj is not None:
        return "<имя>" if not isinstance(obj, (dict, list)) else _shape(obj)
    if key in _TEXT_KEYS and isinstance(obj, str):
        return mask_text(obj)
    if key in _URL_KEYS and isinstance(obj, str):
        return "<url>"
    if isinstance(obj, dict):
        return {k: mask(v, str(k)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask(v) for v in obj]
    if isinstance(obj, str):
        return _PHONE_RE.sub(lambda m: mask_phone(m.group()), obj)
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int) and 70_000_000_000 <= obj <= 89_999_999_999:
        # Число, похожее на российский номер телефона 7XXXXXXXXXX / 8XXXXXXXXXX.
        return mask_phone(obj)
    if isinstance(obj, bytes):
        return f"<bytes, {len(obj)}>"
    return obj


def _shape(obj: Any) -> Any:
    """Оставляет только структуру (ключи и типы), без значений."""
    if isinstance(obj, dict):
        return {k: _shape(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_shape(v) for v in obj[:1]]
    return f"<{type(obj).__name__}>"


class RefuseAuthFlow:
    """AuthFlow для listen: вместо SMS-входа — немедленная ошибка.

    PyMax при отозванном токене и relogin=True удаляет сессию и вызывает
    auth_flow.authenticate (pymax/base.py, Client.start). Здесь это приводит
    к выходу, а не к отправке SMS на номер управляющего.
    """

    async def authenticate(self, app: Any) -> AuthResult:
        raise RuntimeError(
            "Сохранённой сессии нет или она отозвана. SMS-вход в режиме listen "
            "запрещён — выполните команду login."
        )


def make_config() -> ExtraConfig:
    return ExtraConfig(
        telemetry=False,  # по умолчанию True: имитация действий пользователя
        reconnect=False,  # на этапе 0 разрыв = выход, чтобы увидеть его явно
        log_level="INFO",  # на DEBUG PyMax пишет полные payload с токеном
    )


def secure_session_file(work_dir: Path) -> None:
    for name in (SESSION_NAME, f"{SESSION_NAME}-journal", f"{SESSION_NAME}-wal", f"{SESSION_NAME}-shm"):
        path = work_dir / name
        if path.exists():
            os.chmod(path, 0o600)


class Recorder:
    def __init__(self, out_path: Path) -> None:
        self.out_path = out_path
        self._fh = out_path.open("a", encoding="utf-8")

    def write(self, kind: str, data: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            "data": mask(data),
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        self._fh.write(line + "\n")
        self._fh.flush()
        print(line[:500] + ("…" if len(line) > 500 else ""))

    def close(self) -> None:
        self._fh.close()


def contacts_summary(client: Client) -> dict[str, Any]:
    contacts = [c for c in (client.contacts or []) if c is not None]
    with_phone = sum(1 for c in contacts if getattr(c, "phone", None))
    sample = contacts[0].model_dump(mode="json") if contacts else None
    return {
        "contacts_total": len(contacts),
        "contacts_with_phone": with_phone,
        "contact_structure": _shape(sample) if sample else None,
        "chats_total": len(client.chats or []),
        "chat_types": _count_types(client.chats or []),
    }


def _count_types(chats: list[Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for chat in chats:
        t = str(getattr(chat, "type", None))
        result[t] = result.get(t, 0) + 1
    return result


async def run_login(args: argparse.Namespace) -> None:
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (work_dir / SESSION_NAME).exists():
        print(f"Сессия уже есть: {work_dir / SESSION_NAME}. Удалите её вручную, если нужен новый вход.")
        return

    # sms_code_provider=None → ConsoleSmsCodeProvider: код вводится в консоли.
    client = Client(phone=args.phone, session_name=SESSION_NAME, work_dir=str(work_dir), extra_config=make_config())
    started = asyncio.Event()

    @client.on_start()
    async def _on_start(c: Client) -> None:
        started.set()

    task = asyncio.create_task(client.start())
    done, _ = await asyncio.wait({task, asyncio.create_task(started.wait())}, return_when=asyncio.FIRST_COMPLETED)
    if task in done:
        task.result()  # пробросить ошибку входа
        return
    secure_session_file(work_dir)
    me = client.me
    print("Вход выполнен. Мой id:", me.contact.id if me else None)
    print(json.dumps(contacts_summary(client), ensure_ascii=False, indent=2, default=str))
    await client.close()
    task.cancel()


async def run_listen(args: argparse.Namespace) -> None:
    work_dir = Path(args.work_dir)
    if not (work_dir / SESSION_NAME).exists():
        sys.exit("Сессии нет. Сначала: python tools/stage0_probe.py login --phone ...")
    secure_session_file(work_dir)

    out = Recorder(Path(args.out))
    client = Client(
        phone=args.phone,
        session_name=SESSION_NAME,
        work_dir=str(work_dir),
        extra_config=make_config(),
        auth_flow=RefuseAuthFlow(),
    )
    looked_up: set[int] = set()

    @client.on_start()
    async def _on_start(c: Client) -> None:
        out.write("start", {"my_id": c.me.contact.id if c.me else None, **contacts_summary(c)})

    @client.on_raw()
    async def _on_raw(frame: Any, c: Client) -> None:
        out.write("raw", {"opcode": frame.opcode, "cmd": frame.cmd, "seq": frame.seq, "payload": frame.payload})

    @client.on_message()
    async def _on_message(message: Message, c: Client) -> None:
        my_id = c.me.contact.id if c.me else None
        out.write(
            "message",
            {
                "model": message.model_dump(mode="json"),
                "is_own": message.sender is not None and message.sender == my_id,
                "chat_id_xor_check": (my_id ^ message.sender) if (my_id and message.sender) else None,
            },
        )
        if not args.lookup or message.sender is None or message.sender == my_id:
            return
        # Только чтение: профиль отправителя и тип чата. Ничего не отправляется.
        if message.sender not in looked_up:
            looked_up.add(message.sender)
            try:
                user = await c.get_user(message.sender)
                out.write("lookup_user", user.model_dump(mode="json") if user else None)
            except Exception as e:  # noqa: BLE001
                out.write("lookup_user_error", {"error": type(e).__name__, "detail": str(e)})
        if message.chat_id is not None:
            try:
                chat = await c.get_chat(message.chat_id)
                out.write("lookup_chat", {"id": chat.id, "type": str(chat.type), "status": chat.status})
            except Exception as e:  # noqa: BLE001
                out.write("lookup_chat_error", {"error": type(e).__name__, "detail": str(e)})

    @client.on_disconnect()
    async def _on_disconnect(exc: Exception, reconnect: bool, delay: float) -> None:
        out.write("disconnect", {"error": type(exc).__name__, "detail": str(exc), "reconnect": reconnect})

    # Внимание: любой зарегистрированный on_error помечает ошибку как обработанную
    # (pymax/dispatch/dispatcher.py, emit_error), и ошибка входа не пробрасывается
    # из start(), а клиент тихо закрывается. Поэтому ошибку обязательно пишем в журнал.
    @client.on_error()
    async def _on_error(exc: Exception, ctx: Any) -> None:
        out.write(
            "error",
            {"error": type(exc).__name__, "detail": str(exc)[:300], "event_type": str(getattr(ctx, "event_type", None))},
        )

    try:
        if args.minutes:
            await asyncio.wait_for(client.start(), timeout=args.minutes * 60)
        else:
            await client.start()
    except asyncio.TimeoutError:
        print("Время прослушивания истекло.")
    except Exception as e:  # noqa: BLE001
        out.write("fatal", {"error": type(e).__name__, "detail": str(e)})
        raise
    finally:
        await client.close()
        out.close()
        print(f"Журнал: {out.out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Этап 0: печать входящих событий MAX (без отправок)")
    parser.add_argument("command", choices=["login", "listen"])
    parser.add_argument("--phone", required=True, help="+7XXXXXXXXXX")
    parser.add_argument("--work-dir", default="./.session", help="каталог файла сессии (вне git)")
    parser.add_argument("--out", default="./stage0_events.jsonl", help="журнал событий (маскированный)")
    parser.add_argument("--lookup", action="store_true", help="запрашивать профиль отправителя и тип чата")
    parser.add_argument("--minutes", type=float, default=0, help="слушать N минут и выйти (0 — до Ctrl+C)")
    args = parser.parse_args()
    asyncio.run(run_login(args) if args.command == "login" else run_listen(args))


if __name__ == "__main__":
    main()
