"""Regression tests for C8 — SectorRotation exception logging fix.

C8: bare `except Exception: pass` replaced with
    `self._logger.warning(f"..."); continue` in both the
    days_held calc block and the trailing-stop check block.

Run:
    cd /root/atlas && python3 -m pytest tests/test_sector_rotation_regression.py -v
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from strategies.sector_rotation import SectorRotation


# ─── Helpers ─────────────────────────────────────────────────

def _cfg(profit_target: float = 0.0) -> dict:
    return {
        "market": "sp500",
        "market_id": "sp500",
        "strategies": {
            "sector_rotation": {
                "atr_period": 10,
                "atr_stop_mult": 2.0,
                "trailing_stop_atr_mult": 2.5,
                "profit_target_atr_mult": profit_target,
                "max_hold_days": 20,
                "sector_momentum_period": 20,
                "top_sectors": 3,
                "bottom_sectors": 2,
                "min_sector_stocks": 1,
                "stocks_per_sector": 2,
                "rebalance_days": 20,
            }
        },
        "risk": {
            "starting_equity": 10_000,
            "max_risk_per_trade_pct": 0.01,
            "max_open_positions": 10,
            "max_sector_concentration": 5,
        },
    }


def _df(n: int = 50, close: float = 100.0) -> pd.DataFrame:
    """Synthetic daily OHLCV DataFrame."""
    dates = pd.date_range(end=pd.Timestamp.today(), periods=n, freq="B")
    prices = np.linspace(90.0, close, n)
    return pd.DataFrame(
        {
            "open": prices * 0.999,
            "high": prices * 1.02,
            "low": prices * 0.98,
            "close": prices,
            "volume": np.full(n, 1_000_000),
        },
        index=dates,
    )


def _valid_entry_date(df: pd.DataFrame, days_before_end: int = 10) -> str:
    """Return an entry_date string that is days_before_end bars before df.index[-1]."""
    target = df.index[-1] - pd.Timedelta(days=days_before_end)
    return str(target.date())


# ═══════════════════════════════════════════════════════════════
# C8 — exception logging + continue
# ═══════════════════════════════════════════════════════════════

class TestC8ExceptionLogging:
    """C8 regression: bare except pass replaced with warning log + continue."""

    def test_days_held_error_logs_warning_and_continues(self, caplog):
        """C8: bad entry_date triggers warning + continue (not silent pass)."""
        strategy = SectorRotation(_cfg())
        ticker = "AAPL"
        df = _df(n=50, close=100.0)

        pos = {
            "ticker": ticker,
            "strategy": "sector_rotation",
            "entry_price": 95.0,
            "stop_price": 85.0,
            "entry_date": "not-a-date",   # forces pd.Timestamp slicing to raise
            "shares": 10,
        }

        with caplog.at_level(logging.WARNING):
            result = strategy.check_exits(data={ticker: df}, positions=[pos])

        # Must not raise
        assert isinstance(result, list), (
            "C8: check_exits must return a list even when days_held calc fails"
        )

        # C8: must emit a warning (before C8, this was bare pass — no log)
        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("sector_rotation" in m and "days_held" in m for m in warning_msgs), (
            f"C8: expected warning containing 'sector_rotation' and 'days_held'. "
            f"Got: {warning_msgs}"
        )

    def test_days_held_error_continue_processes_next_position(self, caplog):
        """C8: 'continue' means subsequent positions are still processed after error."""
        strategy = SectorRotation(_cfg())

        ticker_bad = "BAD"
        ticker_good = "GOOD"

        n = 60
        df_good = _df(n=n, close=105.0)
        df_bad = _df(n=n, close=100.0)
        # Entry date for good ticker: far enough back to trigger time_exit
        good_entry_date = (datetime.today() - timedelta(days=n)).strftime("%Y-%m-%d")

        positions = [
            {
                "ticker": ticker_bad,
                "strategy": "sector_rotation",
                "entry_price": 95.0,
                "stop_price": 85.0,
                "entry_date": "not-a-date",  # triggers the error
                "shares": 10,
            },
            {
                "ticker": ticker_good,
                "strategy": "sector_rotation",
                "entry_price": 95.0,
                "stop_price": 85.0,
                "entry_date": good_entry_date,
                "shares": 10,
            },
        ]

        with caplog.at_level(logging.WARNING):
            result = strategy.check_exits(
                data={ticker_bad: df_bad, ticker_good: df_good},
                positions=positions,
            )

        assert isinstance(result, list), "check_exits must return list"

        # Good ticker should still produce a time_exit (days_held = n > max_hold=20)
        tickers_exited = [e.get("ticker") for e in result]
        assert ticker_good in tickers_exited, (
            f"C8: 'continue' must allow subsequent positions to be processed. "
            f"Got exits for: {tickers_exited}"
        )
        assert ticker_bad not in tickers_exited, (
            f"C8: the bad-entry-date position should be skipped, not appear in exits"
        )

    def test_c8_source_has_warning_not_bare_pass(self):
        """Shape check: C8 fixed except blocks contain warning log, not just 'pass'."""
        src = Path("strategies/sector_rotation.py").read_text()

        # Both warning messages must appear in source (they replaced bare pass)
        assert "sector_rotation days_held calc failed" in src, (
            "C8: 'sector_rotation days_held calc failed' warning message must be in source"
        )
        assert "sector_rotation trailing-stop check failed" in src, (
            "C8: 'sector_rotation trailing-stop check failed' warning message must be in source"
        )

    def test_c8_source_uses_continue_in_except_blocks(self):
        """Shape check: both C8 fixed except blocks use 'continue' (not pass or return)."""
        src = Path("strategies/sector_rotation.py").read_text()

        # Check days_held block
        days_idx = src.index("sector_rotation days_held calc failed")
        days_block = src[days_idx - 200: days_idx + 300]
        assert "continue" in days_block, (
            "C8: days_held except block must use 'continue' after the warning"
        )

        # Check trailing-stop block
        trail_idx = src.index("sector_rotation trailing-stop check failed")
        trail_block = src[trail_idx - 200: trail_idx + 300]
        assert "continue" in trail_block, (
            "C8: trailing-stop except block must use 'continue' after the warning"
        )

    def test_c8_no_bare_pass_in_check_exits(self):
        """Shape check: check_exits must not contain bare 'except Exception: pass'."""
        src = Path("strategies/sector_rotation.py").read_text()
        idx = src.index("def check_exits")
        # Grab the check_exits function body
        block = src[idx: idx + 5000]

        # Look for the pattern that was the bug: except ... : \n ... pass
        import re
        bare_pass_pattern = re.compile(
            r'except\s+Exception.*?:\s*\n\s*pass', re.DOTALL
        )
        matches = bare_pass_pattern.findall(block)
        assert len(matches) == 0, (
            f"C8: bare 'except Exception: pass' must not exist in check_exits. "
            f"Found {len(matches)} instance(s)."
        )

    def test_check_exits_does_not_raise_on_error_position(self, caplog):
        """C8: check_exits returns without raising when the C8-guarded block errors.

        The C8 fix covers the days_held calc exception. Required fields (entry_price
        etc) must still be present — the guard only covers errors in the date parsing
        and trailing-stop ATR calc blocks.
        """
        strategy = SectorRotation(_cfg())
        ticker = "BROKEN"
        df = _df(n=30, close=100.0)

        # Provide all required fields but use a bad entry_date to trigger the C8 block
        pos = {
            "ticker": ticker,
            "strategy": "sector_rotation",
            "entry_price": 95.0,
            "stop_price": 85.0,
            "entry_date": "not-a-date",  # triggers C8 days_held except block
        }

        with caplog.at_level(logging.WARNING):
            try:
                result = strategy.check_exits(data={ticker: df}, positions=[pos])
            except Exception as e:
                pytest.fail(
                    f"C8: check_exits must not raise when C8-guarded block errors: {e}"
                )

        assert isinstance(result, list)

    def test_trailing_stop_error_logs_warning(self, caplog):
        """C8: trailing-stop error emits warning and continues (not swallowed by bare pass)."""
        strategy = SectorRotation(_cfg())
        src = Path("strategies/sector_rotation.py").read_text()

        # Verify the warning message exists in source as a static assertion
        assert "sector_rotation trailing-stop check failed" in src, (
            "C8: trailing-stop warning message must be present in sector_rotation.py"
        )

        # Verify it's a warning-level log call (not debug)
        trail_idx = src.index("sector_rotation trailing-stop check failed")
        trail_context = src[trail_idx - 150: trail_idx + 100]
        assert "_logger.warning" in trail_context, (
            "C8: trailing-stop error must use self._logger.warning(), not debug/info"
        )
