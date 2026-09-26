"""Транспорт-заглушка для тестов и локальной разработки. В сеть не ходит."""

from __future__ import annotations

import asyncio

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


class FakeTransport(PersonalAccountTransport):
    name = "fake"

    def __init__(self) -> None:
        self.profiles: dict[int, SenderProfile] = {}
        self.chat_kinds: dict[int, ChatKind] = {}
        self.contacts: list[Contact] = []
        self.sent: list[tuple[int, str]] = []
        self.fail_send: Exception | None = None
        self._on_message: MessageHandler | None = None
        self._on_status: StatusHandler | None = None
        self._stopped = asyncio.Event()
        self._connected = False

    async def run(self, on_message: MessageHandler, on_status: StatusHandler) -> None:
        self._on_message, self._on_status = on_message, on_status
        self._connected = True
        await on_status(StatusEvent(TransportStatus.CONNECTED))
        await self._stopped.wait()

    async def stop(self) -> None:
        self._connected = False
        self._stopped.set()

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def get_sender_profile(self, user_id: int) -> SenderProfile | None:
        return self.profiles.get(user_id)

    async def get_chat_kind(self, chat_id: int) -> ChatKind:
        if chat_id < 0:
            return ChatKind.CHANNEL
        return self.chat_kinds.get(chat_id, ChatKind.UNKNOWN)

    async def list_contacts(self) -> list[Contact]:
        return list(self.contacts)

    async def send_text(self, chat_id: int, text: str) -> int | None:
        if self.fail_send:
            raise self.fail_send
        self.sent.append((chat_id, text))
        return len(self.sent)

    # --- для тестов ---
    async def emit_message(self, msg: IncomingMessage) -> None:
        assert self._on_message is not None, "run() ещё не вызван"
        await self._on_message(msg)

    async def emit_status(self, status: TransportStatus, detail: str = "") -> None:
        assert self._on_status is not None, "run() ещё не вызван"
        if status is not TransportStatus.CONNECTED:
            self._connected = False
        await self._on_status(StatusEvent(status, detail))
