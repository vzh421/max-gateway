"""Сопоставление имени контакта телефона с ФИО должника.

Правила (решение управляющего: точные — автоматически, сомнительные — на проверку):
- EXACT — те же слова, что в полном ФИО должника (порядок не важен), и в ФИО
  должника не меньше 3 слов (Фамилия Имя Отчество). Регистр и «ё/е» не важны.
- PARTIAL — совпали ≥2 слова, но не всё; или 1 слово + инициалы («Иванов И.И.»);
  или в контакте лишние слова («Иванов Иван Иванович банкрот»); или у должника
  меньше 3 слов. Такие — только на проверку управляющему.
- Иначе — не совпадает.
Одно общее слово без инициалов (например, только фамилия или имя) — не совпадение:
иначе на проверку ушла бы половина записной книжки.
"""

from __future__ import annotations

import re
from collections import Counter
from enum import IntEnum

_TOKEN_RE = re.compile(r"[a-zа-я]+")


class Match(IntEnum):
    NONE = 0
    PARTIAL = 1
    EXACT = 2


def tokens(name: str | None) -> list[str]:
    if not name:
        return []
    return _TOKEN_RE.findall(name.lower().replace("ё", "е"))


def _split(toks: list[str]) -> tuple[Counter, list[str]]:
    words = Counter(t for t in toks if len(t) > 1)
    initials = [t for t in toks if len(t) == 1]
    return words, initials


def _initials_fit(initials: list[str], rest_words: Counter) -> bool:
    """Каждый инициал совпадает с первой буквой отдельного оставшегося слова."""
    pool = list(rest_words.elements())
    for ch in initials:
        for i, w in enumerate(pool):
            if w[0] == ch:
                pool.pop(i)
                break
        else:
            return False
    return True


def match(contact_name: str | None, debtor_name: str | None) -> Match:
    c_words, c_inits = _split(tokens(contact_name))
    d_words, d_inits = _split(tokens(debtor_name))
    if not c_words or not d_words:
        return Match.NONE
    common = c_words & d_words
    n_common = sum(common.values())
    if c_words == d_words and not c_inits and not d_inits and sum(d_words.values()) >= 3:
        return Match.EXACT
    if n_common >= 2:
        return Match.PARTIAL
    if n_common == 1:
        # «Иванов И.И.» ↔ «Иванов Иван Иванович» и наоборот
        if c_inits and _initials_fit(c_inits, d_words - common):
            return Match.PARTIAL
        if d_inits and _initials_fit(d_inits, c_words - common):
            return Match.PARTIAL
    return Match.NONE
