"""Выгрузка истории переписки с должниками для анализа (только чтение).

Зачем: понять, о чём пишут должники, чтобы спроектировать ответы бота (S4).

Что делает (сверено с исходниками PyMax 2.4.1):
- `fetch_chats(marker)` — список чатов страницами (CHATS_LIST, count=100);
- `get_users(ids)` — профили собеседников пакетом (CONTACT_INFO) → телефон;
- `fetch_history(chat_id, backward=N, from_time=…)` — история (CHAT_HISTORY,
  interactive=False). Отметку «прочитано» (CHAT_MARK) ни один из вызовов не шлёт.
НЕ проверено запуском: как сервер двигает маркер fetch_chats (берём минимальное
last_event_time страницы и останавливаемся, если новых чатов нет) и не отмечает ли сервер
чат прочитанным сам в ответ на CHAT_HISTORY.

Никаких отправок. Поиск по номеру телефона (search_by_phone) не используется: массовый
поиск по телефонам похож на поведение спам-бота.

Выгружаются только личные чаты, где телефон собеседника есть в реестре должников.
Обезличивание: чат → псевдоним D001…; телефоны, ИНН, СНИЛС, e-mail, номера карт и счетов,
паспорт — маскируются; ФИО должника по делу и управляющего — заменяются на [ДОЛЖНИК] /
[УПРАВЛЯЮЩИЙ]; вложения — только тип. Другие имена в тексте могут остаться — файл всё
равно содержит персональные данные и хранится только у управляющего.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .names import tokens
from .pii import mask_text, normalize_phone

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){16,19}(?!\d)")
_ACCOUNT = re.compile(r"(?<!\d)\d{20}(?!\d)")
_PASSPORT = re.compile(r"(?<!\d)\d{2}\s?\d{2}\s?\d{6}(?!\d)")


class HistoryReader(Protocol):
    """Подмножество клиента PyMax, нужное выгрузке (только чтение)."""

    my_id: int

    async def fetch_chats(self, marker: int | None) -> list[Any]: ...
    async def get_users(self, user_ids: list[int]) -> list[Any]: ...
    async def fetch_history(self, chat_id: int, backward: int, from_time: int | None) -> list[Any]: ...


@dataclass
class ExportReport:
    chats_seen: int = 0
    dialogs: int = 0
    debtor_chats: int = 0
    messages: int = 0
    pages: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"чатов просмотрено: {self.chats_seen} (личных: {self.dialogs}), с должниками: {self.debtor_chats}, "
            f"сообщений выгружено: {self.messages}, страниц списка: {self.pages}"
            + ("" if not self.notes else "; " + "; ".join(self.notes))
        )


def _name_patterns(names: list[str]) -> list[re.Pattern[str]]:
    words = {t for n in names for t in tokens(n) if len(t) > 2}
    # Слово целиком, в любом падеже по началу основы (Иванов → Иванова/Ивановым).
    return [re.compile(rf"(?i)(?<![а-яёa-z]){re.escape(w[:-1] if len(w) > 4 else w)}[а-яёa-z]*") for w in words]


def anonymize(text: str, debtor_names: list[str], manager_names: list[str]) -> str:
    text = _EMAIL.sub("<e-mail>", text)
    text = _ACCOUNT.sub("<счёт>", text)
    text = _CARD.sub("<карта>", text)
    text = mask_text(text)  # телефоны, СНИЛС, ИНН
    text = _PASSPORT.sub("<паспорт>", text)
    for p in _name_patterns(manager_names):
        text = p.sub("[УПРАВЛЯЮЩИЙ]", text)
    for p in _name_patterns(debtor_names):
        text = p.sub("[ДОЛЖНИК]", text)
    return text


def _attach_types(msg: Any) -> list[str]:
    out = []
    for a in getattr(msg, "attaches", None) or []:
        t = getattr(a, "type", None)
        out.append(str(getattr(t, "value", t)))
    return out


async def export_history(
    reader: HistoryReader,
    debtor_phones: dict[str, list[str]],  # телефон → номера дел
    debtor_names: dict[str, str],  # номер дела → ФИО
    out_path: Path,
    *,
    manager_names: list[str] = (),  # type: ignore[assignment]
    per_chat: int = 200,
    max_chats: int = 300,
    max_pages: int = 50,
    pause_sec: float = 2.0,
    sleep=asyncio.sleep,
) -> ExportReport:
    report = ExportReport()
    # 1. Личные чаты и собеседники.
    partner_by_chat: dict[int, int] = {}
    seen: set[int] = set()
    marker: int | None = None
    for _ in range(max_pages):
        chats = await reader.fetch_chats(marker)
        report.pages += 1
        new = [c for c in chats if c.id not in seen]
        if not new:
            break
        for c in new:
            seen.add(c.id)
            report.chats_seen += 1
            ctype = str(getattr(getattr(c, "type", None), "value", getattr(c, "type", None)))
            if ctype != "DIALOG":
                continue
            others = [int(u) for u in (getattr(c, "participants", None) or {}) if int(u) != reader.my_id]
            if len(others) == 1:
                partner_by_chat[c.id] = others[0]
                report.dialogs += 1
        times = [getattr(c, "last_event_time", None) for c in new]
        times = [t for t in times if t]
        if not times:
            report.notes.append("у чатов нет last_event_time — список не листается дальше первой страницы")
            break
        next_marker = min(times)
        if marker is not None and next_marker >= marker:
            break
        marker = next_marker
        await sleep(pause_sec)
    else:
        report.notes.append(f"достигнут предел страниц ({max_pages})")

    # 2. Телефоны собеседников пакетами.
    phone_by_user: dict[int, str] = {}
    user_ids = sorted(set(partner_by_chat.values()))
    for i in range(0, len(user_ids), 100):
        for u in await reader.get_users(user_ids[i : i + 100]):
            if "BOT" in (getattr(u, "options", None) or []):
                continue
            p = normalize_phone(getattr(u, "phone", None))
            if p:
                phone_by_user[u.id] = p
        await sleep(pause_sec)

    debtor_chats = [
        (chat_id, debtor_phones[phone_by_user[uid]])
        for chat_id, uid in partner_by_chat.items()
        if uid in phone_by_user and phone_by_user[uid] in debtor_phones
    ][:max_chats]
    if len(debtor_chats) == max_chats:
        report.notes.append(f"ограничение {max_chats} чатов")

    # 3. История, обезличенная.
    with out_path.open("w", encoding="utf-8") as f:
        for n, (chat_id, cases) in enumerate(debtor_chats, start=1):
            names = [debtor_names[c] for c in cases if c in debtor_names]
            messages: list[Any] = []
            from_time: int | None = None
            while len(messages) < per_chat:
                batch = await reader.fetch_history(chat_id, min(40, per_chat - len(messages)), from_time)
                batch = [m for m in batch if all(m.id != x.id for x in messages)]
                if not batch:
                    break
                messages.extend(batch)
                from_time = min(m.time for m in batch) - 1
                await sleep(pause_sec)
            messages.sort(key=lambda m: m.time)
            for m in messages:
                role = "управляющий" if m.sender == reader.my_id else "должник"
                rec = {
                    "chat": f"D{n:03d}",
                    "date": datetime.fromtimestamp(m.time / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                    "role": role,
                    "text": anonymize(m.text or "", names, list(manager_names)),
                    "attachments": _attach_types(m),
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                report.messages += 1
            report.debtor_chats += 1
    return report
