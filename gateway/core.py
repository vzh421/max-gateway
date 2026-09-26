"""Сценарий S1: должник пишет в личный MAX → учёт → перенаправление в бот.

Порядок для каждого входящего:
  1. журнал (всегда, до любой обработки; повтор события — выход);
  2. фильтры: не своё, не канал, личный чат (DIALOG), отправитель не бот (правило 4);
  3. учёт собеседника в personal_chats (телефон, дело, первое/последнее обращение);
  4. белый список → стоп (если номер совпал с должником — уведомить управляющего; правило 3);
  5. поиск в debtor_phones: не найден → уведомление, без ответа (REPLY_TO_UNKNOWN=false);
  6. перенаправление: первое сообщение со ссылкой, затем до REDIRECT_MAX_REMINDERS мягких
     напоминаний не чаще раза в REDIRECT_MIN_INTERVAL_HOURS; дальше — тишина и одно
     уведомление управляющему. Отправка — только через SendGuard, в фоне (задержка 20–90 с
     не задерживает приём следующих событий).

Бот (привязка, согласие на ПДн, ответы) — в отдельной сессии; здесь только ссылка.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from .config import Settings
from .db import Database, utcnow
from .db.models import DebtorPhone, PersonalChat
from .importers import is_whitelisted
from .journal import record_incoming
from .notify import Notifier
from .pii import mask_phone
from .safety import SendGuard, SendStatus, chat_history
from .tokens import deep_link, get_or_issue_token
from .transport.base import ChatKind, IncomingMessage, PersonalAccountTransport

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    """Итог обработки входящего — для журнала приложения и тестов."""

    action: str  # duplicate / skip:<причина> / whitelist / unknown / silent:<причина> / redirect_first / redirect_reminder
    case_ids: tuple[str, ...] = ()


class Core:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        transport: PersonalAccountTransport,
        guard: SendGuard,
        notifier: Notifier,
    ) -> None:
        self.settings = settings
        self.db = db
        self.transport = transport
        self.guard = guard
        self.notifier = notifier
        self._tasks: set[asyncio.Task] = set()

    async def handle_incoming(self, msg: IncomingMessage) -> None:
        try:
            decision = await self.process(msg)
        except Exception as e:  # noqa: BLE001 — сообщение уже в журнале, приём не останавливаем
            log.error("Ошибка обработки сообщения %s в чате %s: %s", msg.message_id, msg.chat_id, type(e).__name__)
            await self.notifier.notify(f"Ошибка обработки входящего в чате {msg.chat_id}: {type(e).__name__}", kind="error")
            return
        log.info("Чат %s: %s %s", msg.chat_id, decision.action, ",".join(decision.case_ids))

    async def process(self, msg: IncomingMessage) -> Decision:
        # 1. Журнал — первым делом.
        if not await record_incoming(self.db, msg):
            return Decision("duplicate")

        # 2. Фильтры (правило 4).
        if msg.is_own:
            return Decision("skip:own")
        if msg.chat_id <= 0 or msg.msg_type != "USER" or msg.sender_id is None:
            return Decision("skip:not_user_message")
        if await self.transport.get_chat_kind(msg.chat_id) is not ChatKind.DIALOG:
            return Decision("skip:not_dialog")
        profile = await self.transport.get_sender_profile(msg.sender_id)
        if profile is not None and profile.is_bot:
            return Decision("skip:bot")
        phone = profile.phone if profile else None

        # 3. Учёт собеседника.
        case_ids = await self._find_cases(phone)
        chat, is_new = await self._register_chat(msg, phone, case_ids)

        # 4. Белый список (правило 3).
        if await is_whitelisted(self.db, phone=phone, max_user_id=msg.sender_id):
            if case_ids and is_new:
                await self.notifier.notify(
                    f"Контакт из белого списка ({mask_phone(phone)}) совпал с должником, дело {', '.join(case_ids)}. "
                    "Автоответа не было — ответьте сами при необходимости.",
                    kind="whitelist_match",
                )
            return Decision("whitelist", tuple(case_ids))

        # 5. Неизвестный номер.
        if not case_ids:
            if is_new:
                who = mask_phone(phone) if phone else "номер скрыт"
                await self.notifier.notify(f"Пишет неизвестный ({who}), чат {msg.chat_id}. Автоответа нет.", kind="unknown")
            if not self.settings.reply_to_unknown:
                return Decision("unknown")

        # 6. Перенаправление в бот.
        return await self._redirect(msg, chat, case_ids)

    async def _find_cases(self, phone: str | None) -> list[str]:
        if not phone:
            return []
        async with self.db.session() as s:
            rows = await s.execute(select(DebtorPhone.case_id).where(DebtorPhone.phone == phone).distinct())
            return sorted(r[0] for r in rows)

    async def _register_chat(self, msg: IncomingMessage, phone: str | None, case_ids: list[str]) -> tuple[PersonalChat, bool]:
        now = utcnow()
        async with self.db.session() as s, s.begin():
            chat = await s.get(PersonalChat, msg.chat_id)
            is_new = chat is None
            if chat is None:
                chat = PersonalChat(chat_id=msg.chat_id, first_seen_at=now)
                s.add(chat)
            chat.max_user_id = msg.sender_id
            if phone:
                chat.phone = phone
            chat.case_id = case_ids[0] if len(case_ids) == 1 else None
            chat.last_incoming_at = now
        return chat, is_new

    async def _redirect(self, msg: IncomingMessage, chat: PersonalChat, case_ids: list[str]) -> Decision:
        s = self.settings
        if not s.bot_username:
            await self.notifier.notify("BOT_USERNAME не задан — перенаправлять в бот некуда", kind="config")
            return Decision("silent:no_bot_username", tuple(case_ids))

        history = await chat_history(self.db, msg.chat_id, dry_run=s.dry_run, window_days=s.redirect_window_days)
        if history.count >= 1 + s.redirect_max_reminders:
            await self._notify_limit_once(msg.chat_id, chat, case_ids, history.count)
            return Decision("silent:limit", tuple(case_ids))
        if history.last_at is not None and utcnow() - history.last_at < timedelta(hours=s.redirect_min_interval_hours):
            return Decision("silent:interval", tuple(case_ids))

        token = await get_or_issue_token(
            self.db, personal_chat_id=msg.chat_id, case_ids=case_ids, ttl_days=s.token_ttl_days
        )
        link = self.transport.format_link(deep_link(s.bot_username, token.token))
        first = history.count == 0
        template = s.redirect_first_template if first else s.redirect_reminder_template
        kind = "redirect_first" if first else "redirect_reminder"
        text = template.format(link=link)
        case_id = case_ids[0] if len(case_ids) == 1 else None
        if first and chat.limit_notified_at is not None:
            # Новый цикл после окна REDIRECT_WINDOW_DAYS — об исчерпании снова сообщим.
            async with self.db.session() as ses, ses.begin():
                row = await ses.get(PersonalChat, msg.chat_id)
                if row is not None:
                    row.limit_notified_at = None
        self._spawn(self._send(msg.chat_id, text, kind, case_id, case_ids))
        return Decision(kind, tuple(case_ids))

    async def _send(self, chat_id: int, text: str, kind: str, case_id: str | None, case_ids: list[str]) -> None:
        result = await self.guard.send(chat_id, text, kind=kind, case_id=case_id)
        if result.status in (SendStatus.BLOCKED_COOLDOWN, SendStatus.BLOCKED_CHAT_LIMIT):
            return  # штатная защита от двойного ответа — не шумим в уведомлениях
        label = "первое перенаправление" if kind == "redirect_first" else "напоминание"
        await self.notifier.notify(
            f"Чат {chat_id}, дело {', '.join(case_ids) or '—'}: {label} — {result.status.value}", kind=kind
        )

    async def _notify_limit_once(self, chat_id: int, chat: PersonalChat, case_ids: list[str], count: int) -> None:
        if chat.limit_notified_at is not None:
            return
        async with self.db.session() as s, s.begin():
            row = await s.get(PersonalChat, chat_id)
            if row is None or row.limit_notified_at is not None:
                return
            row.limit_notified_at = utcnow()
        await self.notifier.notify(
            f"Должник (чат {chat_id}, дело {', '.join(case_ids) or '—'}) продолжает писать в личный MAX после "
            f"{count} перенаправлений в бот. Больше автоответов не будет — ответьте сами.",
            kind="redirect_limit",
        )

    def _spawn(self, coro) -> None:  # type: ignore[no-untyped-def]
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Дождаться фоновых отправок (тесты, остановка)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def cancel_pending(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
