"""Выгрузка истории переписки: только чтение, только должники, обезличивание."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace as NS

from gateway.history_export import anonymize, export_history

ME = 3911587


class FakeReader:
    my_id = ME

    def __init__(self, pages, users, history) -> None:
        self.pages, self.users, self.history = pages, users, history
        self.calls: list[str] = []

    async def fetch_chats(self, marker):
        self.calls.append(f"chats:{marker}")
        if marker is None:
            return self.pages[0]
        return next((p for p in self.pages if p and min(c.last_event_time for c in p) < marker and p is not self.pages[0]), [])

    async def get_users(self, ids):
        self.calls.append(f"users:{len(ids)}")
        return [self.users[i] for i in ids if i in self.users]

    async def fetch_history(self, chat_id, backward, from_time):
        self.calls.append(f"history:{chat_id}")
        msgs = [m for m in self.history.get(chat_id, []) if from_time is None or m.time <= from_time]
        return sorted(msgs, key=lambda m: -m.time)[:backward]


def chat(cid, partner, t, ctype="DIALOG"):
    return NS(id=cid, type=ctype, participants={str(ME): 1, str(partner): 1}, last_event_time=t)


def msg(mid, sender, t, text, attaches=()):
    return NS(id=mid, sender=sender, time=t, text=text, attaches=[NS(type=a) for a in attaches])


async def _noop(_):
    return None


async def test_exports_only_debtor_dialogs_anonymized(tmp_path: Path) -> None:
    pages = [
        [chat(1, 101, 2000), chat(2, 102, 1900), chat(3, 103, 1800, ctype="CHAT")],
        [chat(4, 104, 1000)],
    ]
    users = {
        101: NS(id=101, phone=79161111111, options=[]),  # должник
        102: NS(id=102, phone=79162222222, options=[]),  # не должник
        104: NS(id=104, phone=79164444444, options=[]),  # должник (на 2-й странице)
    }
    history = {
        1: [msg(11, 101, 1_700_000_000_000, "Здравствуйте, Виталий Викторович! Это Иванова, мой тел +7 916 111-11-11"),
            msg(12, ME, 1_700_000_100_000, "Иван Иванович, пришлите справку 2-НДФЛ на ivanov@mail.ru"),
            msg(13, 101, 1_700_000_200_000, "", attaches=["PHOTO"])],
        2: [msg(21, 102, 1_700_000_000_000, "секрет")],
        4: [msg(41, 104, 1_700_000_000_000, "Когда суд? Карта 4276 1234 5678 9012")],
    }
    reader = FakeReader(pages, users, history)
    out = tmp_path / "h.jsonl"
    report = await export_history(
        reader, {"79161111111": ["A1"], "79164444444": ["A2"]},
        {"A1": "Иванов Иван Иванович", "A2": "Петров Пётр Петрович"}, out,
        manager_names=["Жалсанов Виталий Викторович"], sleep=_noop,
    )
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert report.debtor_chats == 2 and report.messages == 4 and report.dialogs == 3
    assert {r["chat"] for r in rows} == {"D001", "D002"}
    text = " ".join(r["text"] for r in rows)
    assert "секрет" not in text  # не должник не выгружен
    for leaked in ("Иванов", "Иван ", "Виталий", "916", "ivanov@", "4276"):
        assert leaked not in text, leaked
    assert "[УПРАВЛЯЮЩИЙ]" in text and "[ДОЛЖНИК]" in text and "<карта>" in text
    assert [r["role"] for r in rows if r["chat"] == "D001"] == ["должник", "управляющий", "должник"]
    assert rows[2]["attachments"] == ["PHOTO"]
    # только чтение: других вызовов нет
    assert all(re.match(r"(chats|users|history):", c) for c in reader.calls)


async def test_history_paginates_within_limit(tmp_path: Path) -> None:
    hist = {1: [msg(i, 101, 1_700_000_000_000 + i * 1000, f"сообщение {i}") for i in range(100)]}
    reader = FakeReader([[chat(1, 101, 5)]], {101: NS(id=101, phone=79161111111, options=[])}, hist)
    report = await export_history(reader, {"79161111111": ["A1"]}, {}, tmp_path / "h.jsonl", per_chat=90, sleep=_noop)
    assert report.messages == 90
    assert sum(1 for c in reader.calls if c.startswith("history")) == 3  # 40 + 40 + 10


async def test_bots_skipped(tmp_path: Path) -> None:
    reader = FakeReader([[chat(1, 101, 5)]], {101: NS(id=101, phone=79161111111, options=["BOT"])}, {})
    report = await export_history(reader, {"79161111111": ["A1"]}, {}, tmp_path / "h.jsonl", sleep=_noop)
    assert report.debtor_chats == 0


def test_anonymize() -> None:
    t = anonymize(
        "Ивановой Марии Петровне: счёт 40817810099910004312, паспорт 45 06 123456, СНИЛС 112-233-445 95",
        ["Иванова Мария Петровна"], [],
    )
    assert "Иванов" not in t and "Мари" not in t and "40817810099910004312" not in t and "123456" not in t
    assert "<счёт>" in t and "<паспорт>" in t


def test_export_module_is_read_only() -> None:
    src = (Path(__file__).resolve().parent.parent / "gateway" / "history_export.py").read_text(encoding="utf-8")
    code = src.split('"""', 2)[2]  # без docstring модуля
    assert not re.search(r"send_message|send_text|read_message|search_by_phone|\.read\(", code)
