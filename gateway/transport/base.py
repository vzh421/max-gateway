"""Интерфейс транспорта личного аккаунта.

core не знает, как подключён личный аккаунт: PyMax (свой шлюз) или Green API (резерв).
Интерфейс не содержит отметки «прочитано» — правило 7 CLAUDE.md.

ВАЖНО: `send_text` вызывает только `gateway.safety.SendGuard`. Любая отправка с личного
аккаунта мимо SendGuard обходит dry-run, kill switch и лимиты.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ChatKind(StrEnum):
    DIALOG = "dialog"  # личный чат (в т.ч. с ботом — см. SenderProfile.is_bot)
    GROUP = "group"
    CHANNEL = "channel"
    UNKNOWN = "unknown"


class TransportStatus(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"  # сеть упала, возможно переподключение
    SESSION_LOST = "session_lost"  # сессия отозвана/недействительна — нужен login
    ERROR = "error"  # неожиданная ошибка библиотеки
    STOPPED = "stopped"  # штатная остановка


@dataclass(frozen=True)
class IncomingMessage:
    message_id: int
    chat_id: int
    sender_id: int | None
    text: str
    time_ms: int
    msg_type: str  # как прислал MAX: USER / CHANNEL / …
    is_own: bool  # отправлено самим управляющим (с телефона)
    attachment_types: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)  # JSON-совместимый слепок события


@dataclass(frozen=True)
class SenderProfile:
    user_id: int
    phone: str | None  # 7XXXXXXXXXX или None, если MAX не отдал
    is_bot: bool
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Contact:
    user_id: int
    phone: str | None


@dataclass(frozen=True)
class StatusEvent:
    status: TransportStatus
    detail: str = ""


MessageHandler = Callable[[IncomingMessage], Awaitable[None]]
StatusHandler = Callable[[StatusEvent], Awaitable[None]]


class PersonalAccountTransport(abc.ABC):
    name: str = "abstract"

    @abc.abstractmethod
    async def run(self, on_message: MessageHandler, on_status: StatusHandler) -> None:
        """Подключиться и слушать события до остановки. Статусы — через on_status."""

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool: ...

    @abc.abstractmethod
    async def get_sender_profile(self, user_id: int) -> SenderProfile | None: ...

    @abc.abstractmethod
    async def get_chat_kind(self, chat_id: int) -> ChatKind: ...

    @abc.abstractmethod
    async def list_contacts(self) -> list[Contact]:
        """Контакты адресной книги аккаунта, известные транспорту на данный момент."""

    @abc.abstractmethod
    async def send_text(self, chat_id: int, text: str) -> int | None:
        """Отправить текст. Только через SendGuard! Возвращает id сообщения, если известен."""
