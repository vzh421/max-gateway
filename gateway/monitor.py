"""Мониторинг соединения с MAX (правило 9) и уведомления управляющему.

Любой статус, кроме CONNECTED/STOPPED, блокирует отправки с личного аккаунта.
Блокировка хранится в БД и НЕ снимается автоматически при переподключении:
управляющий снимает её сам (`python -m gateway sends resume` или POST /admin/sends/resume).
Приём сообщений при этом продолжается.
"""

from __future__ import annotations

import logging

from .db import Database, utcnow
from .notify import Notifier
from .pii import mask_text
from .safety import STATE_SENDS_BLOCKED
from .transport.base import StatusEvent, TransportStatus

log = logging.getLogger(__name__)

_BLOCKING = {TransportStatus.DISCONNECTED, TransportStatus.SESSION_LOST, TransportStatus.ERROR}


class Monitor:
    def __init__(self, db: Database, notifier: Notifier) -> None:
        self.db = db
        self.notifier = notifier
        self.last_status: StatusEvent | None = None

    async def on_status(self, event: StatusEvent) -> None:
        self.last_status = event
        detail = mask_text(event.detail)[:500]
        if event.status in _BLOCKING:
            already = await self.db.get_state(STATE_SENDS_BLOCKED)
            await self.db.set_state(
                STATE_SENDS_BLOCKED,
                {"reason": f"{event.status.value}: {detail}", "at": utcnow().isoformat()},
            )
            log.error("MAX: %s %s — отправки остановлены", event.status.value, detail)
            if not already:
                await self.notifier.notify(
                    f"Шлюз MAX: {event.status.value}. Отправки с личного аккаунта остановлены. "
                    f"Причина: {detail}. Приём продолжается. Снять блокировку: python -m gateway sends resume",
                    kind="monitor",
                    urgent=True,
                )
        elif event.status is TransportStatus.CONNECTED:
            log.info("MAX: соединение установлено")
        else:
            log.info("MAX: %s %s", event.status.value, detail)

    async def resume_sends(self) -> None:
        await self.db.set_state(STATE_SENDS_BLOCKED, None)
        log.warning("Блокировка отправок снята вручную")
