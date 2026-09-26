"""Конфигурация из переменных окружения (.env).

Значения по умолчанию — самые безопасные: dry-run включён, неизвестным не отвечаем,
чаты прочитанными не отмечаем.
"""

from __future__ import annotations

import re
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
    # Перенаправление в бот (правило 5): первая ссылка, затем не более
    # REDIRECT_MAX_REMINDERS мягких напоминаний, не чаще раза в REDIRECT_MIN_INTERVAL_HOURS,
    # в пределах окна REDIRECT_WINDOW_DAYS. Дальше — тишина и уведомление управляющему.
    redirect_min_interval_hours: float = Field(default=24, gt=0)
    redirect_max_reminders: int = Field(default=3, ge=0)
    redirect_window_days: int = Field(default=30, ge=1)
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

    # --- Официальный бот (ведётся в отдельной сессии; здесь — только ссылка и API обмена) ---
    bot_username: str | None = None
    # Ключ, с которым бот обращается к /bot-api/* шлюза (заголовок X-Api-Key).
    bot_api_key: SecretStr | None = None
    # Куда шлюз отправляет уведомления управляющему (эндпоинт бота) и ключ к нему.
    bot_notify_url: str | None = None
    bot_notify_key: SecretStr | None = None
    # Несрочные уведомления копятся и уходят сводкой раз в N минут.
    notify_digest_minutes: float = Field(default=60, gt=0)

    # --- Контакты телефона (Google Контакты, только чтение) ---
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_token_file: Path = Path("/data/google_token.json")
    # Как часто сервис сам синхронизирует контакты; 0 — только вручную (contacts sync).
    contacts_sync_hours: float = Field(default=24, ge=0)

    # --- Служебный API ---
    api_host: str = "127.0.0.1"  # в docker compose — 0.0.0.0, порт проброшен только на 127.0.0.1
    api_port: int = 8080
    admin_token: SecretStr | None = None

    # --- Тексты: утверждает управляющий ---
    redirect_first_template: str = f"{PLACEHOLDER_MARK} Первое перенаправление в бот: {{link}}"
    redirect_reminder_template: str = f"{PLACEHOLDER_MARK} Мягкое напоминание про бот: {{link}}"

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

    @field_validator("redirect_first_template", "redirect_reminder_template")
    @classmethod
    def _template_has_link(cls, v: str) -> str:
        if "{link}" not in v:
            raise ValueError("шаблон перенаправления должен содержать {link}")
        v.format(link="x")  # другие {…} в шаблоне недопустимы
        return v

    @field_validator("bot_username")
    @classmethod
    def _bot_username(cls, v: str | None) -> str | None:
        if not v:
            return None  # пустое значение из .env
        if not re.fullmatch(r"[A-Za-z0-9_]+", v.lstrip("@")):
            raise ValueError("BOT_USERNAME: ожидается ник бота (латиница, цифры, _)")
        return v.lstrip("@")

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
