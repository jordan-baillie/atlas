"""Tests for telegram_messages capture (outbound + inbound).

Relies on session+function autouse _isolate_prod_db fixtures in conftest.py —
all DB writes go to per-test tmp SQLite, never to production atlas.db.
"""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from db import atlas_db


# ─── Outbound ───────────────────────────────────────────────────────────────

def _mock_success_response(message_id: int = 99):
    """Build a context-manager-compatible mock for urlopen success."""
    resp = MagicMock()
    resp.read.return_value = json.dumps({
        "ok": True,
        "result": {"message_id": message_id, "chat": {"id": 12345}, "text": "ok"},
    }).encode("utf-8")
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=resp)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


@pytest.fixture
def _telegram_creds(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")


def test_outbound_success_persisted(_telegram_creds):
    from utils import telegram

    with patch("utils.telegram.urllib.request.urlopen", return_value=_mock_success_response(message_id=777)):
        ok = telegram.send_message("hello world", parse_mode="HTML")

    assert ok is True
    with atlas_db.get_db() as db:
        rows = db.execute("SELECT * FROM telegram_messages WHERE direction='outbound'").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["chat_id"] == "12345"
    assert row["body"] == "hello world"
    assert row["parse_mode"] == "HTML"
    assert row["message_id"] == 777
    assert row["api_status"] == 200
    assert row["api_error"] is None
    assert row["direction"] == "outbound"
    assert row["is_command"] == 0


def test_outbound_http_error_persisted(_telegram_creds):
    from utils import telegram

    http_err = urllib.error.HTTPError(
        url="https://api.telegram.org/x", code=429, msg="Too Many Requests",
        hdrs=None, fp=io.BytesIO(b'{"description":"rate limited"}')
    )
    with patch("utils.telegram.urllib.request.urlopen", side_effect=http_err):
        ok = telegram.send_message("rate-limited body")

    assert ok is False
    with atlas_db.get_db() as db:
        rows = db.execute("SELECT * FROM telegram_messages WHERE direction='outbound'").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["body"] == "rate-limited body"
    assert row["api_status"] == 429
    assert row["api_error"] is not None
    assert "rate limited" in row["api_error"]
    assert row["message_id"] is None


def test_outbound_generic_exception_persisted(_telegram_creds):
    from utils import telegram

    with patch("utils.telegram.urllib.request.urlopen", side_effect=ConnectionError("network down")):
        ok = telegram.send_message("net-fail body")

    assert ok is False
    with atlas_db.get_db() as db:
        rows = db.execute("SELECT * FROM telegram_messages WHERE direction='outbound'").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["body"] == "net-fail body"
    assert row["api_status"] is None
    assert row["api_error"] is not None
    assert "ConnectionError" in row["api_error"]
    assert "network down" in row["api_error"]


def test_outbound_db_failure_failopen_does_not_break_send(_telegram_creds):
    """If DB persistence fails, send_message must still return True on success."""
    from utils import telegram

    def boom(*a, **kw):
        raise RuntimeError("simulated DB crash")

    with patch("utils.telegram.urllib.request.urlopen", return_value=_mock_success_response()), \
         patch("db.atlas_db.record_telegram_outbound", side_effect=boom):
        ok = telegram.send_message("fail-open test")

    # Send pipeline unaffected by capture failure
    assert ok is True


def test_outbound_credentials_missing_does_not_persist(monkeypatch):
    """ValueError on missing creds returns False before any send/persist attempt."""
    from utils import telegram

    # Clear env + point secrets file at nonexistent path
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(telegram, "SECRETS_PATH", telegram.Path("/nonexistent/path/secrets.json"))

    ok = telegram.send_message("never sent")
    assert ok is False
    with atlas_db.get_db() as db:
        rows = db.execute("SELECT * FROM telegram_messages").fetchall()
    assert rows == []


def test_outbound_indexes_used():
    """Verify both indexes exist and are used by EXPLAIN QUERY PLAN."""
    with atlas_db.get_db() as db:
        # Seed 50 rows
        for i in range(50):
            db.execute(
                "INSERT INTO telegram_messages (direction, chat_id, body, sent_at) "
                "VALUES (?, '12345', ?, ?)",
                ("outbound" if i % 2 == 0 else "inbound", f"msg-{i}", f"2026-05-12T00:00:{i:02d}Z")
            )

        plan_chat = db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM telegram_messages WHERE chat_id='12345' ORDER BY sent_at DESC LIMIT 10"
        ).fetchall()
        plan_dir = db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM telegram_messages WHERE direction='outbound' ORDER BY sent_at DESC LIMIT 10"
        ).fetchall()

    plan_chat_text = " ".join(str(r["detail"]) if "detail" in r.keys() else str(tuple(r)) for r in plan_chat)
    plan_dir_text = " ".join(str(r["detail"]) if "detail" in r.keys() else str(tuple(r)) for r in plan_dir)

    assert "idx_tgm_chat_time" in plan_chat_text, f"chat_id query did not hit index: {plan_chat_text}"
    assert "idx_tgm_direction_time" in plan_dir_text, f"direction query did not hit index: {plan_dir_text}"


# ─── Inbound ────────────────────────────────────────────────────────────────

@pytest.fixture
def _bot_module():
    """Import the bot module; tolerate import-time side effects."""
    import importlib
    mod = importlib.import_module("services.telegram_bot")
    return mod


def _make_update(*, text=None, caption=None, message_id=42, chat_id=12345,
                 user_id=999, username="testuser", date_iso="2026-05-12T10:00:00+00:00"):
    """Build a minimal duck-typed Update for capture_inbound_message."""
    from datetime import datetime as _dt
    msg = MagicMock()
    msg.text = text
    msg.caption = caption
    msg.message_id = message_id
    msg.effective_attachment = None
    msg.date = _dt.fromisoformat(date_iso)

    chat = MagicMock()
    chat.id = chat_id

    user = MagicMock()
    user.id = user_id
    user.username = username

    update = MagicMock()
    update.effective_message = msg
    update.effective_chat = chat
    update.effective_user = user
    return update


def test_inbound_command_captured(_bot_module):
    import asyncio
    update = _make_update(text="/status")
    asyncio.run(_bot_module.capture_inbound_message(update, None))

    with atlas_db.get_db() as db:
        rows = db.execute("SELECT * FROM telegram_messages WHERE direction='inbound'").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["body"] == "/status"
    assert row["is_command"] == 1
    assert row["command_name"] == "status"
    assert row["chat_id"] == "12345"
    assert row["user_id"] == "999"
    assert row["username"] == "testuser"
    assert row["message_id"] == 42


def test_inbound_command_with_bot_suffix_captured(_bot_module):
    import asyncio
    update = _make_update(text="/halt@AtlasBot now")
    asyncio.run(_bot_module.capture_inbound_message(update, None))

    with atlas_db.get_db() as db:
        rows = db.execute("SELECT * FROM telegram_messages WHERE direction='inbound'").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["is_command"] == 1
    assert row["command_name"] == "halt"
    assert row["body"] == "/halt@AtlasBot now"


def test_inbound_free_text_captured(_bot_module):
    import asyncio
    update = _make_update(text="hello orchestrator")
    asyncio.run(_bot_module.capture_inbound_message(update, None))

    with atlas_db.get_db() as db:
        rows = db.execute("SELECT * FROM telegram_messages WHERE direction='inbound'").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["body"] == "hello orchestrator"
    assert row["is_command"] == 0
    assert row["command_name"] is None


def test_inbound_media_with_caption_captured(_bot_module):
    import asyncio
    update = _make_update(text=None, caption="screenshot of alert")
    asyncio.run(_bot_module.capture_inbound_message(update, None))

    with atlas_db.get_db() as db:
        rows = db.execute("SELECT body, is_command FROM telegram_messages WHERE direction='inbound'").fetchall()
    assert len(rows) == 1
    assert dict(rows[0])["body"] == "screenshot of alert"
    assert dict(rows[0])["is_command"] == 0


def test_inbound_db_failure_failopen(_bot_module):
    """If DB write fails, handler must not raise — bot stays alive."""
    import asyncio
    update = _make_update(text="/test")
    with patch("db.atlas_db.record_telegram_inbound", side_effect=RuntimeError("boom")):
        # Must NOT raise
        asyncio.run(_bot_module.capture_inbound_message(update, None))


def test_inbound_no_text_no_caption_still_records_placeholder(_bot_module):
    import asyncio
    # Media-only message with no text/caption
    update = _make_update(text=None, caption=None)
    update.effective_message.effective_attachment = MagicMock()
    update.effective_message.effective_attachment.__class__.__name__ = "PhotoSize"
    asyncio.run(_bot_module.capture_inbound_message(update, None))

    with atlas_db.get_db() as db:
        rows = db.execute("SELECT body FROM telegram_messages WHERE direction='inbound'").fetchall()
    assert len(rows) == 1
    assert "media" in dict(rows[0])["body"].lower()
