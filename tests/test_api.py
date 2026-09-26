from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from gateway.api import create_app
from gateway.monitor import Monitor
from gateway.safety import KillSwitch, SendGuard

from .conftest import make_settings
from .helpers import RecNotifier


@pytest.fixture
def client_factory(db, transport, tmp_path: Path):
    def factory(**overrides) -> httpx.AsyncClient:
        settings = make_settings(tmp_path, **overrides)
        ks = KillSwitch(settings.kill_switch, settings.kill_switch_file)
        app = create_app(settings, db, transport, SendGuard(settings, db, transport, ks), Monitor(db, RecNotifier()), ks)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    return factory


async def test_health(client_factory) -> None:
    async with client_factory() as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json()["dry_run"] is True and r.json()["kill_switch"] is False


async def test_admin_disabled_without_token(client_factory) -> None:
    async with client_factory() as c:
        assert (await c.get("/admin/status")).status_code == 503


async def test_admin_wrong_token(client_factory) -> None:
    async with client_factory(admin_token=SecretStr("secret")) as c:
        assert (await c.get("/admin/status", headers={"X-Admin-Token": "bad"})).status_code == 401


async def test_admin_phones_and_kill_switch(client_factory, tmp_path: Path) -> None:
    h = {"X-Admin-Token": "secret"}
    async with client_factory(admin_token=SecretStr("secret")) as c:
        r = await c.post("/admin/debtor-phones", headers=h, json={"items": [
            {"case_id": "1", "phone": "8 916 123 45 67"}, {"case_id": "1", "phone": "плохой"}]})
        assert r.json()["added"] == 1 and len(r.json()["errors"]) == 1
        r = await c.post("/admin/kill-switch", headers=h, json={"on": True, "note": "тест"})
        assert r.json()["active"] is True and (tmp_path / "KILL_SWITCH").exists()
        r = await c.post("/admin/kill-switch", headers=h, json={"on": False})
        assert r.json()["active"] is False
