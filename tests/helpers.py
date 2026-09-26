from __future__ import annotations

import itertools

from gateway.db import Database
from gateway.journal import record_incoming
from gateway.transport.base import IncomingMessage

_ids = itertools.count(117336221156998923)


def incoming(chat_id: int = 116679694, sender_id: int | None = 114281389, **kw) -> IncomingMessage:
    data = {
        "message_id": next(_ids),
        "chat_id": chat_id,
        "sender_id": sender_id,
        "text": "Здравствуйте",
        "time_ms": 1790408648025,
        "msg_type": "USER",
        "is_own": False,
    }
    data.update(kw)
    return IncomingMessage(**data)


async def seed_incoming(db: Database, chat_id: int) -> None:
    """Чат написал нам первым — без этого SendGuard не отправит."""
    await record_incoming(db, incoming(chat_id=chat_id))
