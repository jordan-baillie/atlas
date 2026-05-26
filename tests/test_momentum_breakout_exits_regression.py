"""Regression tests for momentum_breakout.check_exits() — task #353.

Bug: when profit_target_atr_mult > 0, the old elif chain entered the
     "elif self.profit_target_atr_mult > 0" branch on every iteration.
     If TP was not hit, the branch fell through with no exit appended,
     but the elif chain was consumed — trailing stop and time exit branches
     were NEVER reached.

Fix: replaced the if/elif/elif/elif chain with independent if blocks plus
     `continue` after each exit so each branch runs on its own.

Priority order enforced by the fix:
  1. hard stop  (stop_hit)
  2. take profit (take_profit)  — only if configured AND price reached target
  3. trailing stop (trailing_stop)  — independent of TP config
  4. time exit  (time_exit)        — independent of TP and trailing stop

Run:
    cd /root/atlas && python3 -m pytest tests/test_momentum_breakout_exits_regression.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from strategies.momentum_breakout import MomentumBreakout


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _cfg(
    profit_target_atr_mult: float = 6.0,   # live config value
    trailing_stop_atr_mult: float = 4.0,
    max_hold_days: int = 20,
) -> dict:
    return {
        "market": "sp500",
        "market_id": "sp500",
        "risk": {
            "starting_equity": 10_000,
            "max_risk_per_trade_pct": 0.01,
            "max_open_positions": 5,
            "max_sector_concentration": 10,
        },
        "fees": {
            "commission_per_trade": 0,
            "commission_pct": 0.0,
            "slippage_pct": 0.0,
            "min_position_value": 100.0,
            "flat_fee_threshold": 0,
        },
        "trading": {"mode": "paper", "broker": "alpaca", "live_enabled": False},
        "strategies": {
            "momentum_breakout": {
                "lookback_days": 20,
                "atr_period": 14,
                "atr_stop_mult": 3.5,
                "trailing_stop_atr_mult": trailing_stop_atr_mult,
                "profit_target_atr_mult": profit_target_atr_mult,
                "max_hold_days": max_hold_days,
                "trend_ma_period": 50,
            }
        },
    }


# ---------------------------------------------------------------------------
# DataFrame / position builders
# ---------------------------------------------------------------------------

def _make_df(
    n: int,
    *,
    close_values: np.ndarray,
    atr: float,
) -> pd.DataFrame:
    """Build OHLCV DataFrame anchored to TODAY with injected _mb_atr column.

    Always ends at today so that check_exits sees today_date ≈ datetime.today()
    and days_held calculations are predictable.
    """
    dates = pd.date_range(end=pd.Timestamp.today(), periods=n, freq="B")
    prices = close_values
    df = pd.DataFrame(
        {
            "open": prices * 0.999,
            "high": prices * 1.01,
            "low": prices * 0.99,
            "close": prices,
            "volume": np.full(n, 1_000_000.0),
        },
        index=dates,
    )
    # Inject precomputed ATR so check_exits doesn't fall back to stop-based estimate
    df["_mb_atr"] = atr
    return df


def _pos(
    ticker: str = "TEST",
    entry_price: float = 100.0,
    stop_price: float = 80.0,
    take_profit: float = 200.0,
    entry_date: datetime | None = None,
) -> dict:
    if entry_date is None:
        entry_date = datetime.today() - timedelta(days=5)
    return {
        "ticker": ticker,
        "strategy": "momentum_breakout",
        "entry_price": entry_price,
        "stop_price": stop_price,
        "take_profit": take_profit,
        "entry_date": entry_date.strftime("%Y-%m-%d"),
        "shares": 10,
    }


def _strategy(**kw) -> MomentumBreakout:
    s = MomentumBreakout(_cfg(**kw))
    s._precomputed = True   # skip precompute() — we inject _mb_atr directly
    return s


# ---------------------------------------------------------------------------
# Test 1: trailing stop fires when TP is configured but NOT hit
# ---------------------------------------------------------------------------

class TestTrailingStopFiresWhenTPNotHit:
    """Regression: trailing stop must fire independently of profit_target_atr_mult."""

    def test_trailing_fires_with_tp_configured_and_not_hit(self):
        """BUG TRIGGER: price hits trailing stop but TP not hit.

        Before the fix: elif chain consumed by 'elif profit_target_atr_mult > 0',
        trailing_stop branch never reached → NO exit returned.
        After the fix: trailing_stop is an independent 'if' block → exit returned.

        Setup (DataFrame ends at today, 60 bars):
          profit_target_atr_mult = 6.0 (live config)
          trailing_stop_atr_mult = 4.0, ATR = 2.0
          Prices: rally 100→120 (bars 0-29), then drop 120→108 (bars 30-59)
          entry_date = before first bar → mask covers ALL 60 bars
          highest_since_entry = 120.0 (peak at bar 30)
          trailing_stop = 120 − 4×2 = 112.0
          today_close = 108.0 ≤ 112.0 → trailing fires ✓
          take_profit = 200.0 (not hit), stop_price = 80.0 (not hit)
        """
        ticker = "TEST"
        strategy = _strategy(profit_target_atr_mult=6.0, trailing_stop_atr_mult=4.0)

        n = 60
        half = n // 2
        prices = np.concatenate([
            np.linspace(100.0, 120.0, half),   # rally
            np.linspace(120.0, 108.0, n - half), # pullback
        ])
        df = _make_df(n, close_values=prices, atr=2.0)

        # entry_date one day before the first bar → mask = all bars → peak = 120
        entry_date = df.index[0].to_pydatetime() - timedelta(days=1)

        pos = _pos(
            ticker=ticker,
            entry_price=100.0,
            stop_price=80.0,      # not hit (108 > 80)
            take_profit=200.0,    # not hit (108 < 200)
            entry_date=entry_date,
        )

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])
        reasons = [e["reason"] for e in exits]

        assert "trailing_stop" in reasons, (
            f"REGRESSION #353: trailing_stop must fire when price (108.0) "
            f"<= trailing_stop (peak=120, ATR=2.0 → trail=112.0), "
            f"even with profit_target_atr_mult=6.0 configured. Got: {reasons}"
        )
        assert "take_profit" not in reasons, f"TP must not fire (close 108 < tp 200). Got: {reasons}"
        assert "stop_hit" not in reasons, f"Hard stop must not fire (close 108 > stop 80). Got: {reasons}"
        assert len(exits) == 1, f"Exactly one exit per position per call. Got: {exits}"

    def test_trailing_does_not_fire_when_price_above_trail(self):
        """Control: trailing stop does NOT fire when price is above trail level.

        entry_date set to 5 calendar days ago so days_held(≈5) < max_hold_days(20)
        → time exit also stays silent → no exits at all expected.
        close[-1] ≈ 110, peak ≈ 110, ATR=2 → trail = 110 − 8 = 102;
        110 > 102 → trailing does NOT fire.
        """
        ticker = "TEST"
        strategy = _strategy(profit_target_atr_mult=6.0, trailing_stop_atr_mult=4.0)

        n = 40
        # Smooth uptrend: close[-1] ≈ 110, peak ≈ 110, ATR=2 → trail = 110 − 8 = 102
        # today_close=110 > 102 → trailing does NOT fire
        prices = np.linspace(100.0, 110.0, n)
        df = _make_df(n, close_values=prices, atr=2.0)

        # 5 calendar days ago → days_held≈5 < max_hold_days=20 → time exit silent too
        entry_date = datetime.today() - timedelta(days=5)

        pos = _pos(
            ticker=ticker,
            entry_price=100.0,
            stop_price=80.0,
            take_profit=200.0,
            entry_date=entry_date,
        )

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])

        assert exits == [], (
            f"No exits expected (close≈110 > trail≈102, days_held≈5 < 20, hard stop and TP not hit). "
            f"Got: {exits}"
        )


# ---------------------------------------------------------------------------
# Test 2: time exit fires when TP is configured but NOT hit
# ---------------------------------------------------------------------------

class TestTimeExitFiresWhenTPNotHit:
    """Regression: time exit must fire independently of profit_target_atr_mult."""

    def test_time_exit_fires_with_tp_configured_and_not_hit(self):
        """BUG TRIGGER: days_held >= max_hold_days, TP not hit, no trailing hit.

        Before the fix: elif chain consumed by 'elif profit_target_atr_mult > 0',
        time_exit branch never reached → NO exit returned.
        After the fix: time_exit is an independent 'if' block → exit returned.

        Setup (DataFrame ends at today, 40 bars):
          profit_target_atr_mult = 6.0 (live config)
          max_hold_days = 10
          entry_date = today − 15 calendar days → days_held ≈ 15 ≥ 10 → time fires ✓
          Smooth uptrend 100→110: peak ≈ 110, ATR=2.0 → trail = 110 − 8 = 102
          today_close = 110 > 102 → trailing does NOT fire ✓
          take_profit = 200.0 (not hit), stop_price = 80.0 (not hit)
        """
        ticker = "TEST"
        strategy = _strategy(
            profit_target_atr_mult=6.0,
            trailing_stop_atr_mult=4.0,
            max_hold_days=10,
        )

        n = 40
        prices = np.linspace(100.0, 110.0, n)  # smooth uptrend → no trailing
        df = _make_df(n, close_values=prices, atr=2.0)

        # Entry 15 calendar days ago → days_held ≈ 15 ≥ max_hold_days=10
        entry_date = datetime.today() - timedelta(days=15)

        pos = _pos(
            ticker=ticker,
            entry_price=100.0,
            stop_price=80.0,      # not hit
            take_profit=200.0,    # not hit
            entry_date=entry_date,
        )

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])
        reasons = [e["reason"] for e in exits]

        assert "time_exit" in reasons, (
            f"REGRESSION #353: time_exit must fire when days_held(≈15) >= max_hold_days(10), "
            f"even with profit_target_atr_mult=6.0 configured. Got: {reasons}"
        )
        assert "take_profit" not in reasons, f"TP must not fire (close 110 < tp 200). Got: {reasons}"
        assert "stop_hit" not in reasons, f"Hard stop must not fire (close 110 > stop 80). Got: {reasons}"
        assert len(exits) == 1, f"Exactly one exit per position per call. Got: {exits}"

    def test_time_exit_does_not_fire_before_max_hold(self):
        """Control: time exit does NOT fire when days_held < max_hold_days.

        With entry 5 days ago and max_hold_days=20, no exit of any kind expected:
        close≈105, ATR=2, trail=105−8=97 → 105 > 97 (trailing silent);
        stop=80 not hit; TP=200 not hit; days_held≈5 < 20 (time silent).
        """
        ticker = "TEST"
        strategy = _strategy(
            profit_target_atr_mult=6.0,
            trailing_stop_atr_mult=4.0,
            max_hold_days=20,
        )

        n = 40
        prices = np.linspace(100.0, 105.0, n)
        df = _make_df(n, close_values=prices, atr=2.0)

        # Entry 5 calendar days ago → days_held ≈ 5 < 20
        entry_date = datetime.today() - timedelta(days=5)

        pos = _pos(
            ticker=ticker,
            entry_price=100.0,
            stop_price=80.0,
            take_profit=200.0,
            entry_date=entry_date,
        )

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])

        assert exits == [], (
            f"No exits expected (days_held≈5 < max_hold_days=20, trailing silent, "
            f"hard stop and TP not hit). Got: {exits}"
        )


# ---------------------------------------------------------------------------
# Test 3: take profit fires when price reaches target
# ---------------------------------------------------------------------------

class TestTakeProfitFires:
    """Take profit must fire when price >= take_profit (with profit_target_atr_mult > 0)."""

    def test_tp_fires_when_price_reaches_target(self):
        """TP must append a take_profit exit when today_close >= take_profit."""
        ticker = "TEST"
        strategy = _strategy(profit_target_atr_mult=6.0)

        n = 40
        # today_close ≈ 120, above take_profit=115
        prices = np.linspace(100.0, 120.0, n)
        df = _make_df(n, close_values=prices, atr=2.0)

        entry_date = datetime.today() - timedelta(days=5)

        pos = _pos(
            ticker=ticker,
            entry_price=100.0,
            stop_price=80.0,
            take_profit=115.0,    # 120 >= 115 → fires
            entry_date=entry_date,
        )

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])
        reasons = [e["reason"] for e in exits]

        assert "take_profit" in reasons, (
            f"take_profit must fire when close(≈120) >= tp(115). Got: {reasons}"
        )
        assert "trailing_stop" not in reasons, (
            f"trailing_stop must not appear when TP fires (continue skips it). Got: {reasons}"
        )
        assert "time_exit" not in reasons, (
            f"time_exit must not appear when TP fires (continue skips it). Got: {reasons}"
        )
        assert len(exits) == 1, f"Exactly one exit per position per call. Got: {exits}"
        assert exits[0]["exit_price"] == 115.0, (
            f"exit_price should be the TP level 115.0, got {exits[0]['exit_price']}"
        )

    def test_tp_does_not_fire_when_price_below_target(self):
        """Control: TP must NOT fire when today_close < take_profit.

        With entry 3 days ago and max_hold_days=20, no exit of any kind expected:
        close≈110, ATR=2, trail=110−8=102 → 110 > 102 (trailing silent);
        stop=80 not hit; TP=150 not hit; days_held≈3 < 20 (time silent).
        """
        ticker = "TEST"
        strategy = _strategy(profit_target_atr_mult=6.0)

        n = 40
        prices = np.linspace(100.0, 110.0, n)
        df = _make_df(n, close_values=prices, atr=2.0)

        entry_date = datetime.today() - timedelta(days=3)

        pos = _pos(
            ticker=ticker,
            entry_price=100.0,
            stop_price=80.0,
            take_profit=150.0,    # 110 < 150 → not hit
            entry_date=entry_date,
        )

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])

        assert exits == [], (
            f"No exits expected (close≈110 < tp=150, trailing silent, "
            f"hard stop not hit, days_held≈3 < 20). Got: {exits}"
        )

    def test_tp_disabled_when_mult_is_zero(self):
        """With profit_target_atr_mult=0, TP block is skipped entirely."""
        ticker = "TEST"
        strategy = _strategy(profit_target_atr_mult=0.0)

        n = 40
        prices = np.linspace(100.0, 150.0, n)
        df = _make_df(n, close_values=prices, atr=2.0)

        entry_date = datetime.today() - timedelta(days=3)

        pos = _pos(
            ticker=ticker,
            entry_price=100.0,
            stop_price=80.0,
            take_profit=115.0,    # mult=0 disables the check
            entry_date=entry_date,
        )

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])
        reasons = [e["reason"] for e in exits]

        assert "take_profit" not in reasons, (
            f"take_profit must not fire when profit_target_atr_mult=0. Got: {reasons}"
        )


# ---------------------------------------------------------------------------
# Test 4: hard stop fires first and suppresses all other exits
# ---------------------------------------------------------------------------

class TestHardStopPriority:
    """Hard stop is priority 1 — it fires before TP, trailing, and time exit."""

    def test_hard_stop_beats_trailing_and_time(self):
        """stop_hit fires and the position emits exactly one exit."""
        ticker = "TEST"
        strategy = _strategy(
            profit_target_atr_mult=6.0,
            trailing_stop_atr_mult=4.0,
            max_hold_days=5,    # days_held will far exceed this
        )

        n = 40
        # today_close ≈ 70, well below stop_price=80
        # Also below trailing stop (peak≈100, ATR=2 → trail=92)
        prices = np.linspace(100.0, 70.0, n)
        df = _make_df(n, close_values=prices, atr=2.0)

        # Entry well before df to maximise days_held
        entry_date = df.index[0].to_pydatetime() - timedelta(days=1)

        pos = _pos(
            ticker=ticker,
            entry_price=100.0,
            stop_price=80.0,      # 70 <= 80 → stop fires
            take_profit=200.0,
            entry_date=entry_date,
        )

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])
        reasons = [e["reason"] for e in exits]

        assert "stop_hit" in reasons, (
            f"stop_hit must fire when close(≈70) <= stop_price(80). Got: {reasons}"
        )
        assert "trailing_stop" not in reasons, (
            f"trailing_stop must NOT appear when hard stop fires (continue). Got: {reasons}"
        )
        assert "time_exit" not in reasons, (
            f"time_exit must NOT appear when hard stop fires (continue). Got: {reasons}"
        )
        assert "take_profit" not in reasons, (
            f"take_profit must NOT appear when hard stop fires. Got: {reasons}"
        )
        assert len(exits) == 1, f"Hard stop + continue → exactly one exit. Got: {exits}"

    def test_hard_stop_is_always_first_check(self):
        """Source-code shape: stop_hit string appears before take_profit, trailing, time."""
        src = (Path(__file__).resolve().parent.parent / "strategies/momentum_breakout.py").read_text()
        idx = src.index("def check_exits")
        block = src[idx: idx + 4000]

        stop_idx   = block.index('"stop_hit"')
        tp_idx     = block.index('"take_profit"')
        trail_idx  = block.index('"trailing_stop"')
        time_idx   = block.index('"time_exit"')

        assert stop_idx < tp_idx < trail_idx < time_idx, (
            f"Priority order violated. Positions: stop={stop_idx}, tp={tp_idx}, "
            f"trail={trail_idx}, time={time_idx}"
        )


# ---------------------------------------------------------------------------
# Test 4b: trailing stop beats time exit when both conditions are met
# ---------------------------------------------------------------------------

class TestTrailingBeatsTimeExit:
    """Priority 3 (trailing_stop) fires before priority 4 (time_exit).

    When both trailing-stop and max-hold conditions are simultaneously true,
    the trailing_stop exit is returned and time_exit is suppressed by `continue`.
    Hard stop and TP are not hit so they don't interfere.
    """

    def test_trailing_stop_beats_time_exit(self):
        """Trailing stop is returned; time exit is suppressed by `continue`.

        Setup (DataFrame ends at today, 60 bars):
          profit_target_atr_mult = 6.0, trailing_stop_atr_mult = 4.0, ATR = 2.0
          max_hold_days = 10
          Prices: rally 100→120 (bars 0-29), then drop 120→108 (bars 30-59)

          entry_date = ONE DAY before bar-0 of the DataFrame.
          This anchors the mask to cover all 60 bars, so:
            highest_since_entry = 120.0 (peak at bar 30)
            trailing_stop = 120 − 4×2 = 112.0
            today_close = 108.0 ≤ 112.0 → trailing fires ✓

          days_held = (today − entry_date) ≈ 84+ calendar days ≥ max_hold_days=10
          → time exit WOULD fire if trailing did not suppress it.

          take_profit = 200.0 (not hit), stop_price = 80.0 (not hit).

        Expected: exactly one exit with reason "trailing_stop"; "time_exit" absent.

        NOTE: entry_date MUST be before bar-0 (not just 15 calendar days ago).
        If set 15 days ago the mask only sees bars 49-59: peak≈112 → trail≈104;
        close=108 > 104 so trailing does NOT fire and time_exit wins instead.
        Anchoring before bar-0 exposes the true peak of 120.
        """
        ticker = "TEST"
        strategy = _strategy(
            profit_target_atr_mult=6.0,
            trailing_stop_atr_mult=4.0,
            max_hold_days=10,
        )

        n = 60
        half = n // 2
        prices = np.concatenate([
            np.linspace(100.0, 120.0, half),      # rally
            np.linspace(120.0, 108.0, n - half),  # pullback
        ])
        df = _make_df(n, close_values=prices, atr=2.0)

        # One calendar day before bar-0 → mask covers ALL 60 bars
        # → peak=120, trail=112, close=108 ≤ 112 → trailing fires
        # days_held ≈ 84+ calendar days ≥ max_hold_days=10 (time would also fire)
        entry_date = df.index[0].to_pydatetime() - timedelta(days=1)

        pos = _pos(
            ticker=ticker,
            entry_price=100.0,
            stop_price=80.0,      # 108 > 80 → hard stop silent
            take_profit=200.0,    # 108 < 200 → TP silent
            entry_date=entry_date,
        )

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])
        reasons = [e["reason"] for e in exits]

        assert len(exits) == 1, (
            f"Exactly one exit expected (trailing_stop). Got {len(exits)}: {exits}"
        )
        assert exits[0]["reason"] == "trailing_stop", (
            f"trailing_stop (priority 3) must beat time_exit (priority 4). Got: {reasons}"
        )
        assert "time_exit" not in reasons, (
            f"time_exit must be suppressed by trailing_stop's continue. Got: {reasons}"
        )


# ---------------------------------------------------------------------------
# Test 5: at-most-one-exit-per-position invariant
# ---------------------------------------------------------------------------

class TestAtMostOneExitPerPosition:
    """Each position emits at most one exit per check_exits() call."""

    def test_multiple_conditions_met_emits_only_one_exit(self):
        """When stop, trailing, and time all would fire, only stop_hit is returned."""
        ticker = "TEST"
        strategy = _strategy(
            profit_target_atr_mult=6.0,
            trailing_stop_atr_mult=4.0,
            max_hold_days=5,
        )

        n = 40
        # Price crashed 100→60: below stop (80), below trailing, and days_held >> 5
        prices = np.linspace(100.0, 60.0, n)
        df = _make_df(n, close_values=prices, atr=2.0)

        # Entry before all bars so days_held >> max_hold_days
        entry_date = df.index[0].to_pydatetime() - timedelta(days=1)

        pos = _pos(
            ticker=ticker,
            entry_price=100.0,
            stop_price=80.0,
            take_profit=200.0,
            entry_date=entry_date,
        )

        exits = strategy.check_exits(data={ticker: df}, positions=[pos])

        assert len(exits) == 1, (
            f"At most one exit per position per call. Got {len(exits)}: {exits}"
        )
        assert exits[0]["reason"] == "stop_hit", (
            f"Hard stop is priority 1 — must be the one exit. Got: {exits[0]['reason']}"
        )
