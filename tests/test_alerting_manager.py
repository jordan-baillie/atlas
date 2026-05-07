"""Tests for alerting/manager.py (AlertManager facade).

Covers:
  1. AlertManager.send() calls utils.telegram.send_message with right args
  2. AlertManager.notify() calls utils.telegram.notify with right args
  3. Convenience methods (info/important/critical) pass correct level
  4. telegram_enabled=False → returns True without calling send
  5. Exception in underlying call → returns False, never raises
  6. get_alert_manager() is a process-wide singleton
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

_ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(_ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATLAS_ROOT))

import alerting.manager as _mod
from alerting import AlertManager, AlertLevel, get_alert_manager


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Ensure each test gets a fresh singleton."""
    orig = _mod._INSTANCE
    _mod._INSTANCE = None
    yield
    _mod._INSTANCE = orig


# ─── 1. send() routes to utils.telegram.send_message ────────────────────────


class TestAlertManagerSend:
    def test_send_calls_send_message_with_text(self):
        """send(text) calls utils.telegram.send_message(text, ...)."""
        am = AlertManager()
        mock_sm = MagicMock(return_value=True)
        with patch("utils.telegram.send_message", mock_sm):
            result = am.send("hello world")
        assert result is True
        mock_sm.assert_called_once()
        args, kwargs = mock_sm.call_args
        assert args[0] == "hello world"

    def test_send_passes_parse_mode(self):
        """send() forwards parse_mode kwarg."""
        am = AlertManager()
        mock_sm = MagicMock(return_value=True)
        with patch("utils.telegram.send_message", mock_sm):
            am.send("msg", parse_mode="MarkdownV2")
        _, kwargs = mock_sm.call_args
        assert kwargs.get("parse_mode") == "MarkdownV2"

    def test_send_passes_silent(self):
        """send(silent=True) forwards silent flag."""
        am = AlertManager()
        mock_sm = MagicMock(return_value=True)
        with patch("utils.telegram.send_message", mock_sm):
            am.send("msg", silent=True)
        _, kwargs = mock_sm.call_args
        assert kwargs.get("silent") is True

    def test_send_returns_false_on_exception(self):
        """Exception in send_message → False returned, never raises."""
        am = AlertManager()
        with patch("utils.telegram.send_message", side_effect=RuntimeError("boom")):
            result = am.send("msg")
        assert result is False

    def test_send_disabled_returns_true_without_calling_telegram(self):
        """telegram_enabled=False → True returned, send_message NOT called."""
        am = AlertManager(telegram_enabled=False)
        mock_sm = MagicMock()
        with patch("utils.telegram.send_message", mock_sm):
            result = am.send("msg")
        assert result is True
        mock_sm.assert_not_called()


# ─── 2. notify() routes to utils.telegram.notify ────────────────────────────


class TestAlertManagerNotify:
    def test_notify_calls_tg_notify_with_message(self):
        """notify(title) calls utils.telegram.notify with title as message."""
        am = AlertManager()
        mock_notify = MagicMock(return_value=True)
        with patch("utils.telegram.notify", mock_notify):
            result = am.notify("My Title")
        assert result is True
        mock_notify.assert_called_once()
        args, kwargs = mock_notify.call_args
        assert "My Title" in args[0]

    def test_notify_combines_title_and_body(self):
        """notify(title, body) produces single message containing both."""
        am = AlertManager()
        mock_notify = MagicMock(return_value=True)
        with patch("utils.telegram.notify", mock_notify):
            am.notify("The Title", "The body text")
        args, _ = mock_notify.call_args
        msg = args[0]
        assert "The Title" in msg
        assert "The body text" in msg

    def test_notify_maps_critical_level(self):
        """AlertLevel.CRITICAL maps to level='CRITICAL'."""
        am = AlertManager()
        mock_notify = MagicMock(return_value=True)
        with patch("utils.telegram.notify", mock_notify):
            am.notify("title", level=AlertLevel.CRITICAL)
        _, kwargs = mock_notify.call_args
        assert kwargs.get("level") == "CRITICAL"

    def test_notify_maps_important_level(self):
        """AlertLevel.IMPORTANT maps to level='WARNING'."""
        am = AlertManager()
        mock_notify = MagicMock(return_value=True)
        with patch("utils.telegram.notify", mock_notify):
            am.notify("title", level=AlertLevel.IMPORTANT)
        _, kwargs = mock_notify.call_args
        assert kwargs.get("level") == "WARNING"

    def test_notify_maps_info_level(self):
        """AlertLevel.INFO maps to level='INFO'."""
        am = AlertManager()
        mock_notify = MagicMock(return_value=True)
        with patch("utils.telegram.notify", mock_notify):
            am.notify("title", level=AlertLevel.INFO)
        _, kwargs = mock_notify.call_args
        assert kwargs.get("level") == "INFO"

    def test_notify_disabled_returns_true_without_calling_telegram(self):
        """telegram_enabled=False → True returned, notify NOT called."""
        am = AlertManager(telegram_enabled=False)
        mock_notify = MagicMock()
        with patch("utils.telegram.notify", mock_notify):
            result = am.notify("title", "body")
        assert result is True
        mock_notify.assert_not_called()

    def test_notify_returns_false_on_exception(self):
        """Exception in utils.telegram.notify → False, never raises."""
        am = AlertManager()
        with patch("utils.telegram.notify", side_effect=RuntimeError("fail")):
            result = am.notify("title")
        assert result is False


# ─── 3. Convenience level methods ────────────────────────────────────────────


class TestConvenienceMethods:
    def test_info_passes_info_level(self):
        """am.info() calls notify with AlertLevel.INFO."""
        am = AlertManager()
        mock_notify = MagicMock(return_value=True)
        with patch("utils.telegram.notify", mock_notify):
            am.info("title", "body")
        _, kwargs = mock_notify.call_args
        assert kwargs.get("level") == "INFO"

    def test_important_passes_warning_level(self):
        """am.important() calls notify with AlertLevel.IMPORTANT → WARNING."""
        am = AlertManager()
        mock_notify = MagicMock(return_value=True)
        with patch("utils.telegram.notify", mock_notify):
            am.important("title")
        _, kwargs = mock_notify.call_args
        assert kwargs.get("level") == "WARNING"

    def test_critical_passes_critical_level(self):
        """am.critical() calls notify with AlertLevel.CRITICAL → CRITICAL."""
        am = AlertManager()
        mock_notify = MagicMock(return_value=True)
        with patch("utils.telegram.notify", mock_notify):
            am.critical("title")
        _, kwargs = mock_notify.call_args
        assert kwargs.get("level") == "CRITICAL"


# ─── 4. telegram_enabled=False tests (comprehensive) ─────────────────────────


class TestTelegramDisabled:
    def test_all_methods_return_true_when_disabled(self):
        """All methods return True when telegram_enabled=False."""
        am = AlertManager(telegram_enabled=False)
        assert am.send("msg") is True
        assert am.notify("t", "b") is True
        assert am.info("t") is True
        assert am.important("t") is True
        assert am.critical("t") is True

    def test_no_telegram_calls_when_disabled(self):
        """No utils.telegram functions called when disabled."""
        am = AlertManager(telegram_enabled=False)
        mock_sm = MagicMock()
        mock_notify = MagicMock()
        with patch("utils.telegram.send_message", mock_sm), \
             patch("utils.telegram.notify", mock_notify):
            am.send("msg")
            am.notify("t", "b")
            am.info("x")
        mock_sm.assert_not_called()
        mock_notify.assert_not_called()


# ─── 5. Exception safety ──────────────────────────────────────────────────────


class TestExceptionSafety:
    def test_send_exception_returns_false(self):
        am = AlertManager()
        with patch("utils.telegram.send_message", side_effect=OSError("network")):
            assert am.send("msg") is False

    def test_notify_exception_returns_false(self):
        am = AlertManager()
        with patch("utils.telegram.notify", side_effect=OSError("net")):
            assert am.notify("title") is False

    def test_no_exception_propagates(self):
        """Caller never sees an exception from AlertManager methods."""
        am = AlertManager()
        with patch("utils.telegram.send_message", side_effect=Exception("boom")):
            # Should NOT raise
            result = am.send("x")
        assert result is False


# ─── 6. Singleton behaviour ───────────────────────────────────────────────────


class TestSingleton:
    def test_get_alert_manager_same_instance(self):
        """get_alert_manager() returns same instance on repeated calls."""
        a = get_alert_manager()
        b = get_alert_manager()
        assert a is b

    def test_get_alert_manager_creates_alert_manager_instance(self):
        """get_alert_manager() returns an AlertManager."""
        am = get_alert_manager()
        assert isinstance(am, AlertManager)

    def test_singleton_reset_via_module(self):
        """_INSTANCE can be overridden for testing."""
        custom = AlertManager(telegram_enabled=False)
        _mod._INSTANCE = custom
        assert get_alert_manager() is custom
