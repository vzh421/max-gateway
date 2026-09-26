"""Резервный транспорт через Green API — заглушка.

Реализовать, если свой шлюз (PyMax) сломается или будет заблокирован. API Green API
для MAX не сверялся — методы не описаны намеренно.
"""

from __future__ import annotations

from .base import ChatKind, Contact, MessageHandler, PersonalAccountTransport, SenderProfile, StatusHandler


class GreenApiTransport(PersonalAccountTransport):
    name = "green_api"

    def _todo(self) -> NotImplementedError:
        return NotImplementedError("GreenApiTransport не реализован (резерв, см. CLAUDE.md)")

    async def run(self, on_message: MessageHandler, on_status: StatusHandler) -> None:
        raise self._todo()

    async def stop(self) -> None:
        return None

    @property
    def is_connected(self) -> bool:
        return False

    async def get_sender_profile(self, user_id: int) -> SenderProfile | None:
        raise self._todo()

    async def get_chat_kind(self, chat_id: int) -> ChatKind:
        raise self._todo()

    async def list_contacts(self) -> list[Contact]:
        raise self._todo()

    async def send_text(self, chat_id: int, text: str) -> int | None:
        raise self._todo()
