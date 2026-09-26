"""Логика обработки событий личного аккаунта.

Этап 1: только журнал. Сценарий S1 (сопоставление, токены, автоответ) — этап 2.
"""

from __future__ import annotations

import logging

from .db import Database
from .journal import record_incoming
from .transport.base import IncomingMessage

log = logging.getLogger(__name__)


class Core:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def handle_incoming(self, msg: IncomingMessage) -> None:
        # Сначала журнал: если обработка упадёт, сообщение не потеряется.
        is_new = await record_incoming(self.db, msg)
        if not is_new:
            log.info("Повторное событие о сообщении %s в чате %s — пропуск", msg.message_id, msg.chat_id)
            return
        log.info(
            "Входящее: чат %s, отправитель %s, тип %s, своё=%s, вложения=%s",
            msg.chat_id,
            msg.sender_id,
            msg.msg_type,
            msg.is_own,
            ",".join(msg.attachment_types) or "-",
        )
        # Этап 2: фильтры (личный чат, не бот, не своё), белый список, поиск в debtor_phones,
        # cooldown/лимиты через SendGuard, выпуск токена, автоответ.
