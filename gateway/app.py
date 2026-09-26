"""Сборка и запуск сервиса: транспорт + core + мониторинг + служебный API в одном процессе.

Официальный бот — отдельный сервис (другая сессия разработки). Связь с ним — по HTTP:
бот гасит токены через /bot-api/*, шлюз шлёт уведомления на BOT_NOTIFY_URL
(контракт — docs/bot-integration.md).
"""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from .api import create_app
from .config import Settings
from .core import Core
from .db import Database
from .monitor import Monitor
from .notify import BotApiNotifier, DigestNotifier, LogNotifier, Notifier
from .safety import KillSwitch, SendGuard
from .transport.base import PersonalAccountTransport, StatusEvent, TransportStatus
from .transport.pymax_transport import LoginRequiredError, PyMaxTransport

log = logging.getLogger(__name__)


def build_transport(settings: Settings) -> PersonalAccountTransport:
    if settings.max_phone is None:
        raise SystemExit("MAX_PHONE не задан в .env")
    return PyMaxTransport(settings.max_phone.get_secret_value(), settings.session_dir)


async def run_service(settings: Settings, transport: PersonalAccountTransport | None = None) -> None:
    db = Database(settings.database_url)
    transport = transport or build_transport(settings)
    kill_switch = KillSwitch(settings.kill_switch, settings.kill_switch_file)
    base_notifier: Notifier = (
        BotApiNotifier(settings.bot_notify_url, settings.bot_notify_key.get_secret_value() if settings.bot_notify_key else None)
        if settings.bot_notify_url
        else LogNotifier()
    )
    notifier = DigestNotifier(base_notifier, settings.notify_digest_minutes * 60)
    monitor = Monitor(db, notifier)
    guard = SendGuard(settings, db, transport, kill_switch)
    core = Core(settings, db, transport, guard, notifier)

    log.warning(
        "Старт: транспорт=%s, DRY_RUN=%s, KILL_SWITCH=%s, лимит=%s/ч, перенаправлений на чат 1+%s "
        "не чаще %s ч, бот=%s, уведомления=%s",
        transport.name,
        settings.dry_run,
        kill_switch.active,
        settings.max_replies_per_hour,
        settings.redirect_max_reminders,
        settings.redirect_min_interval_hours,
        settings.bot_username or "НЕ ЗАДАН",
        "бот" if settings.bot_notify_url else "лог",
    )

    stale = await guard.cleanup_stale_pending()
    if stale:
        log.warning("Незавершённых отправок после аварийной остановки: %s — помечены unknown", stale)

    api = create_app(settings, db, transport, guard, monitor, kill_switch)
    server = uvicorn.Server(
        uvicorn.Config(api, host=settings.api_host, port=settings.api_port, log_config=None, access_log=False)
    )
    api_task = asyncio.create_task(server.serve(), name="api")
    transport_task = asyncio.create_task(_run_transport(transport, core, monitor), name="transport")
    digest_task = asyncio.create_task(notifier.run(), name="digest")
    contacts_task = asyncio.create_task(_contacts_loop(settings, db, notifier), name="contacts")
    try:
        # Процесс живёт, пока жив служебный API (до SIGTERM). Падение транспорта процесс
        # НЕ завершает: иначе restart-политика Docker переподключала бы личный аккаунт к MAX
        # в цикле. Транспорт после падения не перезапускается — только вручную (перезапуск
        # контейнера) после разбора причины.
        await api_task
    finally:
        await transport.stop()
        await core.cancel_pending()  # незавершённые отправки не выполняем при остановке
        server.should_exit = True
        transport_task.cancel()
        contacts_task.cancel()
        digest_task.cancel()  # при отмене сводка сбрасывается (DigestNotifier.run → flush)
        await asyncio.gather(api_task, transport_task, contacts_task, digest_task, return_exceptions=True)
        if isinstance(base_notifier, BotApiNotifier):
            await base_notifier.aclose()
        await db.dispose()


async def _run_transport(transport: PersonalAccountTransport, core: Core, monitor: Monitor) -> None:
    try:
        await transport.run(core.handle_incoming, monitor.on_status)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.error("Транспорт %s остановлен: %s: %s", transport.name, type(e).__name__, e)
        status = TransportStatus.SESSION_LOST if isinstance(e, LoginRequiredError) else TransportStatus.ERROR
        await monitor.on_status(StatusEvent(status, f"{type(e).__name__}: {e}"))
        return
    log.warning("Транспорт %s завершил работу; приём сообщений остановлен до перезапуска", transport.name)


async def _contacts_loop(settings: Settings, db: Database, notifier: Notifier) -> None:
    """Периодическая синхронизация контактов телефона (если настроен Google)."""
    if not settings.contacts_sync_hours or not settings.google_client_id or not settings.google_client_secret:
        log.info("Автосинхронизация контактов выключена")
        return
    from .contacts import sync_contacts
    from .google_contacts import load_google_contacts

    while True:
        try:
            contacts = await load_google_contacts(
                settings.google_client_id, settings.google_client_secret.get_secret_value(), settings.google_token_file
            )
            report = await sync_contacts(db, contacts)
            log.info("Контакты синхронизированы: %s", report.summary())
            if report.new_review:
                await notifier.notify(
                    f"В контактах телефона {report.new_review} новых спорных совпадений с должниками. "
                    "Проверка: python -m gateway contacts review-export review.csv",
                    kind="contacts_review",
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — сбой синхронизации не останавливает шлюз
            log.error("Синхронизация контактов не удалась: %s: %s", type(e).__name__, e)
            await notifier.notify(f"Синхронизация контактов телефона не удалась: {type(e).__name__}", kind="contacts_error")
        await asyncio.sleep(settings.contacts_sync_hours * 3600)
