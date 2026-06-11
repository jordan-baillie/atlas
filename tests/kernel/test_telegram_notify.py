"""Tests for the atlas.kernel.notify.notify() convenience wrapper.

Covers the string-level prefix logic, exception handling, and return
value passthrough.  All tests mock send_message() to avoid real network
calls.
"""
from __future__ import annotations

from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_notify():
    from atlas.kernel.notify import notify
    return notify


# ---------------------------------------------------------------------------
# No-level behaviour (pure pass-through)
# ---------------------------------------------------------------------------

class TestNotifyNoLevel:
    def test_no_prefix_when_level_is_none(self):
        """notify(msg) with no level sends the message unchanged."""
        notify = _import_notify()
        with patch("atlas.kernel.notify.send_message", return_value=True) as mock_send:
            result = notify("hello world")
        mock_send.assert_called_once_with("hello world", parse_mode="HTML")
        assert result is True

    def test_no_prefix_when_level_empty_string(self):
        """Passing level='' is treated the same as level=None."""
        notify = _import_notify()
        with patch("atlas.kernel.notify.send_message", return_value=True) as mock_send:
            result = notify("plain message", level="")
        mock_send.assert_called_once_with("plain message", parse_mode="HTML")
        assert result is True

    def test_category_does_not_affect_message(self):
        """category= is only used in log lines, not in the message body."""
        notify = _import_notify()
        with patch("atlas.kernel.notify.send_message", return_value=True) as mock_send:
            notify("msg", category="health")
        mock_send.assert_called_once_with("msg", parse_mode="HTML")

    def test_returns_false_when_send_message_returns_false(self):
        """Passes through send_message's False return value."""
        notify = _import_notify()
        with patch("atlas.kernel.notify.send_message", return_value=False):
            result = notify("msg")
        assert result is False


# ---------------------------------------------------------------------------
# Level prefix injection
# ---------------------------------------------------------------------------

class TestNotifyLevelPrefix:
    def test_critical_prefix(self):
        """CRITICAL level prepends 🚨 emoji."""
        notify = _import_notify()
        with patch("atlas.kernel.notify.send_message", return_value=True) as mock_send:
            notify("system down", level="CRITICAL")
        sent_text = mock_send.call_args[0][0]
        assert sent_text.startswith("🚨 ")
        assert "system down" in sent_text

    def test_warning_prefix(self):
        """WARNING level prepends ⚠️ emoji."""
        notify = _import_notify()
        with patch("atlas.kernel.notify.send_message", return_value=True) as mock_send:
            notify("stale data", level="WARNING")
        sent_text = mock_send.call_args[0][0]
        assert sent_text.startswith("⚠️ ")
        assert "stale data" in sent_text

    def test_info_prefix(self):
        """INFO level prepends ℹ️ emoji."""
        notify = _import_notify()
        with patch("atlas.kernel.notify.send_message", return_value=True) as mock_send:
            notify("all good", level="INFO")
        sent_text = mock_send.call_args[0][0]
        assert sent_text.startswith("ℹ️ ")
        assert "all good" in sent_text

    def test_unknown_level_no_prefix(self):
        """Unrecognised level strings produce no prefix."""
        notify = _import_notify()
        with patch("atlas.kernel.notify.send_message", return_value=True) as mock_send:
            notify("message", level="DEBUG")
        sent_text = mock_send.call_args[0][0]
        assert sent_text == "message"

    def test_integer_level_no_prefix(self):
        """Integer levels (legacy smart_notify constants) pass through without prefix."""
        notify = _import_notify()
        with patch("atlas.kernel.notify.send_message", return_value=True) as mock_send:
            notify("msg", level=2)  # 2 == old INFO constant
        sent_text = mock_send.call_args[0][0]
        assert sent_text == "msg"  # no prefix because 2 not in LEVEL_PREFIX dict


# ---------------------------------------------------------------------------
# parse_mode passthrough
# ---------------------------------------------------------------------------

class TestNotifyParseMode:
    def test_default_parse_mode_html(self):
        """Default parse_mode is HTML."""
        notify = _import_notify()
        with patch("atlas.kernel.notify.send_message", return_value=True) as mock_send:
            notify("test")
        _, kwargs = mock_send.call_args
        assert kwargs.get("parse_mode") == "HTML"

    def test_custom_parse_mode(self):
        """Custom parse_mode is forwarded to send_message."""
        notify = _import_notify()
        with patch("atlas.kernel.notify.send_message", return_value=True) as mock_send:
            notify("test", parse_mode="MarkdownV2")
        _, kwargs = mock_send.call_args
        assert kwargs.get("parse_mode") == "MarkdownV2"

    def test_empty_parse_mode(self):
        """parse_mode='' is forwarded (plain text mode)."""
        notify = _import_notify()
        with patch("atlas.kernel.notify.send_message", return_value=True) as mock_send:
            notify("test", parse_mode="")
        _, kwargs = mock_send.call_args
        assert kwargs.get("parse_mode") == ""


# ---------------------------------------------------------------------------
# Exception handling
# ---------------------------------------------------------------------------

class TestNotifyExceptionHandling:
    def test_exception_in_send_message_returns_false(self):
        """If send_message raises, notify() catches it and returns False."""
        notify = _import_notify()
        with patch("atlas.kernel.notify.send_message", side_effect=RuntimeError("network error")):
            result = notify("msg")
        assert result is False

    def test_exception_does_not_propagate(self):
        """send_message exceptions are swallowed -- notify() never raises."""
        notify = _import_notify()
        with patch("atlas.kernel.notify.send_message", side_effect=ValueError("bad creds")):
            try:
                result = notify("msg")
            except Exception:
                assert False, "notify() should not propagate exceptions"
        assert result is False

    def test_exception_logged_as_warning(self, caplog):
        """Exception details are logged at WARNING level for observability."""
        import logging
        notify = _import_notify()
        with patch("atlas.kernel.notify.send_message", side_effect=OSError("timeout")):
            with caplog.at_level(logging.WARNING, logger="atlas.kernel.notify"):
                notify("msg", category="health", level="WARNING")
        assert any("telegram.notify failed" in r.message for r in caplog.records)

    def test_category_appears_in_warning_log(self, caplog):
        """category= value appears in the warning log line for easier debugging."""
        import logging
        notify = _import_notify()
        with patch("atlas.kernel.notify.send_message", side_effect=ConnectionError("down")):
            with caplog.at_level(logging.WARNING, logger="atlas.kernel.notify"):
                notify("msg", category="fred_health")
        assert any("fred_health" in r.message for r in caplog.records)
