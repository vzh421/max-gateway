from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import AppState, Base

__all__ = ["Base", "Database", "utcnow"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> AsyncSession:
        return self.sessionmaker()

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def get_state(self, key: str) -> dict[str, Any] | None:
        async with self.session() as s:
            row = await s.get(AppState, key)
            return row.value if row else None

    async def set_state(self, key: str, value: dict[str, Any] | None) -> None:
        async with self.session() as s, s.begin():
            row = await s.get(AppState, key)
            if row is None:
                s.add(AppState(key=key, value=value))
            else:
                row.value = value
                row.updated_at = utcnow()

    async def list_state(self) -> dict[str, Any]:
        async with self.session() as s:
            rows = (await s.execute(select(AppState))).scalars().all()
            return {r.key: r.value for r in rows}
