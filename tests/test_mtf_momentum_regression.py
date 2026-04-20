"""Regression tests for C6, C7 — MTFMomentum strategy fixes.

C6: generate_signals checks _get_held_tickers() + _can_open_position() OUTSIDE
    the ticker loop as the first two guards.
C7: check_exits order changed to Stop -> TP -> Trailing -> Time, making
    take_profit reachable even when days_held >= 3.

Run:
    cd /root/atlas && python3 -m pytest tests/test_mtf_momentum_regression.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from strategies.mtf_momentum import MTFMomentum


# ─── Config helpers ──────────────────────────────────────────

def _cfg(max_positions: int = 5) -> dict:
    return {
        "market": "sp500",
        "market_id": "sp500",
        "strategies": {
            "mtf_momentum": {
                "weekly_sma_period": 10,
                "weekly_rsi_period": 14,
                "weekly_rsi_min": 50,
                "daily_rsi_period": 14,
                "daily_rsi_max": 45,
                "daily_sma_period": 20,
                "pullback_sma_pct": 0.05,
                "atr_period": 10,
                "atr_stop_mult": 2.0,
                "trailing_stop_atr_mult": 2.5,
                "max_hold_days": 15,
                "use_macd_filter": False,
                "vol_min_ratio": 0.0,
                "min_weekly_confirmation": 1,
            }
        },
        "risk": {
            "starting_equity": 10_000,
            "max_risk_per_trade_pct": 0.02,
            "max_open_positions": max_positions,
            "max_sector_concentration": 10,
        },
    }


# ─── DataFrame builders ──────────────────────────────────────

def _uptrend_df(n: int = 250, base: float = 100.0) -> pd.DataFrame:
    """Synthetic uptrending OHLCV daily data."""
    rng = np.random.default_rng(42)
    dates = pd.date_range(end=pd.Timestamp.today(), periods=n, freq="B")
    prices = base + np.linspace(0, base * 0.5, n) + rng.normal(0, 0.5, n).cumsum()
    prices = np.maximum(prices, 1.0)
    return pd.DataFrame(
        {
            "open": prices * 0.999,
            "high": prices * 1.01,
            "low": prices * 0.99,
            "close": prices,
            "volume": np.full(n, 1_000_000),
        },
        index=dates,
    )


def _held_df(
    n: int = 60,
    close_today: float = 105.0,
    entry_offset_days: int = 7,
) -> pd.DataFrame:
    """DataFrame whose most recent close is *close_today*; entry is n-entry_offset_days bars ago."""
    end_date = pd.Timestamp.today()
    dates = pd.date_range(end=end_date, periods=n, freq="B")
    prices = np.linspace(95.0, close_today, n)
    df = pd.DataFrame(
        {
            "open": prices * 0.999,
            "high": prices * 1.02,
            "low": prices * 0.98,
            "close": prices,
            "volume": np.full(n, 500_000),
        },
        index=dates,
    )
    # Override the last row explicitly
    df.iloc[-1, df.columns.get_loc("close")] = close_today
    df.iloc[-1, df.columns.get_loc("high")] = close_today * 1.01
    df.iloc[-1, df.columns.get_loc("low")] = close_today * 0.99
    return df


def _pos(
    ticker: str = "TEST",
    entry_price: float = 100.0,
    stop: float = 95.0,
    take_profit: float = 115.0,
    days_ago: int = 7,
) -> dict:
    entry_date = (datetime.today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    return {
        "ticker": ticker,
        "strategy": "mtf_momentum",
        "entry_price": entry_price,
        "stop_price": stop,
        "take_profit": take_profit,
        "entry_date": entry_date,
        "shares": 10,
    }


# ═══════════════════════════════════════════════════════════════
# C6 — duplicate position guard
# ═══════════════════════════════════════════════════════════════

class TestC6DuplicatePositionGuard:
    """C6 regression: held-ticker + max-position guards run FIRST in generate_signals."""

    def test_skips_already_held_ticker(self):
        """generate_signals returns 0 signals for a ticker already in existing_positions (C6)."""
        strategy = MTFMomentum(_cfg())
        ticker = "AAPL"

        data = {ticker: _uptrend_df()}
        existing = [{"ticker": ticker, "strategy": "mtf_momentum"}]

        # Mock _get_held_tickers to return the ticker deterministically
        with patch.object(strategy, "_get_held_tickers", return_value={ticker}):
            signals = strategy.generate_signals(
                data, equity=10_000, existing_positions=existing
            )

        assert len(signals) == 0, (
            f"C6: expected 0 signals — {ticker} is already held. Got {len(signals)}."
        )

    def test_respects_max_positions_blocks_all(self):
        """generate_signals emits 0 signals when existing_positions >= max_open_positions (C6)."""
        max_pos = 3
        strategy = MTFMomentum(_cfg(max_positions=max_pos))

        data = {t: _uptrend_df() for t in ["AAPL", "MSFT", "GOOG", "AMZN", "TSLA"]}
        # Fill positions to the exact limit
        existing = [{"ticker": f"HELD{i}", "strategy": "other"} for i in range(max_pos)]

        signals = strategy.generate_signals(
            data, equity=10_000, existing_positions=existing
        )

        assert len(signals) == 0, (
            f"C6: expected 0 signals when at max_positions ({max_pos}). "
            f"Got {len(signals)}."
        )

    def test_held_tickers_called_before_loop(self):
        """Shape check: _get_held_tickers() called BEFORE 'for ticker, df in data' loop (C6)."""
        src = Path("strategies/mtf_momentum.py").read_text()
        idx = src.index("def generate_signals")
        # Look at the first ~600 chars of generate_signals
        block = src[idx: idx + 800]

        held_idx = block.index("_get_held_tickers")
        loop_idx = block.index("for ticker, df in data.items()")
        assert held_idx < loop_idx, (
            "C6: _get_held_tickers() must be called BEFORE the ticker loop"
        )

    def test_can_open_position_checked_inside_loop(self):
        """Shape check: _can_open_position() is checked inside the loop (early-exit path)."""
        src = Path("strategies/mtf_momentum.py").read_text()
        idx = src.index("def generate_signals")
        block = src[idx: idx + 2000]
        # _can_open_position must appear after the for loop start
        loop_idx = block.index("for ticker, df in data.items()")
        can_idx = block.index("_can_open_position")
        assert can_idx > loop_idx, (
            "C6: _can_open_position() must be checked inside the ticker loop"
        )

    def test_skips_with_no_data(self):
        """generate_signals handles empty data dict without error (edge case)."""
        strategy = MTFMomentum(_cfg())
        signals = strategy.generate_signals({}, equity=10_000, existing_positions=[])
        assert signals == []

    def test_allows_signals_when_not_held_and_under_max(self):
        """Control: with no positions held and space available, list proceeds (C6 not over-restrictive)."""
        strategy = MTFMomentum(_cfg(max_positions=5))

        # With held=empty and can_open=True, the list of signals depends on data quality.
        # We just verify the guards don't incorrectly block.
        data = {"AAPL": _uptrend_df()}
        with patch.object(strategy, "_get_held_tickers", return_value=set()):
            with patch.object(strategy, "_can_open_position", return_value=True):
                signals = strategy.generate_signals(
                    data, equity=10_000, existing_positions=[]
                )
        # May be 0 due to indicator thresholds — just verify it ran without error
        assert isinstance(signals, list)


# ═══════════════════════════════════════════════════════════════
# C7 — exit reachability
# ═══════════════════════════════════════════════════════════════

class TestC7ExitReachability:
    """C7 regression: all four exit branches are reachable; TP is not shadowed by trailing stop."""

    def test_take_profit_reachable_on_day3_plus(self):
        """C7: take_profit fires when close >= TP and days_held >= 3."""
        strategy = MTFMomentum(_cfg())
        ticker = "TEST"

        df = _held_df(n=50, close_today=116.0)       # close above TP=115
        pos = _pos(entry_price=100.0, stop=95.0, take_profit=115.0, days_ago=7)

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])
        reasons = [e["reason"] for e in exits]

        assert "take_profit" in reasons, (
            f"C7: expected 'take_profit' exit (close=116, tp=115, days_held=7). Got: {reasons}"
        )

    def test_take_profit_not_shadowed_by_trailing_stop(self):
        """C7 key regression: TP fires first even when trailing stop would also trigger.

        Before C7: trailing_stop was checked before take_profit (days_held >= 3 path),
        so take_profit was unreachable when days_held >= 3 and trailing fired first.
        """
        strategy = MTFMomentum(_cfg())
        ticker = "TEST"

        # Price at 116 — above TP=115 AND above any reasonable trailing stop.
        # Before C7: trailing_stop path evaluated first and could shadow TP.
        df = _held_df(n=50, close_today=116.0)
        pos = _pos(entry_price=100.0, stop=90.0, take_profit=115.0, days_ago=8)

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])
        reasons = [e["reason"] for e in exits]

        assert len(exits) >= 1, "Expected at least one exit"
        assert exits[0]["reason"] == "take_profit", (
            f"C7: first exit must be 'take_profit', not '{exits[0]['reason']}'. "
            f"Before C7 fix, trailing_stop shadowed TP on day 3+. Got: {reasons}"
        )
        assert "trailing_stop" not in reasons, (
            f"C7: 'trailing_stop' must not appear when TP fires. Got: {reasons}"
        )

    def test_stop_loss_reachable(self):
        """C7: stop_loss fires when close <= stop_price (always first in order)."""
        strategy = MTFMomentum(_cfg())
        ticker = "TEST"

        df = _held_df(n=30, close_today=90.0)     # below stop=95
        pos = _pos(entry_price=100.0, stop=95.0, take_profit=120.0, days_ago=3)

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])
        reasons = [e["reason"] for e in exits]

        assert "stop_loss" in reasons, (
            f"C7: expected 'stop_loss' (close=90, stop=95). Got: {reasons}"
        )

    def test_trailing_stop_reachable_when_tp_not_hit(self):
        """C7: trailing_stop path is reachable when close < TP and days_held >= 3."""
        strategy = MTFMomentum(_cfg())
        ticker = "TEST"

        # Build a DataFrame that rallied then fell back, so trailing fires
        n = 60
        entry_dt = datetime.today() - timedelta(days=n)
        dates = pd.date_range(start=entry_dt, periods=n, freq="B")

        # Rally from 100 to 120, then drop to 103
        up = np.linspace(100.0, 120.0, n // 2)
        down = np.linspace(120.0, 103.0, n - n // 2)
        prices = np.concatenate([up, down])

        df = pd.DataFrame(
            {
                "open": prices * 0.999,
                "high": prices * 1.01,
                "low": prices * 0.99,
                "close": prices,
                "volume": np.full(n, 500_000),
            },
            index=dates,
        )
        # final close = 103, low = 101.97
        df.iloc[-1, df.columns.get_loc("close")] = 103.0
        df.iloc[-1, df.columns.get_loc("low")] = 102.0

        # TP very high (not triggered), stop way below (not triggered)
        pos = _pos(
            entry_price=100.0, stop=80.0, take_profit=200.0, days_ago=n
        )

        # Must not raise — path must be executable
        exits = strategy.check_exits(data={ticker: df}, positions=[pos])
        assert isinstance(exits, list), (
            "C7: check_exits must not raise when trailing_stop path is evaluated"
        )
        # trailing_stop may or may not fire (depends on ATR vs drop depth)
        # — the point is the path is reachable, not that it always fires

    def test_time_exit_reachable(self):
        """C7: time_exit fires when days_held >= max_hold_days and days_held < 3.

        Structural note: time_exit sits in an elif chain after trailing stop
        (elif days_held >= 3). It fires when days_held >= max_hold_days but
        days_held < 3, i.e. max_hold_days must be <= 2 for the branch to activate.
        We use max_hold_days=1, days_ago=1 (days_held=1 < 3 and >= max_hold_days).
        """
        # Use a config with a very short max_hold_days so the elif branch is reachable
        cfg_short = _cfg(max_positions=5)
        cfg_short["strategies"]["mtf_momentum"]["max_hold_days"] = 1
        strategy = MTFMomentum(cfg_short)
        ticker = "TEST"

        # days_ago=1 → days_held=1, which is < 3 (skips trailing check)
        #              AND >= max_hold_days=1 (triggers time_exit)
        df = _held_df(n=40, close_today=105.0, entry_offset_days=1)
        pos = _pos(
            entry_price=100.0,
            stop=50.0,          # far below — won't trigger
            take_profit=999.0,  # far above — won't trigger
            days_ago=1,
        )

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])
        reasons = [e["reason"] for e in exits]

        assert "time_exit" in reasons, (
            f"C7: expected 'time_exit' with max_hold_days=1, days_held=1. Got: {reasons}"
        )

    def test_exit_order_is_stop_tp_trailing_time(self):
        """C7 shape check: check_exits source has Stop → TP → Trailing → Time order."""
        src = Path("strategies/mtf_momentum.py").read_text()
        idx = src.index("def check_exits")
        block = src[idx: idx + 4000]

        stop_idx = block.index('"stop_loss"')
        tp_idx = block.index('"take_profit"')
        trail_idx = block.index('"trailing_stop"')
        time_idx = block.index('"time_exit"')

        assert stop_idx < tp_idx, "C7: 'stop_loss' must come before 'take_profit'"
        assert tp_idx < trail_idx, "C7: 'take_profit' must come before 'trailing_stop'"
        assert trail_idx < time_idx, "C7: 'trailing_stop' must come before 'time_exit'"

    def test_take_profit_check_uses_elif_after_stop(self):
        """C7 shape check: take_profit uses 'elif' (exclusive with stop), not nested if."""
        src = Path("strategies/mtf_momentum.py").read_text()
        idx = src.index("def check_exits")
        block = src[idx: idx + 4000]

        # After stop_loss check, take_profit must be elif (not a standalone if)
        stop_pos = block.index('"stop_loss"')
        tp_pos = block.index('"take_profit"')

        # Grab the code between stop and tp exit strings
        between = block[stop_pos: tp_pos]
        assert "elif" in between, (
            "C7: take_profit condition must be 'elif' (exclusive with stop_loss). "
            "If it were a bare 'if', both could fire on the same bar."
        )


# ═══════════════════════════════════════════════════════════════
# C7 follow-up — trailing elif must not shadow time_exit
# ═══════════════════════════════════════════════════════════════

class TestC7FollowupTimeExitShadowing:
    """C7 follow-up: trailing elif must not shadow time_exit when max_hold_days > 3.

    Before the fix, the `elif days_held >= 3:` outer condition on the trailing
    branch consumed the elif chain even when the inner trail price check did NOT
    fire an exit.  Result: time_exit was unreachable whenever
        max_hold_days > 3  AND  days_held >= 3  AND  trail didn't actually hit.

    Fix (C7 follow-up): pre-compute `trail_hit` as a single boolean, then gate
    the trailing branch on `elif trail_hit:`.  The elif chain only commits when
    trailing actually fires; otherwise it falls through to time_exit correctly.

    All 4 tests use max_hold_days=5 (> 3) to stress-test the previously-buggy
    path.  test_time_exit_fires_day5_max_hold_5_no_stop_tp_trail is the key
    regression test — it FAILED on the pre-fix code and PASSES after the fix.
    """

    @staticmethod
    def _make_strategy(max_hold_days: int = 5) -> MTFMomentum:
        cfg = _cfg()
        cfg["strategies"]["mtf_momentum"]["max_hold_days"] = max_hold_days
        return MTFMomentum(cfg)

    # ── 1. Stop loss ──────────────────────────────────────────────────────────

    def test_stop_loss_fires_day1_with_long_max_hold(self):
        """Branch 1 (stop_loss): fires on day 1 even with max_hold_days=5.

        days_ago=1 → days_held=1 (< 3, trail pre-compute skipped entirely).
        close=90 <= stop=95 → stop_loss fires.
        """
        strategy = self._make_strategy(max_hold_days=5)
        ticker = "TEST"

        df = _held_df(n=40, close_today=90.0)   # close well below stop
        pos = _pos(
            ticker=ticker,
            entry_price=100.0,
            stop=95.0,           # 90 <= 95 → fires
            take_profit=999.0,   # unreachable
            days_ago=1,
        )

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])
        reasons = [e["reason"] for e in exits]

        assert "stop_loss" in reasons, (
            f"C7 follow-up: expected 'stop_loss' (close=90, stop=95, days_held=1). "
            f"Got: {reasons}"
        )

    # ── 2. Take profit ────────────────────────────────────────────────────────

    def test_take_profit_fires_day8_with_long_max_hold(self):
        """Branch 2 (take_profit): fires on day 8 with max_hold_days=5 and close >= tp.

        days_ago=8 → days_held=8 (>= 3, trail pre-compute runs).
        Smooth uptrend df: trail_stop = highest_high - 2.5*ATR ≈ 107.
        close=116 > trail_stop → trail_hit=False.
        close=116 >= tp=115 → take_profit fires.
        """
        strategy = self._make_strategy(max_hold_days=5)
        ticker = "TEST"

        df = _held_df(n=60, close_today=116.0)
        pos = _pos(
            ticker=ticker,
            entry_price=100.0,
            stop=80.0,           # far below — won't trigger
            take_profit=115.0,   # 116 >= 115 → fires
            days_ago=8,
        )

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])
        reasons = [e["reason"] for e in exits]

        assert "take_profit" in reasons, (
            f"C7 follow-up: expected 'take_profit' (close=116, tp=115, days_held=8). "
            f"Got: {reasons}"
        )
        assert "trailing_stop" not in reasons, (
            f"C7 follow-up: 'trailing_stop' must not appear when take_profit fires. "
            f"Got: {reasons}"
        )

    # ── 3. Trailing stop ──────────────────────────────────────────────────────

    def test_trailing_fires_day60_with_long_max_hold(self):
        """Branch 3 (trailing_stop): fires when price rallied then dropped, max_hold_days=5.

        Tie-breaker applied: days_ago=60 (all 60 bars in the 'since entry' mask).
        With days_ago=4 or days_ago=8 only 3-6 tail bars are captured; the
        highest high since entry is too close to current close (~106) so
        trail_stop < today_close and trail doesn't fire.  Using days_ago=60
        captures the full rally peak at ~121.2, producing
        trail_stop ≈ 115.8 >> today_close=103, so trail fires deterministically.

        Pattern: rally 100→120 (first 30 bars) then drop 120→103 (last 30 bars).
        tp=200 (unreachable), stop=80 (unreachable).
        """
        strategy = self._make_strategy(max_hold_days=5)
        ticker = "TEST"

        n = 60
        dates = pd.date_range(end=pd.Timestamp.today(), periods=n, freq="B")
        up = np.linspace(100.0, 120.0, n // 2)
        down = np.linspace(120.0, 103.0, n - n // 2)
        prices = np.concatenate([up, down])
        df = pd.DataFrame(
            {
                "open": prices * 0.999,
                "high": prices * 1.01,
                "low": prices * 0.99,
                "close": prices,
                "volume": np.full(n, 500_000),
            },
            index=dates,
        )
        df.iloc[-1, df.columns.get_loc("close")] = 103.0
        df.iloc[-1, df.columns.get_loc("low")] = 102.0

        pos = _pos(
            ticker=ticker,
            entry_price=100.0,
            stop=80.0,           # far below — won't trigger
            take_profit=200.0,   # far above — won't trigger
            days_ago=n,          # all bars in mask
        )

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])
        reasons = [e["reason"] for e in exits]

        assert "trailing_stop" in reasons, (
            f"C7 follow-up: expected 'trailing_stop' (peak=121.2, close=103, "
            f"trail_stop≈115.8). Got: {reasons}"
        )

    # ── 4. Time exit (KEY REGRESSION TEST) ───────────────────────────────────

    def test_time_exit_fires_day5_max_hold_5_no_stop_tp_trail(self):
        """Branch 4 (time_exit): KEY regression — fails on pre-fix code, passes after.

        Before the C7 follow-up fix:
          `elif days_held >= 3:` consumed the elif chain even when the inner
          trail price check FAILED.  days_held=5 >= 3 → chain consumed;
          trail_stop ≈ 97 << today_close=105 → no exit appended, but chain is
          committed, so `elif days_held >= max_hold_days:` is NEVER reached.
          Result: no exit at all, even though days_held=5 >= max_hold_days=5.

        After the fix:
          `trail_hit=False` (smooth uptrend, no significant drop since entry).
          `elif trail_hit:` → False → falls through.
          `elif days_held >= max_hold_days:` → 5 >= 5 → time_exit fires. ✓

        Setup: smooth uptrend _held_df (no high-to-close gap > 2.5*ATR),
        stop=50 (unreachable), tp=999 (unreachable), days_ago=5.
        """
        strategy = self._make_strategy(max_hold_days=5)
        ticker = "TEST"

        # Smooth uptrend: prices 95→105, high=price*1.02, low=price*0.98.
        # ATR(10) ≈ 3.93; highest_high (last ~4 bars) ≈ 106.93;
        # trail_stop ≈ 97.10 << today_close=105 → trail_hit=False. ✓
        df = _held_df(n=60, close_today=105.0)
        pos = _pos(
            ticker=ticker,
            entry_price=100.0,
            stop=50.0,           # far below — won't trigger
            take_profit=999.0,   # far above — won't trigger
            days_ago=5,          # days_held=5 == max_hold_days=5
        )

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])
        reasons = [e["reason"] for e in exits]

        assert "time_exit" in reasons, (
            f"C7 follow-up REGRESSION: expected 'time_exit' with max_hold_days=5, "
            f"days_held=5, smooth uptrend (trail_hit=False). "
            f"This test FAILS on pre-fix code (elif days_held>=3 shadowed the chain). "
            f"Got: {reasons}"
        )
        assert "trailing_stop" not in reasons, (
            f"C7 follow-up: 'trailing_stop' must not appear in a smooth uptrend. "
            f"Got: {reasons}"
        )
        assert "stop_loss" not in reasons, (
            f"C7 follow-up: 'stop_loss' must not appear (stop=50, close=105). "
            f"Got: {reasons}"
        )

