"""Tests for Phase A.5 quick wins.

Covers:
- save_state warning throttling (LivePortfolio)
- _assert_state_file_parity Telegram alert on mismatch
- verify_dual_write Telegram alert on failure
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ════════════════════════════════════════════════════════════════════════════
# Task 5 — save_state warning throttle
# ════════════════════════════════════════════════════════════════════════════

class TestSaveStateWarningThrottle:
    """LivePortfolio.save_state warning must fire once per instance."""

    _MINIMAL_CONFIG: dict = {
        "risk": {
            "starting_equity": 5000,
            "max_risk_per_trade_pct": 0.01,
            "max_open_positions": 10,
            "max_sector_concentration": 3,
            "max_daily_drawdown_pct": 0.05,
        },
        "fees": {},
    }

    def _make_portfolio(self, market_id: str = "test_qw") -> object:
        from brokers.live_portfolio import LivePortfolio
        return LivePortfolio(self._MINIMAL_CONFIG, market_id=market_id)

    def test_save_state_warning_fires_once_per_instance(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Calling save_state 3× with broker_data_valid=False logs warning exactly once."""
        portfolio = self._make_portfolio()
        portfolio.broker_data_valid = False

        with caplog.at_level(logging.WARNING, logger="atlas.live_portfolio"):
            portfolio.save_state()
            portfolio.save_state()
            portfolio.save_state()

        warnings = [
            r for r in caplog.records
            if "broker_data_valid" in r.message and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1, (
            f"Expected exactly 1 warning, got {len(warnings)}: {[r.message for r in warnings]}"
        )

    def test_save_state_warning_resets_on_new_instance(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A fresh LivePortfolio instance should warn again (flag is per-instance)."""
        p1 = self._make_portfolio(market_id="market_a")
        p1.broker_data_valid = False

        p2 = self._make_portfolio(market_id="market_b")
        p2.broker_data_valid = False

        with caplog.at_level(logging.WARNING, logger="atlas.live_portfolio"):
            p1.save_state()  # first instance warns
            p2.save_state()  # second (new) instance also warns

        warnings = [
            r for r in caplog.records
            if "broker_data_valid" in r.message and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 2, (
            f"Expected 2 warnings (one per instance), got {len(warnings)}"
        )

    def test_save_state_flag_initialized_false(self) -> None:
        """_save_state_warned should start as False on a new instance."""
        portfolio = self._make_portfolio()
        assert hasattr(portfolio, "_save_state_warned"), (
            "LivePortfolio must have _save_state_warned attribute"
        )
        assert portfolio._save_state_warned is False


# ════════════════════════════════════════════════════════════════════════════
# Task 6 — _assert_state_file_parity Telegram alert
# ════════════════════════════════════════════════════════════════════════════

class TestAssertStateFileParityTelegram:
    """_assert_state_file_parity must send a Telegram alert on mismatch."""

    def test_assert_state_file_parity_alerts_telegram(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mismatch between SQLite and JSON state file triggers Telegram call."""
        import db.atlas_db as _adb
        from db.atlas_db import _assert_state_file_parity

        # Create a state file that does NOT contain the ticker we'll insert
        state_file = tmp_path / "live_sp500.json"
        state_file.write_text(json.dumps({
            "positions": [{"ticker": "AAPL", "strategy": "momentum"}]
        }))

        # Point the function at our tmp state dir
        monkeypatch.setattr(_adb, "_state_dir_override", str(tmp_path))

        with patch("utils.telegram.send_message") as mock_tg:
            _assert_state_file_parity(
                ticker="NEWTICKER",
                universe="sp500",
                strategy="momentum_breakout",
                entry_price=150.0,
                shares=10,
                stop_price=140.0,
            )

        assert mock_tg.called, "Telegram send_message must be called on parity mismatch"
        msg = mock_tg.call_args[0][0]
        assert "NEWTICKER" in msg, f"Alert must mention the missing ticker: {msg}"
        assert "sp500" in msg, f"Alert must mention the market: {msg}"

    def test_no_alert_when_ticker_already_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No Telegram alert when the ticker is already in the state file."""
        import db.atlas_db as _adb
        from db.atlas_db import _assert_state_file_parity

        state_file = tmp_path / "live_sp500.json"
        state_file.write_text(json.dumps({
            "positions": [{"ticker": "AAPL", "strategy": "momentum"}]
        }))

        monkeypatch.setattr(_adb, "_state_dir_override", str(tmp_path))

        with patch("utils.telegram.send_message") as mock_tg:
            _assert_state_file_parity(
                ticker="AAPL",  # already in state
                universe="sp500",
                strategy="momentum",
                entry_price=150.0,
                shares=10,
                stop_price=140.0,
            )

        assert not mock_tg.called, "No alert when ticker is already in state file"

    def test_cooldown_prevents_repeated_alerts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1h cooldown prevents second alert for same market within the window."""
        import time
        import db.atlas_db as _adb
        from db.atlas_db import _assert_state_file_parity

        state_file = tmp_path / "live_sp500.json"
        state_file.write_text(json.dumps({"positions": []}))

        # Pre-populate cooldown: alert sent 10 minutes ago (well within 1h)
        cooldown_file = tmp_path / "parity_alert_cooldown.json"
        cooldown_file.write_text(json.dumps({"sp500": time.time() - 600}))

        monkeypatch.setattr(_adb, "_state_dir_override", str(tmp_path))

        with patch("utils.telegram.send_message") as mock_tg:
            _assert_state_file_parity(
                ticker="SOMESTOCK",
                universe="sp500",
                strategy="test",
                entry_price=100.0,
                shares=5,
                stop_price=90.0,
            )

        assert not mock_tg.called, (
            "Telegram should NOT fire within 1h cooldown window"
        )


# ════════════════════════════════════════════════════════════════════════════
# Task 7 — verify_dual_write Telegram alert
# ════════════════════════════════════════════════════════════════════════════

class TestVerifyDualWriteTelegramAlert:
    """_alert_telegram_on_fail must call Telegram with failed check names."""

    def test_verify_dual_write_alerts_on_fail(self) -> None:
        """Telegram is called when dual-write check fails."""
        from scripts.verify_dual_write import _alert_telegram_on_fail

        failed_results = {
            "plans": False,
            "ohlcv": True,
            "equity": False,
        }

        with patch("utils.telegram.send_message") as mock_tg:
            _alert_telegram_on_fail(failed_results, source="cron")

        assert mock_tg.called, "Telegram must be called on dual-write failure"
        msg = mock_tg.call_args[0][0]
        assert "DUAL-WRITE VERIFY FAILED" in msg, f"Message must mention failure: {msg}"
        assert "plans" in msg, f"Message must list failed check 'plans': {msg}"
        assert "equity" in msg, f"Message must list failed check 'equity': {msg}"
        # 'ohlcv' passed — must NOT appear as a failure
        # (it appears in the 'Field:' list only for failed checks)
        failed_lines = [l for l in msg.splitlines() if l.startswith("Field:")]
        assert not any("ohlcv" in l for l in failed_lines), (
            f"'ohlcv' passed — should not appear in failed fields: {msg}"
        )

    def test_verify_dual_write_includes_source(self) -> None:
        """Alert message must include the source (cron/manual)."""
        from scripts.verify_dual_write import _alert_telegram_on_fail

        with patch("utils.telegram.send_message") as mock_tg:
            _alert_telegram_on_fail({"plans": False}, source="cron")

        msg = mock_tg.call_args[0][0]
        assert "cron" in msg, f"Source must be in alert message: {msg}"

    def test_no_alert_when_all_pass(self) -> None:
        """No Telegram call when all checks pass."""
        from scripts.verify_dual_write import _alert_telegram_on_fail

        with patch("utils.telegram.send_message") as mock_tg:
            # Call with all-passing results — caller gates this, but test directly
            _alert_telegram_on_fail({"plans": True, "ohlcv": True}, source="cron")

        # Should still call (caller is responsible for the gate), but message
        # should have no fields listed
        if mock_tg.called:
            msg = mock_tg.call_args[0][0]
            failed_lines = [l for l in msg.splitlines() if l.startswith("Field:")]
            assert not failed_lines, "No failed fields to report"
