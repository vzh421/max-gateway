"""Защитные правила 1, 5, 6, 7, 8 и SendGuard целиком."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from gateway.config import PLACEHOLDER_MARK, Settings
from gateway.db.models import MessageLog
from gateway.safety import STATE_SENDS_BLOCKED, SendStatus

from .helpers import age_out_outgoing, seed_incoming

TEXT = "Ссылка на бота: https://max.ru/bot?start=abc"


# --- конфигурация по умолчанию ---

def test_defaults_are_safe() -> None:
    s = Settings(_env_file=None)
    assert s.dry_run is True
    assert s.reply_to_unknown is False
    assert s.mark_read is False
    assert s.kill_switch is False
    assert s.redirect_min_interval_hours == 24
    assert s.redirect_max_reminders == 3
    assert s.redirect_window_days == 30
    assert s.max_replies_per_hour == 10
    assert s.delay_range == (20.0, 90.0)
    assert s.token_ttl_days == 14


def test_mark_read_true_is_rejected() -> None:
    with pytest.raises(ValidationError, match="MARK_READ"):
        Settings(_env_file=None, mark_read=True)


def test_debug_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError, match="DEBUG"):
        Settings(_env_file=None, log_level="debug")


def test_live_mode_only_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    assert Settings(_env_file=None).dry_run is False


def test_transport_interface_has_no_mark_read() -> None:
    from gateway.transport.base import PersonalAccountTransport

    names = {n.lower() for n in dir(PersonalAccountTransport)}
    assert not names & {"read", "mark_read", "mark_as_read", "read_message", "read_chat"}


# --- dry-run ---

async def test_dry_run_sends_nothing(db, transport, guard_factory, sleeper) -> None:
    await seed_incoming(db, 100)
    res = await guard_factory().send(100, TEXT)
    assert res.status is SendStatus.DRY_RUN
    assert transport.sent == []
    assert sleeper.calls == []
    async with db.session() as s:
        row = await s.get(MessageLog, res.log_id)
    assert row.dry_run is True and row.status == "dry_run" and row.direction == "out"


async def test_live_send_with_delay(db, transport, guard_factory, sleeper) -> None:
    await seed_incoming(db, 100)
    res = await guard_factory(dry_run=False).send(100, TEXT)
    assert res.status is SendStatus.SENT
    assert transport.sent == [(100, TEXT)]
    assert len(sleeper.calls) == 1 and 20 <= sleeper.calls[0] <= 90


# --- никаких рассылок первым ---

@pytest.mark.parametrize("dry_run", [True, False])
async def test_never_writes_first(db, transport, guard_factory, dry_run) -> None:
    res = await guard_factory(dry_run=dry_run).send(100, TEXT)
    assert res.status is SendStatus.BLOCKED_NOT_INITIATED
    assert transport.sent == []


# --- kill switch ---

async def test_kill_switch_env(db, transport, guard_factory) -> None:
    await seed_incoming(db, 100)
    res = await guard_factory(dry_run=False, kill_switch=True).send(100, TEXT)
    assert res.status is SendStatus.BLOCKED_KILL_SWITCH
    assert transport.sent == []


async def test_kill_switch_file(db, transport, guard_factory, tmp_path: Path) -> None:
    await seed_incoming(db, 100)
    (tmp_path / "KILL_SWITCH").write_text("стоп")
    res = await guard_factory(dry_run=False).send(100, TEXT)
    assert res.status is SendStatus.BLOCKED_KILL_SWITCH
    assert transport.sent == []


async def test_kill_switch_during_delay(db, transport, guard_factory, sleeper, tmp_path: Path) -> None:
    await seed_incoming(db, 100)
    guard = guard_factory(dry_run=False)

    async def flip() -> None:
        guard.kill_switch.enable("во время задержки")

    sleeper.hook = flip
    res = await guard.send(100, TEXT)
    assert res.status is SendStatus.BLOCKED_KILL_SWITCH
    assert transport.sent == []


async def test_kill_switch_blocks_dry_run_too(db, guard_factory, tmp_path: Path) -> None:
    await seed_incoming(db, 100)
    (tmp_path / "KILL_SWITCH").touch()
    res = await guard_factory().send(100, TEXT)
    assert res.status is SendStatus.BLOCKED_KILL_SWITCH


# --- блокировка мониторингом ---

async def test_monitor_block(db, transport, guard_factory) -> None:
    await seed_incoming(db, 100)
    await db.set_state(STATE_SENDS_BLOCKED, {"reason": "disconnected", "at": "x"})
    res = await guard_factory(dry_run=False).send(100, TEXT)
    assert res.status is SendStatus.BLOCKED_MONITOR
    assert transport.sent == []


async def test_monitor_block_during_delay(db, transport, guard_factory, sleeper) -> None:
    await seed_incoming(db, 100)

    async def drop() -> None:
        await db.set_state(STATE_SENDS_BLOCKED, {"reason": "session_lost", "at": "x"})

    sleeper.hook = drop
    res = await guard_factory(dry_run=False).send(100, TEXT)
    assert res.status is SendStatus.BLOCKED_MONITOR
    assert transport.sent == []


# --- неутверждённый текст ---

async def test_placeholder_blocked_in_live(db, transport, guard_factory) -> None:
    await seed_incoming(db, 100)
    res = await guard_factory(dry_run=False).send(100, f"{PLACEHOLDER_MARK} текст")
    assert res.status is SendStatus.BLOCKED_PLACEHOLDER
    assert transport.sent == []


async def test_placeholder_allowed_in_dry_run(db, guard_factory) -> None:
    await seed_incoming(db, 100)
    res = await guard_factory().send(100, f"{PLACEHOLDER_MARK} текст")
    assert res.status is SendStatus.DRY_RUN


# --- перенаправления: не чаще раза в сутки, не больше 1 + 3 за окно ---

async def _age_out(db, hours: float) -> None:
    await age_out_outgoing(db, hours)


async def test_min_interval_per_chat(db, transport, guard_factory) -> None:
    await seed_incoming(db, 100)
    guard = guard_factory(dry_run=False)
    assert (await guard.send(100, TEXT)).status is SendStatus.SENT
    assert (await guard.send(100, TEXT)).status is SendStatus.BLOCKED_COOLDOWN
    await _age_out(db, 23)
    assert (await guard.send(100, TEXT)).status is SendStatus.BLOCKED_COOLDOWN
    await _age_out(db, 1.5)
    assert (await guard.send(100, TEXT)).status is SendStatus.SENT
    assert len(transport.sent) == 2


async def test_chat_limit_first_plus_three(db, transport, guard_factory) -> None:
    await seed_incoming(db, 100)
    guard = guard_factory(dry_run=False)
    statuses = []
    for _ in range(5):
        statuses.append((await guard.send(100, TEXT)).status)
        await _age_out(db, 25)
    assert statuses == [SendStatus.SENT] * 4 + [SendStatus.BLOCKED_CHAT_LIMIT]
    assert len(transport.sent) == 4


async def test_chat_limit_window_restarts(db, transport, guard_factory) -> None:
    await seed_incoming(db, 100)
    guard = guard_factory(dry_run=False, redirect_max_reminders=0)
    assert (await guard.send(100, TEXT)).status is SendStatus.SENT
    await _age_out(db, 25)
    assert (await guard.send(100, TEXT)).status is SendStatus.BLOCKED_CHAT_LIMIT
    await _age_out(db, 24 * 30)
    assert (await guard.send(100, TEXT)).status is SendStatus.SENT


async def test_cancel_during_delay_frees_slot(db, transport, guard_factory, sleeper) -> None:
    await seed_incoming(db, 100)
    guard = guard_factory(dry_run=False)

    async def cancel() -> None:
        raise asyncio.CancelledError

    sleeper.hook = cancel
    with pytest.raises(asyncio.CancelledError):
        await guard.send(100, TEXT)
    sleeper.hook = None
    assert transport.sent == []
    assert (await guard.send(100, TEXT)).status is SendStatus.SENT


async def test_stale_pending_counts_as_sent(db, transport, guard_factory) -> None:
    await seed_incoming(db, 100)
    async with db.session() as s, s.begin():
        s.add(MessageLog(channel="personal", direction="out", chat_id=100, status="pending", dry_run=False))
    guard = guard_factory(dry_run=False)
    assert await guard.cleanup_stale_pending() == 1
    assert (await guard.send(100, TEXT)).status is SendStatus.BLOCKED_COOLDOWN
    assert transport.sent == []


async def test_dry_run_history_does_not_block_live(db, transport, guard_factory) -> None:
    await seed_incoming(db, 100)
    assert (await guard_factory().send(100, TEXT)).status is SendStatus.DRY_RUN
    assert (await guard_factory(dry_run=False).send(100, TEXT)).status is SendStatus.SENT


async def test_failed_send_does_not_count(db, transport, guard_factory) -> None:
    await seed_incoming(db, 100)
    guard = guard_factory(dry_run=False)
    transport.fail_send = ConnectionError("нет сети")
    assert (await guard.send(100, TEXT)).status is SendStatus.FAILED
    transport.fail_send = None
    assert (await guard.send(100, TEXT)).status is SendStatus.SENT


# --- лимит в час ---

async def test_rate_limit(db, transport, guard_factory) -> None:
    guard = guard_factory(dry_run=False, max_replies_per_hour=3)
    for chat in range(1, 5):
        await seed_incoming(db, chat)
    results = [await guard.send(chat, TEXT) for chat in range(1, 5)]
    assert [r.status for r in results] == [SendStatus.SENT] * 3 + [SendStatus.BLOCKED_RATE_LIMIT]
    assert len(transport.sent) == 3


async def test_rate_limit_window_slides(db, transport, guard_factory) -> None:
    guard = guard_factory(dry_run=False, max_replies_per_hour=1)
    await seed_incoming(db, 1)
    await seed_incoming(db, 2)
    await guard.send(1, TEXT)
    await _age_out(db, 61 / 60)
    assert (await guard.send(2, TEXT)).status is SendStatus.SENT


async def test_rate_limit_zero_means_no_replies(db, transport, guard_factory) -> None:
    await seed_incoming(db, 1)
    res = await guard_factory(dry_run=False, max_replies_per_hour=0).send(1, TEXT)
    assert res.status is SendStatus.BLOCKED_RATE_LIMIT


async def test_concurrent_sends_same_chat_only_one(db, transport, guard_factory, sleeper) -> None:
    await seed_incoming(db, 100)
    guard = guard_factory(dry_run=False)

    async def yield_control() -> None:
        await asyncio.sleep(0)

    sleeper.hook = yield_control
    results = await asyncio.gather(*(guard.send(100, TEXT) for _ in range(5)))
    statuses = sorted(r.status.value for r in results)
    assert statuses.count("sent") == 1
    assert statuses.count("blocked:cooldown") == 4
    assert len(transport.sent) == 1


async def test_every_attempt_is_logged(db, guard_factory) -> None:
    await seed_incoming(db, 100)
    guard = guard_factory()
    await guard.send(100, TEXT)
    await guard.send(100, TEXT)
    async with db.session() as s:
        rows = (await s.execute(select(MessageLog.status).where(MessageLog.direction == "out"))).scalars().all()
    assert sorted(rows) == ["blocked:cooldown", "dry_run"]


def test_env_example_loads(tmp_path: Path) -> None:
    """.env.example как есть (пустые BOT_USERNAME/ADMIN_TOKEN и т.п.) — валидный безопасный конфиг."""
    env = Path(__file__).resolve().parent.parent / ".env.example"
    s = Settings(_env_file=str(env))
    assert s.dry_run is True and s.bot_username is None
    assert PLACEHOLDER_MARK in s.redirect_first_template and "{link}" in s.redirect_reminder_template


def test_bad_bot_username_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, bot_username="bad name/")
    assert Settings(_env_file=None, bot_username="@id123_bot").bot_username == "id123_bot"
