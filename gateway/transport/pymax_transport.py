"""Транспорт личного аккаунта на PyMax (maxapi-python==2.4.1).

Все вызовы сверены с исходниками 2.4.1 и журналом этапа 0 (docs/stage0.md):
- `Client(phone, session_name, work_dir, extra_config, auth_flow, ...)`;
- `ExtraConfig.telemetry` по умолчанию True — выключаем (имитация действий пользователя);
- при отзыве токена и `relogin=True` PyMax удаляет сессию и вызывает `auth_flow` —
  `RefuseAuthFlow` вместо SMS-входа бросает исключение (SMS только в `login`);
- `on_message(message, client)`, `on_disconnect(exc, reconnect, delay)`,
  `on_error(exc, ctx)`, `on_start(client)`;
- любой зарегистрированный `on_error` помечает ошибку как обработанную, и ошибка
  входа НЕ пробрасывается из `start()` — клиент тихо закрывается. Поэтому штатный
  возврат из `start()` без `stop()` считается потерей сессии;
- id личного чата = my_id ^ user_id; чат с ботом — тоже DIALOG, бот определяется по
  `User.options` ∋ "BOT"; у каналов chat_id < 0 и `message.type == "CHANNEL"`;
- свои сообщения с телефона приходят в `on_message` с `sender == me.contact.id`.

Отметки «прочитано» (`Message.read`, `read_message`) здесь не вызываются нигде.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from pymax import Client, ExtraConfig, Message, SyncOverrides
from pymax.auth.models import AuthResult

from ..pii import normalize_phone
from .base import (
    ChatKind,
    Contact,
    IncomingMessage,
    MessageHandler,
    PersonalAccountTransport,
    SenderProfile,
    StatusEvent,
    StatusHandler,
    TransportStatus,
)

log = logging.getLogger(__name__)

SESSION_NAME = "session.db"
_CHAT_TYPES = {"DIALOG": ChatKind.DIALOG, "CHAT": ChatKind.GROUP, "CHANNEL": ChatKind.CHANNEL}


class LoginRequiredError(RuntimeError):
    pass


class RefuseAuthFlow:
    """AuthFlow демона: вместо SMS-входа — ошибка. SMS — только `python -m gateway login`."""

    async def authenticate(self, app: Any) -> AuthResult:
        raise LoginRequiredError("Сессии MAX нет или она отозвана. Нужен вход: python -m gateway login")


def json_safe(obj: Any) -> Any:
    """Слепок модели для JSON-колонки: bytes (thumbhash, previewData фото) → длина."""
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, bytes):
        return f"<bytes {len(obj)}>"
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def secure_session_dir(session_dir: Path) -> None:
    """Каталог 700, файлы сессии 600 — PyMax права сам не выставляет."""
    session_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(session_dir, 0o700)
    for path in session_dir.glob(f"{SESSION_NAME}*"):
        os.chmod(path, 0o600)


def make_extra_config(*, full_contacts_sync: bool = False) -> ExtraConfig:
    kwargs: dict[str, Any] = {
        "telemetry": False,
        "reconnect": True,
        "relogin": True,  # вместе с RefuseAuthFlow: отозванная сессия → выход, а не цикл
        "log_level": "INFO",
    }
    if full_contacts_sync:
        # -1 — начальное значение маркера у новой сессии (pymax/types/domain/sync.py).
        # Что сервер при этом отдаёт полный список контактов — НЕ проверено запуском.
        kwargs["sync"] = SyncOverrides(contacts_sync=-1)
    return ExtraConfig(**kwargs)


def message_to_incoming(message: Message, my_id: int | None) -> IncomingMessage:
    attach_types: list[str] = []
    for a in message.attaches:
        t = getattr(a, "type", None)
        attach_types.append(str(getattr(t, "value", t)))
    return IncomingMessage(
        message_id=message.id,
        chat_id=message.chat_id if message.chat_id is not None else 0,
        sender_id=message.sender,
        text=message.text or "",
        time_ms=message.time,
        msg_type=str(message.type),
        is_own=my_id is not None and message.sender == my_id,
        attachment_types=tuple(attach_types),
        raw=json_safe(message.model_dump(mode="python", by_alias=True)),
    )


def user_to_profile(user: Any) -> SenderProfile:
    options = tuple(str(o) for o in (getattr(user, "options", None) or []))
    return SenderProfile(
        user_id=user.id,
        phone=normalize_phone(getattr(user, "phone", None)),
        is_bot="BOT" in options,
        options=options,
    )


class PyMaxTransport(PersonalAccountTransport):
    name = "pymax"

    def __init__(self, phone: str, session_dir: Path) -> None:
        self._phone = phone
        self._session_dir = session_dir
        self._client: Client | None = None
        self._stopping = False
        self._chat_kind_cache: dict[int, ChatKind] = {}

    def _build_client(self, *, auth_flow: Any, full_contacts_sync: bool = False) -> Client:
        secure_session_dir(self._session_dir)
        kwargs: dict[str, Any] = {
            "phone": self._phone,
            "session_name": SESSION_NAME,
            "work_dir": str(self._session_dir),
            "extra_config": make_extra_config(full_contacts_sync=full_contacts_sync),
        }
        if auth_flow is not None:
            kwargs["auth_flow"] = auth_flow
        return Client(**kwargs)

    @property
    def my_id(self) -> int | None:
        me = self._client.me if self._client else None
        return me.contact.id if me else None

    @property
    def is_connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    async def run(self, on_message: MessageHandler, on_status: StatusHandler) -> None:
        if not (self._session_dir / SESSION_NAME).exists():
            raise LoginRequiredError("Файла сессии нет. Нужен вход: python -m gateway login")
        client = self._build_client(auth_flow=RefuseAuthFlow())
        self._client = client
        self._stopping = False

        @client.on_start()
        async def _on_start(c: Client) -> None:
            secure_session_dir(self._session_dir)
            await on_status(StatusEvent(TransportStatus.CONNECTED))

        @client.on_message()
        async def _on_message(message: Message, c: Client) -> None:
            await on_message(message_to_incoming(message, self.my_id))

        @client.on_disconnect()
        async def _on_disconnect(exc: Exception, reconnect: bool, delay: float) -> None:
            await on_status(StatusEvent(TransportStatus.DISCONNECTED, f"{type(exc).__name__}: {exc}"))

        @client.on_error()
        async def _on_error(exc: Exception, ctx: Any) -> None:
            event_type = getattr(ctx, "event_type", None)
            await on_status(StatusEvent(TransportStatus.ERROR, f"{type(exc).__name__} в {event_type}: {exc}"))

        try:
            await client.start()
        except asyncio.CancelledError:
            raise
        except LoginRequiredError as e:
            await on_status(StatusEvent(TransportStatus.SESSION_LOST, str(e)))
            raise
        except Exception as e:
            await on_status(StatusEvent(TransportStatus.ERROR, f"{type(e).__name__}: {e}"))
            raise
        finally:
            await client.close()
        if self._stopping:
            await on_status(StatusEvent(TransportStatus.STOPPED))
        else:
            # start() вернулся сам: при зарегистрированном on_error так выглядит ошибка входа.
            await on_status(StatusEvent(TransportStatus.SESSION_LOST, "клиент MAX завершился без команды остановки"))

    async def stop(self) -> None:
        self._stopping = True
        if self._client:
            await self._client.close()

    def _require_client(self) -> Client:
        if self._client is None:
            raise RuntimeError("Транспорт не запущен")
        return self._client

    async def get_sender_profile(self, user_id: int) -> SenderProfile | None:
        user = await self._require_client().get_user(user_id)
        return user_to_profile(user) if user else None

    async def get_chat_kind(self, chat_id: int) -> ChatKind:
        if chat_id < 0:
            return ChatKind.CHANNEL  # по журналу этапа 0 у каналов отрицательный chatId
        if chat_id in self._chat_kind_cache:
            return self._chat_kind_cache[chat_id]
        chat = await self._require_client().get_chat(chat_id)
        t = getattr(chat.type, "value", chat.type)
        kind = _CHAT_TYPES.get(str(t), ChatKind.UNKNOWN)
        self._chat_kind_cache[chat_id] = kind
        return kind

    async def list_contacts(self) -> list[Contact]:
        client = self._require_client()
        return [Contact(user_id=u.id, phone=normalize_phone(u.phone)) for u in client.contacts if u is not None]

    async def send_text(self, chat_id: int, text: str) -> int | None:
        # PyMax разбирает текст как markdown (pymax/infra/message.py) — шаблоны это учитывают.
        msg = await self._require_client().send_message(chat_id, text)
        return msg.id if msg else None

    async def login_interactive(self, *, full_contacts_sync: bool = False) -> list[Contact]:
        """Вход по SMS (код вводится в консоли) или по существующей сессии. Возвращает контакты.

        Вызывается только из `python -m gateway login` / `contacts-sync`, управляющим лично.
        """
        client = self._build_client(auth_flow=None, full_contacts_sync=full_contacts_sync)
        self._client = client
        started = asyncio.Event()

        @client.on_start()
        async def _on_start(c: Client) -> None:
            started.set()

        task = asyncio.create_task(client.start())
        waiter = asyncio.create_task(started.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                task.result()
                raise RuntimeError("Клиент MAX завершился до входа")
            secure_session_dir(self._session_dir)
            return await self.list_contacts()
        finally:
            waiter.cancel()
            self._stopping = True
            await client.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
