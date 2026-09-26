"""Конфигурация из переменных окружения (.env).

Значения по умолчанию — самые безопасные: dry-run включён, неизвестным не отвечаем,
чаты прочитанными не отмечаем.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Маркер неутверждённого текста. Пока он есть в шаблоне, боевая отправка запрещена.
PLACEHOLDER_MARK = "[НЕ УТВЕРЖДЕНО]"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Защитные правила (CLAUDE.md, раздел «ВАЖНО») ---
    dry_run: bool = True
    reply_to_unknown: bool = False
    reply_cooldown_days: int = Field(default=30, ge=1)
    max_replies_per_hour: int = Field(default=10, ge=0)
    reply_delay_sec: str = "20-90"
    mark_read: bool = False
    kill_switch: bool = False
    kill_switch_file: Path = Path("/data/KILL_SWITCH")

    # --- Личный аккаунт MAX ---
    max_phone: SecretStr | None = None
    session_dir: Path = Path("/data/session")

    # --- БД ---
    database_url: str = "postgresql+asyncpg://gateway:gateway@db:5432/gateway"

    # --- Токены привязки ---
    token_ttl_days: int = Field(default=14, ge=1)

    # --- ai4au (только чтение) ---
    ai4au_base_url: str = "https://ai4au.ru"
    ai4au_api_token: SecretStr | None = None

    # --- Официальный бот (этап 2) ---
    bot_token: SecretStr | None = None
    bot_username: str | None = None

    # --- Служебный API ---
    api_host: str = "127.0.0.1"  # в docker compose — 0.0.0.0, порт проброшен только на 127.0.0.1
    api_port: int = 8080
    admin_token: SecretStr | None = None

    # --- Тексты: утверждает управляющий ---
    auto_reply_template: str = f"{PLACEHOLDER_MARK} Текст автоответа со ссылкой {{link}}"

    log_level: str = "INFO"

    @field_validator("mark_read")
    @classmethod
    def _mark_read_forbidden(cls, v: bool) -> bool:
        if v:
            raise ValueError("MARK_READ=true не поддерживается: чаты управляющего нельзя отмечать прочитанными")
        return v

    @field_validator("reply_delay_sec")
    @classmethod
    def _check_delay(cls, v: str) -> str:
        parse_delay_range(v)
        return v

    @field_validator("log_level")
    @classmethod
    def _no_debug(cls, v: str) -> str:
        # На DEBUG PyMax пишет в лог полные кадры, включая токен и тексты.
        if v.upper() == "DEBUG":
            raise ValueError("LOG_LEVEL=DEBUG запрещён: библиотека MAX пишет в лог токен сессии")
        return v.upper()

    @property
    def delay_range(self) -> tuple[float, float]:
        return parse_delay_range(self.reply_delay_sec)


def parse_delay_range(value: str) -> tuple[float, float]:
    """«20-90» → (20.0, 90.0); «30» → (30.0, 30.0)."""
    parts = [p.strip() for p in value.replace("–", "-").split("-")]
    try:
        nums = [float(p) for p in parts if p]
    except ValueError as e:
        raise ValueError(f"REPLY_DELAY_SEC: ожидается «мин-макс», получено {value!r}") from e
    if len(nums) == 1:
        nums = nums * 2
    if len(nums) != 2 or nums[0] < 0 or nums[0] > nums[1]:
        raise ValueError(f"REPLY_DELAY_SEC: ожидается «мин-макс», получено {value!r}")
    return nums[0], nums[1]


@lru_cache
def get_settings() -> Settings:
    return Settings()
