"""Разбор CSV-экспорта Google Контактов.

ФОРМАТ НЕ СВЕРЕН с документацией Google (support.google.com из среды разработки недоступен)
и с реальным файлом. Разбор намеренно терпимый:
- телефоны — все колонки вида «Phone N - Value» (регистр не важен); в одной ячейке
  может быть несколько номеров через « ::: »;
- имя — из колонок полного имени («Name», «File As») и частей
  («First/Given Name», «Middle/Additional Name», «Last/Family Name»).
Если колонок с телефонами не найдено — ошибка со списком заголовков: ничего не загружается.
Контакты с одним номером объединяются (решения управляющего привязаны к номеру).
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from .contacts import RawContact
from .pii import normalize_phone

_PHONE_COL = re.compile(r"^phone\s*\d+\s*-\s*value$", re.I)
_FULL = ("name", "file as")
_FIRST = ("first name", "given name")
_MIDDLE = ("middle name", "additional name")
_LAST = ("last name", "family name")
RESOURCE = "google_csv"


class GoogleCsvError(ValueError):
    pass


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise GoogleCsvError("Кодировка не распознана (ожидается UTF-8)")


def parse_google_csv(text: str) -> tuple[list[RawContact], int]:
    """→ (контакты, сгруппированные по номеру; число нераспознанных номеров)."""
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    lower = {h: h.strip().lower() for h in headers}
    phone_cols = [h for h in headers if _PHONE_COL.match(lower[h])]
    if not phone_cols:
        raise GoogleCsvError(
            "Не найдены колонки телефонов вида «Phone 1 - Value». Заголовки файла: " + ", ".join(headers[:40])
        )

    def col(names: tuple[str, ...]) -> str | None:
        return next((h for h in headers if lower[h] in names), None)

    full_cols = [h for h in headers if lower[h] in _FULL]
    first, middle, last = col(_FIRST), col(_MIDDLE), col(_LAST)

    by_phone: dict[str, list[str]] = {}
    bad = 0
    for row in reader:
        names: list[str] = []
        for h in full_cols:
            v = (row.get(h) or "").strip()
            if v and v not in names:
                names.append(v)
        parts = " ".join(p for p in ((row.get(last) or "").strip() if last else "",
                                     (row.get(first) or "").strip() if first else "",
                                     (row.get(middle) or "").strip() if middle else "") if p)
        if parts and parts not in names:
            names.append(parts)
        for h in phone_cols:
            for raw in (row.get(h) or "").split(":::"):
                raw = raw.strip()
                if not raw:
                    continue
                phone = normalize_phone(raw)
                if not phone:
                    bad += 1
                    continue
                acc = by_phone.setdefault(phone, [])
                acc.extend(n for n in names if n not in acc)
    contacts = [RawContact(RESOURCE, tuple(n), ("+" + p,)) for p, n in by_phone.items()]
    return contacts, bad


def load_google_csv(path: Path) -> tuple[list[RawContact], int]:
    return parse_google_csv(_decode(path.read_bytes()))
