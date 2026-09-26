"""Модель данных сервиса (CLAUDE.md, «Модель данных»).

Id MAX хранятся в BigInteger: id сообщений ~1.2e17, id каналов отрицательные
(по журналу этапа 0). case_id ai4au — строка: тип id в ai4au ещё не сверен.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# В SQLite (тесты) автоинкремент работает только у INTEGER PRIMARY KEY.
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    pass


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DebtorPhone(Base):
    """Телефоны должников. В ai4au телефонов нет — храним у себя."""

    __tablename__ = "debtor_phones"
    __table_args__ = (UniqueConstraint("case_id", "phone", name="uq_debtor_phones_case_phone"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False)
    phone: Mapped[str] = mapped_column(String(11), nullable=False, index=True)  # 7XXXXXXXXXX
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class WhitelistEntry(Base):
    """Кому автоответ запрещён всегда (адресная книга на момент первого запуска + ручной)."""

    __tablename__ = "whitelist"
    __table_args__ = (
        CheckConstraint("phone IS NOT NULL OR max_user_id IS NOT NULL", name="ck_whitelist_has_key"),
        CheckConstraint("source IN ('address_book', 'manual')", name="ck_whitelist_source"),
        UniqueConstraint("phone", name="uq_whitelist_phone"),
        UniqueConstraint("max_user_id", name="uq_whitelist_user"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    phone: Mapped[str | None] = mapped_column(String(11))
    max_user_id: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class PersonalChat(Base):
    """Личный чат аккаунта управляющего ↔ телефон ↔ дело."""

    __tablename__ = "personal_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    max_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    phone: Mapped[str | None] = mapped_column(String(11), index=True)
    case_id: Mapped[str | None] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = _now()
    last_incoming_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_auto_reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LinkToken(Base):
    """Одноразовый токен deep link бота → дело."""

    __tablename__ = "link_tokens"

    token: Mapped[str] = mapped_column(String(128), primary_key=True)
    case_id: Mapped[str | None] = mapped_column(String(64))  # None — выбор дела в боте
    personal_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by_bot_user_id: Mapped[int | None] = mapped_column(BigInteger)


class BotBinding(Base):
    """Пользователь бота ↔ дело."""

    __tablename__ = "bot_bindings"
    __table_args__ = (UniqueConstraint("bot_user_id", "case_id", name="uq_bot_bindings_user_case"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    bot_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bot_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False)
    token: Mapped[str | None] = mapped_column(String(128))
    pd_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bound_at: Mapped[datetime] = _now()


class MessageLog(Base):
    """Журнал всех входящих и исходящих (канал personal/bot)."""

    __tablename__ = "message_log"
    __table_args__ = (
        # Идемпотентность: повторное событие о том же сообщении не создаёт вторую запись.
        UniqueConstraint("channel", "direction", "chat_id", "external_message_id", name="uq_message_log_event"),
        CheckConstraint("channel IN ('personal', 'bot')", name="ck_message_log_channel"),
        CheckConstraint("direction IN ('in', 'out')", name="ck_message_log_direction"),
        Index("ix_message_log_out_recent", "channel", "direction", "dry_run", "created_at"),
        Index("ix_message_log_chat", "channel", "chat_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = _now()
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(3), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    external_message_id: Mapped[int | None] = mapped_column(BigInteger)
    sender_id: Mapped[int | None] = mapped_column(BigInteger)
    text: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[list[Any] | None] = mapped_column(JSON)
    case_id: Mapped[str | None] = mapped_column(String(64))
    # Для исходящих: auto_reply / notification / bot_reply.
    kind: Mapped[str | None] = mapped_column(String(32))
    # Для исходящих: pending / sent / dry_run / blocked:<причина> / failed.
    status: Mapped[str | None] = mapped_column(String(48))
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class AppState(Base):
    """Служебное состояние: блокировка отправок, импорт адресной книги и т.п."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
