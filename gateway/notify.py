"""Уведомления управляющему.

Доставка — через эндпоинт бота (бот ведётся в отдельной сессии, контракт —
docs/bot-integration.md). Срочные (сбой соединения) уходят сразу, остальные
копятся и уходят сводкой раз в NOTIFY_DIGEST_MINUTES («сводкой, не спамом»).
Текст уведомлений проходит маскировку ПДн.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import httpx

from .pii import mask_text

log = logging.getLogger(__name__)


class Notifier(Protocol):
    async def notify(self, text: str, *, kind: str = "info", urgent: bool = False) -> None: ...


class LogNotifier:
    """Если BOT_NOTIFY_URL не задан — уведомления только в лог."""

    async def notify(self, text: str, *, kind: str = "info", urgent: bool = False) -> None:
        log.warning("УВЕДОМЛЕНИЕ УПРАВЛЯЮЩЕМУ [%s%s]: %s", kind, ", срочно" if urgent else "", mask_text(text))


class BotApiNotifier:
    """POST {url} с JSON {"kind", "urgent", "text"} и заголовком X-Api-Key."""

    def __init__(self, url: str, key: str | None, *, client: httpx.AsyncClient | None = None) -> None:
        self.url = url
        self.key = key
        self._client = client or httpx.AsyncClient(timeout=10)
        self._fallback = LogNotifier()

    async def notify(self, text: str, *, kind: str = "info", urgent: bool = False) -> None:
        payload = {"kind": kind, "urgent": urgent, "text": mask_text(text)}
        headers = {"X-Api-Key": self.key} if self.key else {}
        try:
            r = await self._client.post(self.url, json=payload, headers=headers)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001 — уведомление не должно ронять обработку
            log.error("Уведомление через бота не доставлено: %s", type(e).__name__)
            await self._fallback.notify(text, kind=kind, urgent=urgent)

    async def aclose(self) -> None:
        await self._client.aclose()


class DigestNotifier:
    """Срочные — сразу; несрочные — сводкой (одним сообщением) раз в interval секунд."""

    def __init__(self, inner: Notifier, interval_sec: float) -> None:
        self.inner = inner
        self.interval_sec = interval_sec
        self._buffer: list[str] = []
        self._lock = asyncio.Lock()

    async def notify(self, text: str, *, kind: str = "info", urgent: bool = False) -> None:
        if urgent:
            await self.inner.notify(text, kind=kind, urgent=True)
            return
        async with self._lock:
            self._buffer.append(text)

    async def flush(self) -> None:
        async with self._lock:
            items, self._buffer = self._buffer, []
        if not items:
            return
        lines = "\n".join(f"• {t}" for t in items[:100])
        more = f"\n… и ещё {len(items) - 100}" if len(items) > 100 else ""
        await self.inner.notify(f"Шлюз MAX, сводка ({len(items)}):\n{lines}{more}", kind="digest")

    async def run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval_sec)
                await self.flush()
        finally:
            await self.flush()
