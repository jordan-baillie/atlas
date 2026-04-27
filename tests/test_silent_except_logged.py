"""Regression tests: silent except blocks in operational scripts are now logged.

Strategy
--------
Most of the A2 spec sites were ALREADY fixed before this test suite was added.
For the sites that are callable in isolation we use functional tests (monkeypatch +
caplog).  For sites inside complex pipelines (crash-notify guard, main() block) we
fall back to *shape checks* — assert that the source text near the exception
contains ``logger.warning(`` and does NOT contain a bare ``pass`` as the sole
handler.

Items covered
-------------
A2.1  eod_settlement.py      — crash-notify telegram fallback (already fixed)
A2.2  execute_approved.py    — _notify_execution / _notify_auto_approve (already fixed)
A2.3  reconcile_positions.py — send_telegram_summary / broker.disconnect (already fixed)
A2.4  sync_protective_orders.py — _maybe_alert_stuck telegram / broker.disconnect (already fixed)
Extra eod_settlement.py      — RegimeModel classification fallback (newly logged)
Extra eod_settlement.py      — timezone detection fallback (newly logged)
Extra reconcile_positions.py — setup_logging fallback (newly logged)
Extra sync_protective_orders.py — setup_logging fallback (newly logged)
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT / "scripts"


def _source(fname: str) -> str:
    return (SCRIPTS / fname).read_text()


def _window(src: str, needle: str, before: int = 5, after: int = 12) -> str:
    """Return a window of lines around the first occurrence of *needle*."""
    lines = src.splitlines()
    for i, line in enumerate(lines):
        if needle in line:
            start = max(0, i - before)
            end = min(len(lines), i + after)
            return "\n".join(lines[start:end])
    return ""


def _load_script(fname: str):
    """Load a scripts/*.py file as a module, adding scripts/ to sys.path."""
    fpath = SCRIPTS / fname
    spec = importlib.util.spec_from_file_location(
        fpath.stem, str(fpath),
        submodule_search_locations=[],
    )
    mod = importlib.util.module_from_spec(spec)
    # Temporarily add scripts dir to path so the module's relative imports work
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec.loader.exec_module(mod)
    return mod


# ═══════════════════════════════════════════════════════════════════════════
# A2.1 — eod_settlement.py crash-notify telegram fallback
# ═══════════════════════════════════════════════════════════════════════════

class TestA21EodSettlementCrashNotify:
    """A2.1 — eod_settlement.py crash-notify telegram fallback (shape check)."""

    def test_crash_notify_has_warning_log(self):
        src = _source("eod_settlement.py")
        window = _window(src, "eod_settlement CRASHED", before=2, after=15)
        assert "log.warning(" in window, (
            "eod_settlement crash-notify: expected log.warning() near 'CRASHED' block, "
            f"got:\n{window}"
        )

    def test_crash_notify_no_bare_pass(self):
        src = _source("eod_settlement.py")
        window = _window(src, "eod_settlement CRASHED", before=2, after=15)
        lines = [ln.strip() for ln in window.splitlines()]
        bare_passes = [ln for ln in lines if ln == "pass"]
        assert not bare_passes, (
            f"eod_settlement crash-notify: bare 'pass' found in exception block:\n{window}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# A2.2 — execute_approved.py notify functions
# ═══════════════════════════════════════════════════════════════════════════

class TestA22ExecuteApprovedNotifyExecution:
    """A2.2 — execute_approved._notify_execution telegram fallback."""

    def test_notify_execution_logs_telegram_failure(self, caplog):
        """Monkeypatch send_message to raise; assert WARNING is emitted."""
        ea = _load_script("execute_approved.py")
        with patch("utils.telegram.send_message", side_effect=RuntimeError("sim-tg-fail")):
            with caplog.at_level(logging.WARNING):
                ea._notify_execution(
                    market_id="sp500",
                    trade_date="2099-01-01",
                    report={
                        "successful_entries": 1,
                        "successful_exits": 0,
                        "total_entries": 1,
                        "total_exits": 0,
                        "entries": [
                            {
                                "ticker": "AAPL",
                                "status": "ok",
                                "price": 100.0,
                                "qty": 1,
                                "success": True,
                            }
                        ],
                    },
                )
        msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "telegram" in m.lower() or "notification" in m.lower()
            for m in msgs
        ), f"_notify_execution: expected Telegram failure warning, got: {msgs}"

    def test_notify_execution_shape_has_warning(self):
        src = _source("execute_approved.py")
        window = _window(src, "def _notify_execution", before=0, after=55)
        assert "log.warning(" in window, (
            "_notify_execution: expected log.warning() in function body"
        )


class TestA22ExecuteApprovedNotifyAutoApprove:
    """A2.2 — execute_approved._notify_auto_approve telegram fallback."""

    def test_notify_auto_approve_logs_telegram_failure(self, caplog):
        ea = _load_script("execute_approved.py")
        with patch("utils.telegram.send_message", side_effect=RuntimeError("sim-tg-fail")):
            with caplog.at_level(logging.WARNING):
                ea._notify_auto_approve(
                    market_id="sp500",
                    trade_date="2099-01-01",
                    n_entries=1,
                    n_exits=0,
                )
        msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "telegram" in m.lower() or "notification" in m.lower()
            for m in msgs
        ), f"_notify_auto_approve: expected Telegram failure warning, got: {msgs}"

    def test_notify_auto_approve_shape_has_warning(self):
        src = _source("execute_approved.py")
        window = _window(src, "def _notify_auto_approve", before=0, after=30)
        assert "log.warning(" in window


# ═══════════════════════════════════════════════════════════════════════════
# A2.3 — reconcile_positions.py telegram + disconnect
# ═══════════════════════════════════════════════════════════════════════════

class TestA23ReconcilePositionsTelegram:
    """A2.3 — reconcile_positions.send_telegram_summary telegram fallback."""

    def test_send_telegram_summary_logs_failure(self, caplog):
        rp = _load_script("reconcile_positions.py")
        dummy_result = {
            "market_id": "sp500",
            "summary": {
                "internal_count": 1, "broker_count": 1,
                "phantom": 0, "untracked": 0, "mismatch": 0, "drift": 0,
            },
            "discrepancies": [],
            "fixed": False,
        }
        with patch("utils.telegram.send_message", side_effect=RuntimeError("sim-tg-fail")):
            with caplog.at_level(logging.WARNING):
                result = rp.send_telegram_summary(dummy_result, fixed=False)
        assert result is False
        msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "telegram" in m.lower() or "send" in m.lower()
            for m in msgs
        ), f"send_telegram_summary: expected Telegram warning, got: {msgs}"

    def test_send_telegram_summary_shape_has_warning(self):
        src = _source("reconcile_positions.py")
        window = _window(src, "def send_telegram_summary", before=0, after=15)
        assert "logger.warning(" in window


class TestA23ReconcilePositionsDisconnect:
    """A2.3 — reconcile_positions.py broker.disconnect fallback (shape check)."""

    def test_disconnect_shape_has_warning(self):
        src = _source("reconcile_positions.py")
        window = _window(src, "broker.disconnect()", before=3, after=6)
        assert "logger.warning(" in window, (
            "reconcile_positions: expected logger.warning() near broker.disconnect(), "
            f"got:\n{window}"
        )

    def test_disconnect_no_bare_pass(self):
        src = _source("reconcile_positions.py")
        window = _window(src, "broker.disconnect()", before=3, after=6)
        lines = [ln.strip() for ln in window.splitlines()]
        assert "pass" not in lines, (
            f"reconcile_positions: bare 'pass' found near broker.disconnect():\n{window}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# A2.4 — sync_protective_orders.py telegram + disconnect
# ═══════════════════════════════════════════════════════════════════════════

class TestA24SyncProtectiveTelegram:
    """A2.4 — sync_protective_orders._maybe_alert_stuck telegram fallback."""

    def test_maybe_alert_stuck_has_warning(self):
        src = _source("sync_protective_orders.py")
        # The function is ~45 lines long; use a generous after window
        window = _window(src, "def _maybe_alert_stuck", before=0, after=50)
        assert "logger.warning(" in window, (
            "_maybe_alert_stuck: expected logger.warning() in function body. "
            f"Window:\n{window}"
        )

    def test_maybe_alert_stuck_no_bare_pass(self):
        src = _source("sync_protective_orders.py")
        window = _window(src, "def _maybe_alert_stuck", before=0, after=50)
        lines = [ln.strip() for ln in window.splitlines()]
        # The warning log line should be present; a bare 'pass' should not
        assert "pass" not in lines, (
            f"sync_protective_orders: bare 'pass' in _maybe_alert_stuck block:\n{window}"
        )


class TestA24SyncProtectiveDisconnect:
    """A2.4 — sync_protective_orders.py broker.disconnect fallback (shape check)."""

    def test_disconnect_shape_has_warning(self):
        src = _source("sync_protective_orders.py")
        window = _window(src, "broker.disconnect()", before=3, after=6)
        assert "logger.warning(" in window, (
            "sync_protective_orders: expected logger.warning() near broker.disconnect()"
        )

    def test_disconnect_no_bare_pass(self):
        src = _source("sync_protective_orders.py")
        window = _window(src, "broker.disconnect()", before=3, after=6)
        lines = [ln.strip() for ln in window.splitlines()]
        assert "pass" not in lines


# ═══════════════════════════════════════════════════════════════════════════
# Extra — value-fallback blocks that were silently swallowing exceptions
# ═══════════════════════════════════════════════════════════════════════════

class TestExtraEodRegimeModelFallback:
    """eod_settlement.py — RegimeModel classification fallback now logs at DEBUG."""

    def test_stop_loss_regime_fallback_has_debug(self):
        src = _source("eod_settlement.py")
        assert "RegimeModel classification failed" in src, (
            "eod_settlement.py: expected 'RegimeModel classification failed' debug message"
        )

    def test_regime_fallback_not_bare_except(self):
        import re
        src = _source("eod_settlement.py")
        silent = re.findall(
            r"except Exception:\s*\n\s*_eod_regime = None",
            src,
        )
        assert not silent, (
            f"eod_settlement.py: found {len(silent)} bare 'except Exception: "
            f"_eod_regime=None' block(s) — should have 'as _re' + debug log"
        )


class TestExtraEodTimezoneFallback:
    """eod_settlement.py — timezone detection fallback now logs at DEBUG."""

    def test_timezone_fallback_has_debug(self):
        src = _source("eod_settlement.py")
        assert "Could not detect operator timezone" in src, (
            "eod_settlement.py: expected 'Could not detect operator timezone' debug message"
        )

    def test_timezone_fallback_not_bare_except(self):
        import re
        src = _source("eod_settlement.py")
        silent = re.findall(
            r"except Exception:\s*\n\s*_settle_tz = BRISBANE",
            src,
        )
        assert not silent, (
            "eod_settlement.py: bare 'except Exception: _settle_tz=BRISBANE' still present"
        )


class TestExtraSetupLoggingFallback:
    """reconcile_positions.py + sync_protective_orders.py setup_logging fallback."""

    def test_reconcile_setup_logging_fallback_has_warning(self):
        src = _source("reconcile_positions.py")
        assert "setup_logging failed, using basicConfig fallback" in src, (
            "reconcile_positions.py: expected warning about setup_logging fallback"
        )

    def test_sync_protective_setup_logging_fallback_has_warning(self):
        src = _source("sync_protective_orders.py")
        assert "setup_logging failed, using basicConfig fallback" in src, (
            "sync_protective_orders.py: expected warning about setup_logging fallback"
        )

    def test_reconcile_no_bare_except_setup_logging(self):
        import re
        src = _source("reconcile_positions.py")
        silent = re.findall(
            r'except Exception:\s*\n\s*logging\.basicConfig',
            src,
        )
        assert not silent, (
            "reconcile_positions.py: bare except after setup_logging still present"
        )

    def test_sync_protective_no_bare_except_setup_logging(self):
        import re
        src = _source("sync_protective_orders.py")
        silent = re.findall(
            r'except Exception:\s*\n\s*logging\.basicConfig',
            src,
        )
        assert not silent, (
            "sync_protective_orders.py: bare except after setup_logging still present"
        )
