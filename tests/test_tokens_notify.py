"""Токены и API для бота; уведомления через API бота и сводка."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from gateway.api import create_app
from gateway.db.models import LinkToken
from gateway.monitor import Monitor
from gateway.notify import BotApiNotifier, DigestNotifier
from gateway.safety import KillSwitch, SendGuard
from gateway.tokens import TOKEN_RE, deep_link, get_or_issue_token, new_token, redeem_token

from .conftest import make_settings
from .helpers import RecNotifier, shift_time

KEY = {"X-Api-Key": "bot-secret"}


def test_token_format() -> None:
    t = new_token()
    assert TOKEN_RE.fullmatch(t) and len(t) == 32
    assert deep_link("test_bot", t) == f"https://max.ru/test_bot?start={t}"


@pytest.fixture
def api(db, transport, tmp_path: Path):
    settings = make_settings(tmp_path, bot_api_key=SecretStr("bot-secret"))
    ks = KillSwitch(settings.kill_switch, settings.kill_switch_file)
    app = create_app(settings, db, transport, SendGuard(settings, db, transport, ks), Monitor(db, RecNotifier()), ks)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _issue(db, cases=("A-1",)) -> str:
    return (await get_or_issue_token(db, personal_chat_id=100, case_ids=list(cases), ttl_days=14)).token


async def test_token_lifecycle(db, api) -> None:
    token = await _issue(db)
    async with api as c:
        r = await c.get(f"/bot-api/v1/tokens/{token}", headers=KEY)
        assert r.status_code == 200 and r.json()["status"] == "valid" and r.json()["case_id"] == "A-1"
        r = await c.post(f"/bot-api/v1/tokens/{token}/redeem", headers=KEY, json={"bot_user_id": 7})
        assert r.status_code == 200 and r.json()["redeemed_now"] is True
        # повтор тем же пользователем — идемпотентно
        r = await c.post(f"/bot-api/v1/tokens/{token}/redeem", headers=KEY, json={"bot_user_id": 7})
        assert r.status_code == 200 and r.json()["redeemed_now"] is False
        # другим пользователем — нельзя (токен одноразовый)
        r = await c.post(f"/bot-api/v1/tokens/{token}/redeem", headers=KEY, json={"bot_user_id": 8})
        assert r.status_code == 409 and r.json()["status"] == "used"


async def test_expired_token_rejected(db, api) -> None:
    token = await _issue(db)
    await shift_time(db, LinkToken, "expires_at", -timedelta(days=15))
    async with api as c:
        assert (await c.get(f"/bot-api/v1/tokens/{token}", headers=KEY)).json()["status"] == "expired"
        r = await c.post(f"/bot-api/v1/tokens/{token}/redeem", headers=KEY, json={"bot_user_id": 7})
        assert r.status_code == 410


async def test_unknown_and_malformed_tokens(api) -> None:
    async with api as c:
        assert (await c.get(f"/bot-api/v1/tokens/{'0' * 32}", headers=KEY)).status_code == 404
        assert (await c.get("/bot-api/v1/tokens/DROP%20TABLE", headers=KEY)).status_code == 404
        r = await c.post("/bot-api/v1/tokens/abc/redeem", headers=KEY, json={"bot_user_id": 1})
        assert r.status_code == 404


async def test_bot_api_requires_key(db, api) -> None:
    token = await _issue(db)
    async with api as c:
        assert (await c.get(f"/bot-api/v1/tokens/{token}")).status_code == 401
        assert (await c.get(f"/bot-api/v1/tokens/{token}", headers={"X-Api-Key": "bad"})).status_code == 401


async def test_redeem_concurrent_only_one(db) -> None:
    token = await _issue(db)
    results = await asyncio.gather(*(redeem_token(db, token, bot_user_id=i) for i in range(5)))
    assert sum(1 for _, now in results if now) == 1


async def test_token_reused_until_close_to_expiry(db) -> None:
    t1 = await _issue(db)
    assert (await get_or_issue_token(db, personal_chat_id=100, case_ids=["A-1"], ttl_days=14)).token == t1
    await shift_time(db, LinkToken, "expires_at", -timedelta(days=14) + timedelta(minutes=30))
    t2 = (await get_or_issue_token(db, personal_chat_id=100, case_ids=["A-1"], ttl_days=14)).token
    assert t2 != t1


async def test_bot_api_notifier_posts_masked_json() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers.get("X-Api-Key"), json.loads(request.content)))
        return httpx.Response(200)

    n = BotApiNotifier("http://bot/notify", "k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await n.notify("Пишет +7 916 123-45-67", kind="unknown", urgent=True)
    key, body = seen[0]
    assert key == "k" and body["kind"] == "unknown" and body["urgent"] is True
    assert "123-45-67" not in body["text"]


async def test_bot_api_notifier_failure_does_not_raise(caplog) -> None:
    n = BotApiNotifier("http://bot/notify", None, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    await n.notify("текст")
    assert "не доставлено" in caplog.text


async def test_digest() -> None:
    inner = RecNotifier()
    d = DigestNotifier(inner, 3600)
    await d.notify("срочно", urgent=True)
    await d.notify("раз")
    await d.notify("два")
    assert inner.texts == ["срочно"]
    await d.flush()
    assert len(inner.items) == 2 and "раз" in inner.texts[1] and "два" in inner.texts[1]
    await d.flush()  # пустая сводка не отправляется
    assert len(inner.items) == 2
