"""Сопоставление имени контакта телефона с ФИО должника.

Правила (решение управляющего: точные — автоматически, сомнительные — на проверку):
- EXACT — те же слова, что в полном ФИО должника (порядок не важен), и в ФИО
  должника не меньше 3 слов (Фамилия Имя Отчество). Регистр и «ё/е» не важны.
- PARTIAL — совпала ФАМИЛИЯ должника и ещё хотя бы одно слово или инициалы
  («Иванов Иван», «Иванов И.И.», «Иванов Иван Иванович банкрот»); или у должника
  меньше 3 слов. Такие — только на проверку управляющему.
- Иначе — не совпадает.
Фамилия — первое слово ФИО должника («Фамилия Имя Отчество», как в case-map.json).
Совпадение только имени и отчества при другой фамилии («Сергей Александрович») — не
совпадение: на реальных данных это давало ~300 ложных кандидатов из ~460.
Одно общее слово без инициалов (только фамилия или имя) — тоже не совпадение.
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
    d_tokens = [t for t in tokens(debtor_name) if len(t) > 1]
    c_words, c_inits = _split(tokens(contact_name))
    d_words, d_inits = _split(tokens(debtor_name))
    if not c_words or not d_words or not d_tokens:
        return Match.NONE
    surname = d_tokens[0]
    common = c_words & d_words
    n_common = sum(common.values())
    if c_words == d_words and not c_inits and not d_inits and sum(d_words.values()) >= 3:
        return Match.EXACT
    if surname not in common:
        return Match.NONE
    if n_common >= 2:
        return Match.PARTIAL
    # «Иванов И.И.» ↔ «Иванов Иван Иванович»
    if c_inits and _initials_fit(c_inits, d_words - common):
        return Match.PARTIAL
    return Match.NONE
