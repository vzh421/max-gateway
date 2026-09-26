"""Служебный HTTP API (FastAPI). Наружу не публикуется — порт только на 127.0.0.1.

Админ-эндпоинты требуют заголовок X-Admin-Token = ADMIN_TOKEN. Без ADMIN_TOKEN они выключены.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import Settings
from .db import Database
from .importers import ImportReport, PhoneRow, add_debtor_phones, parse_debtor_phones
from .monitor import Monitor
from .safety import KillSwitch, SendGuard
from .tokens import TokenState, get_token_info, redeem_token
from .transport.base import PersonalAccountTransport


class PhoneIn(BaseModel):
    case_id: str
    phone: str
    note: str | None = None


class PhonesIn(BaseModel):
    source: str = "api"
    items: list[PhoneIn]


class RedeemIn(BaseModel):
    bot_user_id: int
    bot_chat_id: int | None = None


class KillSwitchIn(BaseModel):
    on: bool
    note: str = ""


def create_app(
    settings: Settings,
    db: Database,
    transport: PersonalAccountTransport,
    guard: SendGuard,
    monitor: Monitor,
    kill_switch: KillSwitch,
) -> FastAPI:
    app = FastAPI(title="max-gateway", docs_url=None, redoc_url=None, openapi_url=None)

    def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
        expected = settings.admin_token.get_secret_value() if settings.admin_token else None
        if not expected:
            raise HTTPException(503, "ADMIN_TOKEN не задан — служебные эндпоинты выключены")
        if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
            raise HTTPException(401, "Неверный X-Admin-Token")

    admin = [Depends(require_admin)]

    def require_bot(x_api_key: Annotated[str | None, Header()] = None) -> None:
        expected = settings.bot_api_key.get_secret_value() if settings.bot_api_key else None
        if not expected:
            raise HTTPException(503, "BOT_API_KEY не задан — API для бота выключен")
        if not x_api_key or not hmac.compare_digest(x_api_key, expected):
            raise HTTPException(401, "Неверный X-Api-Key")

    bot = [Depends(require_bot)]

    # --- API для бота (контракт: docs/bot-integration.md) ---

    @app.get("/bot-api/v1/tokens/{token}", dependencies=bot)
    async def token_info(token: str) -> dict:
        info = await get_token_info(db, token)
        if info.state is TokenState.NOT_FOUND:
            raise HTTPException(404, {"status": "not_found"})
        return info.as_dict()

    @app.post("/bot-api/v1/tokens/{token}/redeem", dependencies=bot)
    async def token_redeem(token: str, body: RedeemIn) -> JSONResponse:
        info, redeemed = await redeem_token(db, token, bot_user_id=body.bot_user_id)
        data = info.as_dict() | {"redeemed_now": redeemed}
        if info.state is TokenState.NOT_FOUND:
            return JSONResponse(data, status_code=404)
        if redeemed:
            return JSONResponse(data, status_code=200)
        if info.state is TokenState.USED and info.used_by_bot_user_id == body.bot_user_id:
            return JSONResponse(data, status_code=200)  # повтор тем же пользователем — идемпотентно
        if info.state is TokenState.EXPIRED:
            return JSONResponse(data, status_code=410)
        return JSONResponse(data, status_code=409)  # погашен другим пользователем

    @app.get("/health")
    async def health() -> dict:
        blocked = await guard.sends_blocked()
        last = monitor.last_status
        return {
            "transport": transport.name,
            "connected": transport.is_connected,
            "last_status": last.status.value if last else None,
            "dry_run": settings.dry_run,
            "kill_switch": kill_switch.active,
            "sends_blocked": bool(blocked),
        }

    @app.get("/admin/status", dependencies=admin)
    async def status() -> dict:
        return {
            "kill_switch": {"active": kill_switch.active, "reason": kill_switch.reason()},
            "state": await db.list_state(),
        }

    @app.post("/admin/debtor-phones", dependencies=admin)
    async def add_phones(body: PhonesIn) -> dict:
        report = ImportReport()
        rows: list[PhoneRow] = parse_debtor_phones(
            [{"case_id": i.case_id, "phone": i.phone, "note": i.note or ""} for i in body.items], report
        )
        await add_debtor_phones(db, rows, body.source, report)
        return {"total": report.total_rows, "added": report.added, "duplicates": report.duplicates, "errors": report.errors}

    @app.post("/admin/kill-switch", dependencies=admin)
    async def set_kill_switch(body: KillSwitchIn) -> dict:
        if body.on:
            kill_switch.enable(body.note)
        else:
            kill_switch.disable()
        return {"active": kill_switch.active, "reason": kill_switch.reason()}

    @app.post("/admin/sends/resume", dependencies=admin)
    async def resume() -> dict:
        await monitor.resume_sends()
        return {"sends_blocked": False}

    return app
