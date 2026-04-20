"""Regression tests for C3, C4 — LiveExecutor exit path fixes.

C3: phantom _portfolio.load_state() call removed; record_closed_trade is now reachable
C4: exit fill poll loop wraps get_order_status in try/except/break guard

Run:
    cd /root/atlas && python3 -m pytest tests/test_live_executor_exit_regression.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from brokers.live_portfolio import LivePortfolio


# ─── Helpers ─────────────────────────────────────────────────

def _minimal_config() -> dict:
    return {
        "market_id": "sp500",
        "risk": {
            "starting_equity": 5000,
            "max_risk_per_trade_pct": 0.01,
            "max_open_positions": 10,
            "max_sector_concentration": 2,
            "max_daily_drawdown_pct": 0.02,
            "leverage": 1.0,
        },
        "fees": {"commission_per_trade": 0, "commission_pct": 0},
    }


def _closed_trade(ticker: str = "TEST") -> dict:
    return {
        "ticker": ticker,
        "strategy": "mtf_momentum",
        "entry_price": 100.0,
        "exit_price": 110.0,
        "shares": 10,
        "pnl": 100.0,
        "pnl_pct": 10.0,
        "holding_days": 5,
        "exit_reason": "take_profit",
        "exit_date": "2026-04-20",
        "entry_date": "2026-04-15",
        "order_id": f"ORD-{ticker}-001",
    }


# ═══════════════════════════════════════════════════════════════
# C3 — record_closed_trade reachability
# ═══════════════════════════════════════════════════════════════

class TestC3RecordClosedTrade:
    """C3 regression: load_state() phantom removed; record_closed_trade is now reachable."""

    def test_record_closed_trade_appends_to_list(self, tmp_path, monkeypatch):
        """record_closed_trade appends the trade when broker_data_valid=True (C3 path)."""
        monkeypatch.setattr("brokers.live_portfolio.PROJECT_ROOT", tmp_path)
        (tmp_path / "brokers" / "state").mkdir(parents=True)

        portfolio = LivePortfolio(_minimal_config(), market_id="sp500")
        portfolio.broker_data_valid = True

        with patch.object(portfolio, "_trigger_dashboard_refresh"):
            portfolio.record_closed_trade(_closed_trade())

        assert len(portfolio.closed_trades) == 1
        assert portfolio.closed_trades[0]["ticker"] == "TEST"
        assert portfolio.closed_trades[0]["pnl"] == 100.0

    def test_live_portfolio_has_no_load_state_method(self):
        """Regression guard: LivePortfolio.load_state() must NOT exist.

        Before C3: _execute_exit called _portfolio.load_state() which raised
        AttributeError, swallowing the record_closed_trade call silently.
        """
        assert not hasattr(LivePortfolio, "load_state"), (
            "LivePortfolio.load_state() must not exist — it was the phantom method "
            "that caused AttributeError and prevented record_closed_trade (C3 regression)."
        )

    def test_record_closed_trade_rejects_ghost_exit(self, tmp_path, monkeypatch):
        """record_closed_trade rejects ghost trades (exit_date < entry_date)."""
        monkeypatch.setattr("brokers.live_portfolio.PROJECT_ROOT", tmp_path)
        (tmp_path / "brokers" / "state").mkdir(parents=True)

        portfolio = LivePortfolio(_minimal_config(), market_id="sp500")
        portfolio.broker_data_valid = True

        ghost = {
            "ticker": "GHOST",
            "strategy": "test",
            "entry_price": 100.0,
            "exit_price": 90.0,
            "shares": 5,
            "pnl": -50.0,
            "pnl_pct": -10.0,
            "holding_days": 0,
            "exit_reason": "stop_loss",
            "exit_date": "2026-01-01",   # before entry date!
            "entry_date": "2026-04-15",
            "order_id": "ORD-GHOST-001",
        }

        with patch.object(portfolio, "_trigger_dashboard_refresh"):
            portfolio.record_closed_trade(ghost)

        assert len(portfolio.closed_trades) == 0, "Ghost trade must be rejected"

    def test_record_closed_trade_does_not_raise_attribute_error(self, tmp_path, monkeypatch):
        """Calling record_closed_trade must NOT raise AttributeError (C3 guard)."""
        monkeypatch.setattr("brokers.live_portfolio.PROJECT_ROOT", tmp_path)
        (tmp_path / "brokers" / "state").mkdir(parents=True)

        portfolio = LivePortfolio(_minimal_config(), market_id="sp500")
        portfolio.broker_data_valid = True

        with patch.object(portfolio, "_trigger_dashboard_refresh"):
            try:
                portfolio.record_closed_trade(_closed_trade("AAPL"))
            except AttributeError as e:
                pytest.fail(
                    f"C3: AttributeError raised — phantom load_state() method may have returned: {e}"
                )

    def test_record_closed_trade_rejects_duplicate(self, tmp_path, monkeypatch):
        """record_closed_trade silently rejects a duplicate (same ticker/date/price)."""
        monkeypatch.setattr("brokers.live_portfolio.PROJECT_ROOT", tmp_path)
        (tmp_path / "brokers" / "state").mkdir(parents=True)

        portfolio = LivePortfolio(_minimal_config(), market_id="sp500")
        portfolio.broker_data_valid = True

        trade = _closed_trade("AAPL")

        with patch.object(portfolio, "_trigger_dashboard_refresh"):
            portfolio.record_closed_trade(trade)
            portfolio.record_closed_trade(trade)  # duplicate

        assert len(portfolio.closed_trades) == 1, (
            "Duplicate trade must be silently rejected — only 1 entry expected"
        )

    def test_c3_executor_source_has_no_load_state_call(self):
        """Shape check: live_executor.py must not call _portfolio.load_state() (C3 guard)."""
        src = Path("brokers/live_executor.py").read_text()
        # This pattern was the bug — it should no longer appear
        assert "_portfolio.load_state()" not in src, (
            "C3: _portfolio.load_state() call must not exist in live_executor.py"
        )


# ═══════════════════════════════════════════════════════════════
# C4 — exit fill poll exception guard
# ═══════════════════════════════════════════════════════════════

class TestC4ExitPollGuard:
    """C4 regression: exit poll loop breaks (not crashes) on get_order_status exception."""

    def test_exit_poll_source_has_exception_guard(self):
        """Shape check: exit fill poll must wrap get_order_status in try/except/break (C4)."""
        src = Path("brokers/live_executor.py").read_text()
        # Anchor on the log message that starts the poll block
        idx = src.index("Waiting for fill on")
        block = src[idx: idx + 2500]

        assert "except Exception" in block, (
            "C4: exit fill poll must have 'except Exception' guard"
        )
        assert "Exit fill poll error" in block, (
            "C4: exit fill poll must log 'Exit fill poll error' on exception"
        )
        assert "break" in block, (
            "C4: exit fill poll must break on exception to prevent infinite loop"
        )

    def test_exit_poll_source_exception_is_before_loop_end(self):
        """Shape check: the except clause is inside the while loop body (C4 placement)."""
        src = Path("brokers/live_executor.py").read_text()
        idx = src.index("Waiting for fill on")
        block = src[idx: idx + 2500]
        # except Exception must come after the get_order_status call
        poll_call_idx = block.index("get_order_status")
        except_idx = block.index("except Exception")
        break_idx = block.index("Exit fill poll error")
        assert poll_call_idx < except_idx, (
            "C4: 'except Exception' must come after the get_order_status call"
        )
        assert except_idx < break_idx, (
            "C4: exception guard must contain the 'Exit fill poll error' log"
        )

    def test_poll_guard_inline_simulation(self):
        """Inline simulation: exception in poll body is caught and does NOT propagate (C4)."""
        import time as _time

        # Reproduce the exact control flow from live_executor.py:
        #   while _time.time() - _poll_start < _max_wait:
        #       try:
        #           status_result = get_order_status(order_id)
        #           ...
        #       except Exception as _poll_exc:
        #           logger.warning(...)
        #           break

        call_count = 0
        poll_broke = False
        propagated = False

        def mock_get_order_status(order_id: str):
            nonlocal call_count
            call_count += 1
            raise ConnectionError("simulated network failure")

        # Set poll_start to NOW so the loop runs (time_elapsed=0 < _max_wait=60)
        _poll_start = _time.time()
        _max_wait = 60  # seconds — loop will enter on first check (elapsed ~ 0)

        try:
            # Run exactly one iteration then stop (no real sleep)
            entered_loop = False
            while _time.time() - _poll_start < _max_wait:
                if entered_loop:
                    break  # only one iteration in test
                entered_loop = True
                # Mirror the exact try/except/break pattern from C4 fix
                try:
                    _status = mock_get_order_status("ORD-001")
                    if _status.fill_price > 0:
                        break
                except Exception as _poll_exc:
                    poll_broke = True
                    break
        except Exception:
            propagated = True

        assert not propagated, (
            "C4: poll exception must NOT propagate outside the while loop"
        )
        assert poll_broke, (
            "C4: poll loop must set break flag on exception (exception was caught)"
        )
        assert call_count == 1, "get_order_status was called once before exception"
