"""Защитные правила отправки с личного аккаунта (CLAUDE.md, «ВАЖНО»).

SendGuard — единственная точка, через которую уходит что-либо с личного аккаунта.
Порядок проверок (любая неудача → отправки нет, запись в message_log):
  1. kill switch (переменная KILL_SWITCH или файл-флаг) — правило 8;
  2. блокировка мониторингом (разрыв/слёт сессии/ошибка) — правило 9;
  3. неутверждённый текст (плейсхолдер) — только в боевом режиме;
  4. чат писал нам первым — «никаких рассылок первым» (правило 5);
  5. перенаправлений в чат за окно REDIRECT_WINDOW_DAYS не больше 1 + REDIRECT_MAX_REMINDERS,
     и не чаще раза в REDIRECT_MIN_INTERVAL_HOURS — правило 5;
  6. лимит в час — правило 6;
  7. dry-run — правило 1 (только запись «отправил бы»).
Перед боевой отправкой — случайная задержка REPLY_DELAY_SEC и повторная проверка 1–2.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

from sqlalchemy import func, select, update

from .config import PLACEHOLDER_MARK, Settings
from .db import Database, utcnow
from .db.models import MessageLog
from .transport.base import PersonalAccountTransport

log = logging.getLogger(__name__)

STATE_SENDS_BLOCKED = "sends_blocked"
PERSONAL = "personal"
# Статусы исходящих, которые считаются «отправленными» для лимитов (в своём режиме).
COUNTED_STATUSES = ("sent", "dry_run", "pending", "unknown")


@dataclass(frozen=True)
class ChatHistory:
    count: int  # отправлено в чат за окно (в том же режиме dry-run/боевой)
    last_at: datetime | None


async def chat_history(db: Database, chat_id: int, *, dry_run: bool, window_days: int) -> ChatHistory:
    since = utcnow() - timedelta(days=window_days)
    async with db.session() as s:
        row = (
            await s.execute(
                select(func.count(), func.max(MessageLog.created_at)).where(
                    MessageLog.channel == PERSONAL,
                    MessageLog.direction == "out",
                    MessageLog.chat_id == chat_id,
                    MessageLog.dry_run.is_(dry_run),
                    MessageLog.status.in_(COUNTED_STATUSES),
                    MessageLog.created_at >= since,
                )
            )
        ).one()
    last = row[1]
    if last is not None and last.tzinfo is None:  # SQLite возвращает время без зоны
        last = last.replace(tzinfo=timezone.utc)
    return ChatHistory(count=row[0], last_at=last)


class SendStatus(StrEnum):
    SENT = "sent"
    DRY_RUN = "dry_run"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"  # сервис упал во время отправки — считаем отправленным (без дублей)
    BLOCKED_KILL_SWITCH = "blocked:kill_switch"
    BLOCKED_MONITOR = "blocked:monitor"
    BLOCKED_PLACEHOLDER = "blocked:placeholder"
    BLOCKED_NOT_INITIATED = "blocked:not_initiated"
    BLOCKED_COOLDOWN = "blocked:cooldown"
    BLOCKED_CHAT_LIMIT = "blocked:chat_limit"
    BLOCKED_RATE_LIMIT = "blocked:rate_limit"


@dataclass(frozen=True)
class SendResult:
    status: SendStatus
    log_id: int
    detail: str = ""

    @property
    def delivered(self) -> bool:
        return self.status is SendStatus.SENT


class KillSwitch:
    """KILL_SWITCH=true в окружении или существующий файл-флаг.

    Файл проверяется при каждой отправке — включение действует мгновенно, без перезапуска.
    """

    def __init__(self, env_flag: bool, flag_file: Path) -> None:
        self.env_flag = env_flag
        self.flag_file = flag_file

    @property
    def active(self) -> bool:
        return self.env_flag or self.flag_file.exists()

    def reason(self) -> str:
        if self.env_flag:
            return "KILL_SWITCH=true"
        return f"файл {self.flag_file}" if self.flag_file.exists() else ""

    def enable(self, note: str = "") -> None:
        self.flag_file.parent.mkdir(parents=True, exist_ok=True)
        self.flag_file.write_text(f"{utcnow().isoformat()} {note}\n", encoding="utf-8")

    def disable(self) -> None:
        self.flag_file.unlink(missing_ok=True)


class SendGuard:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        transport: PersonalAccountTransport,
        kill_switch: KillSwitch,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.transport = transport
        self.kill_switch = kill_switch
        self._sleep = sleep
        self._rng = rng or random.Random()
        # Проверка лимитов и резервирование записи — атомарно в пределах процесса.
        self._lock = asyncio.Lock()

    async def cleanup_stale_pending(self) -> int:
        """«pending» после аварийной остановки: неизвестно, ушло ли сообщение.

        Помечаем «unknown» — оно продолжает учитываться в лимитах (лучше не ответить,
        чем ответить дважды), но больше не висит как незавершённое.
        """
        async with self.db.session() as s, s.begin():
            result = await s.execute(
                update(MessageLog)
                .where(MessageLog.direction == "out", MessageLog.status == "pending")
                .values(status=SendStatus.UNKNOWN.value)
            )
            return result.rowcount or 0

    async def sends_blocked(self) -> dict | None:
        return await self.db.get_state(STATE_SENDS_BLOCKED)

    async def send(self, chat_id: int, text: str, *, kind: str = "redirect", case_id: str | None = None) -> SendResult:
        dry_run = self.settings.dry_run
        async with self._lock:
            status, detail = await self._precheck(chat_id, text, dry_run)
            if status is None:
                # Резервируем место в лимите до задержки, чтобы параллельные ответы его видели.
                status = SendStatus.DRY_RUN if dry_run else None
            log_id = await self._insert_out(chat_id, text, kind, case_id, dry_run, (status or "pending"), detail)

        if status is not None:
            if status is SendStatus.DRY_RUN:
                log.info("DRY-RUN: отправил бы в чат %s (%s), %s симв.", chat_id, kind, len(text))
            else:
                log.warning("Отправка в чат %s заблокирована: %s %s", chat_id, status.value, detail)
            return SendResult(status, log_id, detail)

        low, high = self.settings.delay_range
        try:
            await self._sleep(self._rng.uniform(low, high))
        except asyncio.CancelledError:
            # Остановка сервиса во время задержки: не отправляем и снимаем резерв.
            await asyncio.shield(self._finish(log_id, SendStatus.CANCELLED, "остановка во время задержки"))
            raise

        # За время задержки могли включить kill switch или упасть соединение.
        if self.kill_switch.active:
            return await self._finish(log_id, SendStatus.BLOCKED_KILL_SWITCH, self.kill_switch.reason())
        blocked = await self.sends_blocked()
        if blocked:
            return await self._finish(log_id, SendStatus.BLOCKED_MONITOR, str(blocked.get("reason", "")))

        try:
            message_id = await self.transport.send_text(chat_id, text)
        except Exception as e:  # noqa: BLE001
            log.error("Ошибка отправки в чат %s: %s", chat_id, type(e).__name__)
            return await self._finish(log_id, SendStatus.FAILED, f"{type(e).__name__}: {e}")
        return await self._finish(log_id, SendStatus.SENT, "", external_message_id=message_id)

    async def _precheck(self, chat_id: int, text: str, dry_run: bool) -> tuple[SendStatus | None, str]:
        if self.kill_switch.active:
            return SendStatus.BLOCKED_KILL_SWITCH, self.kill_switch.reason()
        blocked = await self.sends_blocked()
        if blocked:
            return SendStatus.BLOCKED_MONITOR, str(blocked.get("reason", ""))
        if not dry_run and PLACEHOLDER_MARK in text:
            return SendStatus.BLOCKED_PLACEHOLDER, "текст не утверждён управляющим"

        now = utcnow()
        async with self.db.session() as s:
            initiated = await s.scalar(
                select(func.count()).select_from(MessageLog).where(
                    MessageLog.channel == PERSONAL, MessageLog.direction == "in", MessageLog.chat_id == chat_id
                )
            )
            if not initiated:
                return SendStatus.BLOCKED_NOT_INITIATED, "в этот чат нам ещё не писали"

            history = await chat_history(
                self.db, chat_id, dry_run=dry_run, window_days=self.settings.redirect_window_days
            )
            max_total = 1 + self.settings.redirect_max_reminders
            if history.count >= max_total:
                return SendStatus.BLOCKED_CHAT_LIMIT, f"в чат уже ушло {history.count} из {max_total} за окно"
            min_interval = timedelta(hours=self.settings.redirect_min_interval_hours)
            if history.last_at is not None and now - history.last_at < min_interval:
                return SendStatus.BLOCKED_COOLDOWN, f"прошло меньше {self.settings.redirect_min_interval_hours} ч"

            last_hour = await s.scalar(
                select(func.count()).select_from(MessageLog).where(
                    MessageLog.channel == PERSONAL,
                    MessageLog.direction == "out",
                    MessageLog.dry_run.is_(dry_run),
                    MessageLog.status.in_(COUNTED_STATUSES),
                    MessageLog.created_at >= now - timedelta(hours=1),
                )
            )
            if last_hour >= self.settings.max_replies_per_hour:
                return SendStatus.BLOCKED_RATE_LIMIT, f"лимит {self.settings.max_replies_per_hour} в час"
        return None, ""

    async def _insert_out(
        self, chat_id: int, text: str, kind: str, case_id: str | None, dry_run: bool, status: str, detail: str
    ) -> int:
        async with self.db.session() as s, s.begin():
            row = MessageLog(
                created_at=utcnow(),
                channel=PERSONAL,
                direction="out",
                chat_id=chat_id,
                text=text,
                case_id=case_id,
                kind=kind,
                status=str(status),
                dry_run=dry_run,
                raw={"detail": detail} if detail else None,
            )
            s.add(row)
            await s.flush()
            return row.id

    async def _finish(
        self, log_id: int, status: SendStatus, detail: str, *, external_message_id: int | None = None
    ) -> SendResult:
        async with self.db.session() as s, s.begin():
            row = await s.get(MessageLog, log_id)
            assert row is not None
            row.status = status.value
            row.external_message_id = external_message_id
            if detail:
                row.raw = {"detail": detail}
        if status is not SendStatus.SENT:
            log.warning("Отправка (message_log #%s) не выполнена: %s %s", log_id, status.value, detail)
        return SendResult(status, log_id, detail)
