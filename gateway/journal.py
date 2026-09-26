"""Журнал входящих (message_log). Запись — до любой обработки события."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from .db import Database, utcnow
from .db.models import MessageLog
from .transport.base import IncomingMessage


async def record_incoming(db: Database, msg: IncomingMessage, *, channel: str = "personal") -> bool:
    """Сохраняет входящее. False — такое событие уже было (повторная доставка)."""
    try:
        async with db.session() as s, s.begin():
            s.add(
                MessageLog(
                    created_at=utcnow(),
                    channel=channel,
                    direction="in",
                    chat_id=msg.chat_id,
                    external_message_id=msg.message_id,
                    sender_id=msg.sender_id,
                    text=msg.text,
                    attachments=list(msg.attachment_types) or None,
                    dry_run=False,
                    raw=msg.raw or None,
                )
            )
    except IntegrityError:
        return False
    return True
