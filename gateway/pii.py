"""Нормализация телефонов и маскировка персональных данных в логах."""

from __future__ import annotations

import logging
import re

_DIGITS = re.compile(r"\D")
# Телефон РФ в произвольной записи: +7 (916) 123-45-67, 89161234567, 7916...
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
# СНИЛС: 123-456-789 01 или 12345678901 (11 цифр пересекаются с телефоном — маскируется выше).
_SNILS_RE = re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[\s-]?\d{2}(?!\d)")
# ИНН: 10 или 12 цифр подряд.
_INN_RE = re.compile(r"(?<!\d)(?:\d{12}|\d{10})(?!\d)")


def normalize_phone(value: object) -> str | None:
    """Приводит номер РФ к виду 7XXXXXXXXXX. Нераспознанный номер → None."""
    if value is None:
        return None
    digits = _DIGITS.sub("", str(value))
    if len(digits) == 11 and digits[0] in "78":
        return "7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "7" + digits
    return None


def mask_phone(value: object) -> str:
    digits = _DIGITS.sub("", str(value))
    if len(digits) < 5:
        return "<телефон>"
    return f"{digits[0]}{'*' * (len(digits) - 3)}{digits[-2:]}"


def mask_text(text: str) -> str:
    """Маскирует телефоны, СНИЛС и ИНН в произвольной строке."""
    text = _PHONE_RE.sub(lambda m: mask_phone(m.group()), text)
    text = _SNILS_RE.sub("<СНИЛС>", text)
    return _INN_RE.sub("<ИНН>", text)


class PiiMaskingFilter(logging.Filter):
    """Маскирует ПДн во всех записях лога (включая логи сторонних библиотек)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — не ломаем логирование из-за чужого формата
            return True
        masked = mask_text(message)
        if masked != message or record.args:
            record.msg = masked
            record.args = None
        return True


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(PiiMaskingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
