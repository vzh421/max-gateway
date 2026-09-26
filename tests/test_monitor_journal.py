"""Мониторинг (правило 9), журнал и идемпотентность, цикл Core ↔ транспорт."""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from gateway.core import Core
from gateway.db.models import MessageLog
from gateway.journal import record_incoming
from gateway.monitor import Monitor
from gateway.safety import STATE_SENDS_BLOCKED, SendStatus
from gateway.transport.base import StatusEvent, TransportStatus
from gateway.transport.fake import FakeTransport

from .helpers import incoming, seed_incoming


class RecNotifier:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def notify(self, text: str) -> None:
        self.texts.append(text)


async def test_disconnect_blocks_sends_and_notifies(db, transport, guard_factory) -> None:
    notifier = RecNotifier()
    monitor = Monitor(db, notifier)
    await seed_incoming(db, 100)
    await monitor.on_status(StatusEvent(TransportStatus.DISCONNECTED, "ConnectionResetError"))
    assert await db.get_state(STATE_SENDS_BLOCKED)
    assert len(notifier.texts) == 1
    res = await guard_factory(dry_run=False).send(100, "текст")
    assert res.status is SendStatus.BLOCKED_MONITOR
    assert transport.sent == []


async def test_notification_not_repeated_while_blocked(db) -> None:
    notifier = RecNotifier()
    monitor = Monitor(db, notifier)
    for status in (TransportStatus.DISCONNECTED, TransportStatus.ERROR, TransportStatus.SESSION_LOST):
        await monitor.on_status(StatusEvent(status, "x"))
    assert len(notifier.texts) == 1


async def test_reconnect_does_not_unblock(db) -> None:
    monitor = Monitor(db, RecNotifier())
    await monitor.on_status(StatusEvent(TransportStatus.SESSION_LOST, "FAIL_LOGIN_TOKEN"))
    await monitor.on_status(StatusEvent(TransportStatus.CONNECTED))
    assert await db.get_state(STATE_SENDS_BLOCKED)
    await monitor.resume_sends()
    assert await db.get_state(STATE_SENDS_BLOCKED) is None


async def test_notification_masks_pii(db) -> None:
    notifier = RecNotifier()
    await Monitor(db, notifier).on_status(StatusEvent(TransportStatus.ERROR, "ошибка для +7 916 123-45-67"))
    assert "123-45-67" not in notifier.texts[0] and "9161234567" not in notifier.texts[0]


async def test_record_incoming_idempotent(db) -> None:
    msg = incoming()
    assert await record_incoming(db, msg) is True
    assert await record_incoming(db, msg) is False
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(MessageLog)) == 1


async def test_core_via_transport_logs_everything_once(db, transport) -> None:
    core = Core(db)
    monitor = Monitor(db, RecNotifier())
    task = asyncio.create_task(transport.run(core.handle_incoming, monitor.on_status))
    await asyncio.sleep(0)
    group_msg = incoming(chat_id=-68089406059881, sender_id=None, msg_type="CHANNEL")
    own_msg = incoming(sender_id=3911587, is_own=True)
    dup = incoming()
    for m in (group_msg, own_msg, dup, dup):
        await transport.emit_message(m)
    await transport.stop()
    await task
    async with db.session() as s:
        count = await s.scalar(select(func.count()).select_from(MessageLog).where(MessageLog.direction == "in"))
        outgoing = await s.scalar(select(func.count()).select_from(MessageLog).where(MessageLog.direction == "out"))
    assert count == 3  # повтор dup не записан второй раз
    assert outgoing == 0  # этап 1: никаких ответов
    assert transport.sent == []


async def test_transport_crash_blocks_sends_without_exit(db) -> None:
    from gateway.app import _run_transport
    from gateway.transport.pymax_transport import LoginRequiredError

    class Broken(FakeTransport):
        async def run(self, on_message, on_status):  # type: ignore[override]
            raise LoginRequiredError("нет сессии")

    notifier = RecNotifier()
    monitor = Monitor(db, notifier)
    await _run_transport(Broken(), Core(db), monitor)  # не бросает исключение наружу
    state = await db.get_state(STATE_SENDS_BLOCKED)
    assert state and state["reason"].startswith("session_lost")
    assert len(notifier.texts) == 1
