"""Общие фикстуры. БД: SQLite в памяти всегда; PostgreSQL — если задан TEST_DATABASE_URL."""

from __future__ import annotations

import os
import random
from pathlib import Path

import pytest

from gateway.config import Settings
from gateway.db import Database
from gateway.db.models import Base
from gateway.safety import KillSwitch, SendGuard
from gateway.transport.fake import FakeTransport

_PG_URL = os.environ.get("TEST_DATABASE_URL")
_ENV_KEYS = [
    "DRY_RUN", "REPLY_TO_UNKNOWN", "REPLY_COOLDOWN_DAYS", "MAX_REPLIES_PER_HOUR", "REPLY_DELAY_SEC",
    "MARK_READ", "KILL_SWITCH", "KILL_SWITCH_FILE", "LOG_LEVEL", "DATABASE_URL", "ADMIN_TOKEN",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "postgres":
        if not _PG_URL:
            pytest.skip("TEST_DATABASE_URL не задан")
        url = _PG_URL
    else:
        url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    database = Database(url)
    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield database
    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await database.dispose()


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {"kill_switch_file": tmp_path / "KILL_SWITCH", "reply_delay_sec": "20-90"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []
        self.hook = None  # вызывается во время «задержки»

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.hook:
            await self.hook()


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def sleeper() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
def guard_factory(db: Database, transport: FakeTransport, sleeper: SleepRecorder, tmp_path: Path):
    def factory(**overrides) -> SendGuard:
        settings = make_settings(tmp_path, **overrides)
        ks = KillSwitch(settings.kill_switch, settings.kill_switch_file)
        return SendGuard(settings, db, transport, ks, sleep=sleeper, rng=random.Random(1))

    return factory
