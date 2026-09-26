"""Одноразовые токены deep link бота: выпуск (шлюз) и погашение (бот через /bot-api).

Формат токена — 32 шестнадцатеричных символа (128 бит, `secrets.token_hex(16)`).
Не `token_urlsafe`: его «_» вырезает markdown PyMax в исходящем тексте, а алфавит
hex входит в допустимый для payload (латиница, цифры, «-», «_»; до 128 символов).
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from sqlalchemy import select, update

from .db import Database, utcnow
from .db.models import LinkToken

TOKEN_RE = re.compile(r"[0-9a-f]{32}")


def new_token() -> str:
    return secrets.token_hex(16)


def deep_link(bot_username: str, token: str | None = None) -> str:
    base = f"https://max.ru/{bot_username}"
    return f"{base}?start={token}" if token else base


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:  # SQLite хранит время без зоны
        return dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(frozen=True)
class ChatToken:
    token: str | None  # None — действующего токена нет, но чат уже привязан (ссылка без payload)
    reused: bool


async def get_or_issue_token(
    db: Database, *, personal_chat_id: int, case_ids: list[str], ttl_days: int
) -> ChatToken:
    """Действующий неиспользованный токен чата переиспользуется (одна ссылка в напоминаниях).

    Если токен этого чата уже погашен ботом — должник в боте; новый токен не нужен.
    """
    now = utcnow()
    async with db.session() as s, s.begin():
        rows = (
            await s.execute(
                select(LinkToken).where(LinkToken.personal_chat_id == personal_chat_id).order_by(LinkToken.created_at.desc())
            )
        ).scalars().all()
        if any(r.used_at is not None for r in rows):
            return ChatToken(token=None, reused=True)
        for r in rows:
            if _aware(r.expires_at) > now + timedelta(hours=1):
                return ChatToken(token=r.token, reused=True)
        token = new_token()
        s.add(
            LinkToken(
                token=token,
                case_id=case_ids[0] if len(case_ids) == 1 else None,
                candidate_case_ids=case_ids if len(case_ids) > 1 else None,
                personal_chat_id=personal_chat_id,
                created_at=now,
                expires_at=now + timedelta(days=ttl_days),
            )
        )
    return ChatToken(token=token, reused=False)


class TokenState(StrEnum):
    VALID = "valid"
    USED = "used"
    EXPIRED = "expired"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class TokenInfo:
    state: TokenState
    case_id: str | None = None
    candidate_case_ids: list[str] | None = None
    expires_at: datetime | None = None
    used_at: datetime | None = None
    used_by_bot_user_id: int | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.state.value,
            "case_id": self.case_id,
            "candidate_case_ids": self.candidate_case_ids or [],
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "used_at": self.used_at.isoformat() if self.used_at else None,
        }


def _info(row: LinkToken | None, now: datetime) -> TokenInfo:
    if row is None:
        return TokenInfo(TokenState.NOT_FOUND)
    expires, used = _aware(row.expires_at), _aware(row.used_at)
    if used is not None:
        state = TokenState.USED
    elif expires <= now:
        state = TokenState.EXPIRED
    else:
        state = TokenState.VALID
    return TokenInfo(state, row.case_id, row.candidate_case_ids, expires, used, row.used_by_bot_user_id)


async def get_token_info(db: Database, token: str) -> TokenInfo:
    if not TOKEN_RE.fullmatch(token):
        return TokenInfo(TokenState.NOT_FOUND)
    async with db.session() as s:
        return _info(await s.get(LinkToken, token), utcnow())


async def redeem_token(db: Database, token: str, *, bot_user_id: int) -> tuple[TokenInfo, bool]:
    """Погасить токен. Возвращает (состояние, погашен_сейчас).

    Атомарно: условный UPDATE — второй параллельный вызов токен не получит.
    Повторный вызов тем же пользователем бота — идемпотентен (USED, но с его id).
    """
    if not TOKEN_RE.fullmatch(token):
        return TokenInfo(TokenState.NOT_FOUND), False
    now = utcnow()
    async with db.session() as s, s.begin():
        result = await s.execute(
            update(LinkToken)
            .where(LinkToken.token == token, LinkToken.used_at.is_(None), LinkToken.expires_at > now)
            .values(used_at=now, used_by_bot_user_id=bot_user_id)
        )
        redeemed = result.rowcount == 1
    async with db.session() as s:
        info = _info(await s.get(LinkToken, token), utcnow())
    return info, redeemed
