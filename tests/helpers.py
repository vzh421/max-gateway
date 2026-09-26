from __future__ import annotations

import itertools
from datetime import timedelta

from sqlalchemy import select

from gateway.db import Database
from gateway.db.models import MessageLog
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


class RecNotifier:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, bool]] = []

    @property
    def texts(self) -> list[str]:
        return [t for t, _, _ in self.items]

    def kinds(self) -> list[str]:
        return [k for _, k, _ in self.items]

    async def notify(self, text: str, *, kind: str = "info", urgent: bool = False) -> None:
        self.items.append((text, kind, urgent))


async def shift_time(db: Database, model, column: str, delta: timedelta, *where) -> None:
    """Сдвиг времени на стороне Python: арифметика дат в SQL на SQLite некорректна."""
    async with db.session() as s, s.begin():
        for row in (await s.execute(select(model).where(*where))).scalars():
            setattr(row, column, getattr(row, column) + delta)


async def age_out_outgoing(db: Database, hours: float) -> None:
    await shift_time(db, MessageLog, "created_at", -timedelta(hours=hours), MessageLog.direction == "out")
