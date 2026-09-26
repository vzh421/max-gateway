"""Сценарий S1: учёт собеседника, распознавание должника, перенаправление в бот."""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest
from sqlalchemy import select, update

from gateway.core import Core
from gateway.db.models import LinkToken, MessageLog, PersonalChat
from gateway.importers import add_whitelist
from gateway.safety import KillSwitch, SendGuard
from gateway.transport.base import ChatKind, SenderProfile
from gateway.transport.fake import FakeTransport

from .conftest import make_settings
from .helpers import RecNotifier, age_out_outgoing, incoming

CHAT, USER, PHONE, CASE = 116679694, 114281389, "79161234567", "A40-1234/2025"
LINK_RE = re.compile(r"https://max\.ru/test_bot\?start=([0-9a-f]{32})")


class Env:
    def __init__(self, db, tmp_path: Path, **overrides) -> None:
        values = {
            "bot_username": "test_bot",
            "dry_run": False,
            "redirect_first_template": "Пишите в бот: {link}",
            "redirect_reminder_template": "Напоминаем про бот: {link}",
        }
        values.update(overrides)
        self.settings = make_settings(tmp_path, **values)
        self.db = db
        self.transport = FakeTransport()
        self.transport.chat_kinds[CHAT] = ChatKind.DIALOG
        self.transport.profiles[USER] = SenderProfile(USER, PHONE, is_bot=False)
        self.notifier = RecNotifier()
        ks = KillSwitch(self.settings.kill_switch, self.settings.kill_switch_file)

        async def no_sleep(_: float) -> None:
            return None

        self.guard = SendGuard(self.settings, db, self.transport, ks, sleep=no_sleep, rng=random.Random(1))
        self.core = Core(self.settings, db, self.transport, self.guard, self.notifier)

    async def debtor(self, phone: str = PHONE, case_id: str = CASE) -> None:
        from gateway.db import utcnow
        from gateway.db.models import DebtorPhone

        async with self.db.session() as s, s.begin():
            s.add(DebtorPhone(case_id=case_id, phone=phone, source="test", created_at=utcnow()))

    async def msg(self, **kw):
        decision = await self.core.process(incoming(chat_id=kw.pop("chat_id", CHAT), sender_id=kw.pop("sender_id", USER), **kw))
        await self.core.drain()
        return decision

    async def age_out(self, hours: float) -> None:
        await age_out_outgoing(self.db, hours)


@pytest.fixture
def env(db, tmp_path: Path):
    def factory(**overrides) -> Env:
        return Env(db, tmp_path, **overrides)

    return factory


async def test_debtor_first_message_gets_link(env) -> None:
    e = env()
    await e.debtor()
    d = await e.msg()
    assert d.action == "redirect_first" and d.case_ids == (CASE,)
    assert len(e.transport.sent) == 1
    chat_id, text = e.transport.sent[0]
    assert chat_id == CHAT and text.startswith("Пишите в бот: ")
    token = LINK_RE.search(text).group(1)
    async with e.db.session() as s:
        row = await s.get(LinkToken, token)
        chat = await s.get(PersonalChat, CHAT)
    assert row.case_id == CASE and row.personal_chat_id == CHAT and row.used_at is None
    assert chat.phone == PHONE and chat.case_id == CASE and chat.max_user_id == USER


async def test_reminders_daily_max_three_then_silence(env) -> None:
    e = env()
    await e.debtor()
    actions = [(await e.msg()).action]
    actions.append((await e.msg()).action)  # в тот же день — тишина
    for _ in range(4):
        await e.age_out(25)
        actions.append((await e.msg()).action)
    assert actions == [
        "redirect_first", "silent:interval",
        "redirect_reminder", "redirect_reminder", "redirect_reminder", "silent:limit",
    ]
    texts = [t for _, t in e.transport.sent]
    assert len(texts) == 4
    assert texts[0].startswith("Пишите в бот") and all(t.startswith("Напоминаем") for t in texts[1:])
    # во всех сообщениях одна и та же ссылка
    assert len({LINK_RE.search(t).group(1) for t in texts}) == 1
    # об исчерпании лимита — одно уведомление, повторные сообщения не шумят
    await e.age_out(25)
    assert (await e.msg()).action == "silent:limit"
    assert e.notifier.kinds().count("redirect_limit") == 1


async def test_duplicate_event_no_second_reply(env) -> None:
    e = env()
    await e.debtor()
    m = incoming(chat_id=CHAT, sender_id=USER)
    assert (await e.core.process(m)).action == "redirect_first"
    await e.age_out(25)
    assert (await e.core.process(m)).action == "duplicate"
    await e.core.drain()
    assert len(e.transport.sent) == 1


@pytest.mark.parametrize(
    ("kw", "setup", "expected"),
    [
        ({"sender_id": 3911587, "is_own": True}, None, "skip:own"),
        ({"chat_id": -68089406059881, "sender_id": None, "msg_type": "CHANNEL"}, None, "skip:not_user_message"),
        ({"chat_id": 555}, "group", "skip:not_dialog"),
        ({"sender_id": 543835}, "bot", "skip:bot"),
    ],
)
async def test_filters(env, kw, setup, expected) -> None:
    e = env()
    await e.debtor()
    if setup == "group":
        e.transport.chat_kinds[555] = ChatKind.GROUP
    if setup == "bot":
        e.transport.profiles[543835] = SenderProfile(543835, None, is_bot=True, options=("BOT",))
    assert (await e.msg(**kw)).action == expected
    assert e.transport.sent == []


async def test_unknown_number_notified_once_no_reply(env) -> None:
    e = env()
    assert (await e.msg()).action == "unknown"
    await e.age_out(25)
    assert (await e.msg()).action == "unknown"
    assert e.transport.sent == []
    assert e.notifier.kinds() == ["unknown"]
    assert PHONE not in e.notifier.texts[0]  # маскировано
    async with e.db.session() as s:
        chat = await s.get(PersonalChat, CHAT)
    assert chat.phone == PHONE and chat.case_id is None  # собеседник учтён


async def test_hidden_phone_is_unknown(env) -> None:
    e = env()
    await e.debtor()
    e.transport.profiles.clear()  # MAX не отдал профиль/телефон
    assert (await e.msg()).action == "unknown"
    assert e.transport.sent == []


async def test_whitelist_debtor_match_notifies_without_reply(env) -> None:
    e = env()
    await e.debtor()
    await add_whitelist(e.db, phone=PHONE, source="address_book")
    assert (await e.msg()).action == "whitelist"
    assert e.transport.sent == []
    assert e.notifier.kinds() == ["whitelist_match"]


async def test_whitelist_by_user_id(env) -> None:
    e = env()
    await e.debtor()
    await add_whitelist(e.db, max_user_id=USER, source="address_book")
    assert (await e.msg()).action == "whitelist"
    assert e.transport.sent == []


async def test_several_cases_token_with_candidates(env) -> None:
    e = env()
    await e.debtor(case_id="A-2")
    await e.debtor(case_id="A-1")
    d = await e.msg()
    assert d.action == "redirect_first" and d.case_ids == ("A-1", "A-2")
    token = LINK_RE.search(e.transport.sent[0][1]).group(1)
    async with e.db.session() as s:
        row = await s.get(LinkToken, token)
    assert row.case_id is None and row.candidate_case_ids == ["A-1", "A-2"]


async def test_dry_run_decides_but_sends_nothing(env) -> None:
    e = env(dry_run=True)
    await e.debtor()
    assert (await e.msg()).action == "redirect_first"
    assert (await e.msg()).action == "silent:interval"
    assert e.transport.sent == []
    async with e.db.session() as s:
        rows = (await s.execute(select(MessageLog).where(MessageLog.direction == "out"))).scalars().all()
    assert [(r.status, r.dry_run, r.kind) for r in rows] == [("dry_run", True, "redirect_first")]


async def test_used_token_reminder_without_payload(env) -> None:
    e = env()
    await e.debtor()
    await e.msg()
    async with e.db.session() as s, s.begin():
        await s.execute(update(LinkToken).values(used_at=LinkToken.created_at, used_by_bot_user_id=1))
    await e.age_out(25)
    assert (await e.msg()).action == "redirect_reminder"
    assert e.transport.sent[-1][1] == "Напоминаем про бот: https://max.ru/test_bot"


async def test_reply_to_unknown_true_redirects_without_case(env) -> None:
    e = env(reply_to_unknown=True)
    d = await e.msg()
    assert d.action == "redirect_first" and d.case_ids == ()
    token = LINK_RE.search(e.transport.sent[0][1]).group(1)
    async with e.db.session() as s:
        row = await s.get(LinkToken, token)
    assert row.case_id is None and row.candidate_case_ids is None


async def test_no_bot_username_no_reply(env) -> None:
    e = env(bot_username=None)
    await e.debtor()
    assert (await e.msg()).action == "silent:no_bot_username"
    assert e.transport.sent == [] and "config" in e.notifier.kinds()


async def test_placeholder_template_never_sent_live(env) -> None:
    e = env(redirect_first_template="[НЕ УТВЕРЖДЕНО] {link}")
    await e.debtor()
    assert (await e.msg()).action == "redirect_first"  # решение принято…
    assert e.transport.sent == []  # …но SendGuard не выпустил неутверждённый текст


async def test_processing_error_is_contained(env) -> None:
    e = env()
    await e.debtor()

    async def boom(_: int):
        raise TimeoutError("MAX не ответил")

    e.transport.get_chat_kind = boom  # type: ignore[method-assign]
    await e.core.handle_incoming(incoming(chat_id=CHAT, sender_id=USER))  # не бросает
    assert e.transport.sent == [] and "error" in e.notifier.kinds()
    async with e.db.session() as s:
        assert (await s.execute(select(MessageLog).where(MessageLog.direction == "in"))).scalars().first()


def test_fake_transport_is_default_link_format() -> None:
    assert FakeTransport().format_link("https://max.ru/x") == "https://max.ru/x"
