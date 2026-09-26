"""Адаптер PyMax без сети: разбор моделей по структуре из журнала этапа 0."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pymax import Message, User

from gateway.transport.pymax_transport import (
    LoginRequiredError,
    PyMaxTransport,
    RefuseAuthFlow,
    json_safe,
    make_extra_config,
    message_to_incoming,
    user_to_profile,
)

MY_ID = 3911587

# Ключи и типы — как в NOTIF_MESSAGE журнала этапа 0 (значения условные).
PHOTO_MSG = {
    "id": 117336402451831708,
    "chatId": 116679694,
    "sender": 114281389,
    "time": 1790411417059,
    "type": "USER",
    "text": "",
    "prevMessageId": 117336402358663608,
    "attaches": [
        {"_type": "PHOTO", "previewData": b"\x82\xd2\x00", "baseUrl": "u", "photoToken": "t",
         "width": 736, "height": 920, "photoId": 39518378669},
    ],
}


def test_message_with_photo_bytes_is_json_safe() -> None:
    msg = message_to_incoming(Message.model_validate(PHOTO_MSG), MY_ID)
    assert msg.chat_id == 116679694 and msg.sender_id == 114281389
    assert msg.attachment_types == ("PHOTO",)
    assert msg.is_own is False
    json.dumps(msg.raw)  # не падает на bytes (ошибка прототипа этапа 0)


def test_own_message_detected() -> None:
    data = dict(PHOTO_MSG, sender=MY_ID, attaches=[])
    assert message_to_incoming(Message.model_validate(data), MY_ID).is_own is True


def test_bot_detected_by_options() -> None:
    bot = User.model_validate({"id": 543835, "options": ["SERVICE_ACCOUNT", "TT", "ONEME", "OFFICIAL", "BOT"], "names": []})
    person = User.model_validate({"id": 114281389, "options": ["TT", "ONEME"], "phone": 79161234567, "names": []})
    assert user_to_profile(bot).is_bot is True and user_to_profile(bot).phone is None
    p = user_to_profile(person)
    assert p.is_bot is False and p.phone == "79161234567"


def test_extra_config_safe() -> None:
    cfg = make_extra_config()
    assert cfg.telemetry is False
    assert cfg.relogin is True and cfg.reconnect is True
    assert cfg.log_level == "INFO"
    assert make_extra_config(full_contacts_sync=True).sync.contacts_sync == -1


async def test_refuse_auth_flow_never_requests_sms() -> None:
    with pytest.raises(LoginRequiredError):
        await RefuseAuthFlow().authenticate(object())


async def test_run_without_session_file_refuses(tmp_path: Path) -> None:
    t = PyMaxTransport("+70000000000", tmp_path / "session")

    async def noop(*_):
        return None

    with pytest.raises(LoginRequiredError):
        await t.run(noop, noop)


def test_json_safe() -> None:
    assert json_safe({"a": b"xx", "b": [1, (2, b"y")]}) == {"a": "<bytes 2>", "b": [1, [2, "<bytes 1>"]]}


def test_send_text_only_called_from_send_guard() -> None:
    """Отправка с личного аккаунта — только через SendGuard (gateway/safety.py)."""
    root = Path(__file__).resolve().parent.parent / "gateway"
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel == "safety.py" or rel.startswith("transport/"):
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"\.send_text\(|\.send_message\(", text):
            offenders.append(rel)
    assert offenders == []


def test_no_read_marks_anywhere() -> None:
    root = Path(__file__).resolve().parent.parent / "gateway"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\.read\(\)|read_message\(|mark_read\(", text), path


def test_link_survives_pymax_markdown() -> None:
    """Ссылка с «_» в нике бота доходит до MAX целиком (элемент LINK)."""
    from pymax.formatting.markdown import Formatter

    from gateway.tokens import deep_link, new_token

    url = deep_link("id123_test_bot", new_token())
    t = PyMaxTransport("+70000000000", Path("/nonexistent"))
    clean, elements = Formatter.format_markdown(f"Пишите в бот: {t.format_link(url)}")
    assert url in clean
    assert [e.attributes.url for e in elements if e.type == "LINK"] == [url]
    # без обёртки PyMax портит адрес — поэтому обёртка обязательна
    assert url not in Formatter.format_markdown(url)[0]
