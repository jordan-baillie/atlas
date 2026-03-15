"""Tests for strategies/entry_optimizer.py — no network required, < 5 s."""
from datetime import timedelta

import pandas as pd
import pytest

from strategies.entry_optimizer import (
    RefinedEntry,
    get_opening_range,
    refine_entry_prices,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_15min_bars(
    n: int = 8,
    open_: float = 100.0,
    high_mult: float = 1.01,
    low_mult: float = 0.99,
    tz: str = "America/New_York",
) -> pd.DataFrame:
    """Create a synthetic 15-min OHLCV DataFrame starting at market open (9:30 ET)."""
    start = pd.Timestamp("2026-01-05 09:30:00", tz=tz)
    index = pd.DatetimeIndex([start + timedelta(minutes=15 * i) for i in range(n)], tz=tz)
    data = {
        "open":   [open_] * n,
        "high":   [open_ * high_mult] * n,
        "low":    [open_ * low_mult] * n,
        "close":  [open_] * n,
        "volume": [10_000] * n,
    }
    return pd.DataFrame(data, index=index)


def make_signal(
    ticker: str = "AAPL",
    strategy: str = "mean_reversion",
    entry_price: float = 100.0,
    atr: float = 2.0,
) -> dict:
    return {
        "ticker": ticker,
        "strategy": strategy,
        "entry_price": entry_price,
        "features": {"atr_14": atr},
    }


EMPTY_CONFIG: dict = {}


# ─────────────────────────────────────────────────────────────────────────────
# RefinedEntry dataclass
# ─────────────────────────────────────────────────────────────────────────────

class TestRefinedEntry:
    def test_creation_full(self):
        """RefinedEntry can be constructed with all fields."""
        entry = RefinedEntry(
            ticker="AAPL",
            original_entry=150.0,
            refined_entry=148.5,
            order_type="limit",
            limit_price=148.5,
            reason="test reason",
            atr=2.5,
        )
        assert entry.ticker == "AAPL"
        assert entry.original_entry == 150.0
        assert entry.refined_entry == 148.5
        assert entry.order_type == "limit"
        assert entry.limit_price == 148.5
        assert entry.reason == "test reason"
        assert entry.atr == 2.5

    def test_defaults(self):
        """Optional fields default to sensible values."""
        entry = RefinedEntry(
            ticker="MSFT",
            original_entry=300.0,
            refined_entry=300.0,
            order_type="market",
        )
        assert entry.limit_price is None
        assert entry.reason == ""
        assert entry.atr == 0.0

    def test_market_entry_no_limit_price(self):
        """Market entries have no limit_price."""
        entry = RefinedEntry(
            ticker="TSLA", original_entry=200.0, refined_entry=200.0,
            order_type="market",
        )
        assert entry.limit_price is None


# ─────────────────────────────────────────────────────────────────────────────
# Empty signals list
# ─────────────────────────────────────────────────────────────────────────────

class TestEmptySignals:
    def test_empty_returns_empty(self):
        assert refine_entry_prices([], {}, EMPTY_CONFIG) == []


# ─────────────────────────────────────────────────────────────────────────────
# No intraday data / no ATR → market fallback
# ─────────────────────────────────────────────────────────────────────────────

class TestMarketFallback:
    def test_no_intraday_data(self):
        """All signals fall back to market when no bars provided."""
        signals = [
            make_signal("AAPL", "mean_reversion", atr=2.0),
            make_signal("MSFT", "momentum_breakout", atr=5.0),
        ]
        result = refine_entry_prices(signals, {}, EMPTY_CONFIG)
        assert len(result) == 2
        for ref in result:
            assert ref.order_type == "market"
            assert ref.reason == "no intraday data"
            assert ref.refined_entry == ref.original_entry
            assert ref.limit_price is None

    def test_ticker_missing_from_intraday(self):
        """Ticker not in intraday_data → market fallback."""
        signals = [make_signal("GOOGL", "mean_reversion", atr=3.0)]
        intraday = {"AAPL": make_15min_bars()}
        result = refine_entry_prices(signals, intraday, EMPTY_CONFIG)
        assert result[0].order_type == "market"
        assert result[0].reason == "no intraday data"

    def test_no_atr_market_fallback(self):
        """ATR = 0 causes market fallback even with bars."""
        signals = [make_signal("AAPL", "mean_reversion", atr=0.0)]
        result = refine_entry_prices(signals, {"AAPL": make_15min_bars()}, EMPTY_CONFIG)
        assert result[0].order_type == "market"
        assert result[0].reason == "no intraday data"

    def test_empty_bars_df(self):
        """Empty DataFrame → market fallback."""
        signals = [make_signal("AAPL", "mean_reversion", atr=2.0)]
        empty_df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        result = refine_entry_prices(signals, {"AAPL": empty_df}, EMPTY_CONFIG)
        assert result[0].order_type == "market"


# ─────────────────────────────────────────────────────────────────────────────
# MR strategies → limit orders
# ─────────────────────────────────────────────────────────────────────────────

class TestMRStrategies:
    @pytest.mark.parametrize(
        "strategy", ["mean_reversion", "connors_rsi2", "short_term_mr"]
    )
    def test_mr_returns_limit(self, strategy):
        """MR strategies produce DAY limit orders below opening low."""
        bars = make_15min_bars(n=8, open_=100.0, high_mult=1.01, low_mult=0.99)
        atr = 2.0
        signals = [make_signal("AAPL", strategy, entry_price=100.0, atr=atr)]

        result = refine_entry_prices(signals, {"AAPL": bars}, EMPTY_CONFIG)
        ref = result[0]

        assert ref.order_type == "limit"
        assert ref.limit_price is not None
        assert ref.limit_price < ref.original_entry        # limit < entry price
        assert ref.limit_price >= ref.original_entry - atr # not more than 1 ATR below
        assert "MR dip limit" in ref.reason

    def test_mr_limit_floor(self):
        """MR limit is floored at entry − 1 ATR (very deep dip protection)."""
        # Opening low is way below entry, so floor should kick in
        bars = make_15min_bars(n=8, open_=50.0, high_mult=1.01, low_mult=0.50)
        atr = 2.0
        entry = 100.0
        signals = [make_signal("AAPL", "mean_reversion", entry_price=entry, atr=atr)]

        result = refine_entry_prices(signals, {"AAPL": bars}, EMPTY_CONFIG)
        ref = result[0]

        assert ref.order_type == "limit"
        assert ref.limit_price >= entry - atr  # floor respected


# ─────────────────────────────────────────────────────────────────────────────
# Momentum strategies
# ─────────────────────────────────────────────────────────────────────────────

class TestMomentumStrategies:
    @pytest.mark.parametrize("strategy", ["momentum_breakout", "trend_following"])
    def test_breakout_confirmed_market(self, strategy):
        """30-min high > entry_price → breakout confirmed, market order."""
        # Opening high is 110 > entry of 105
        bars = make_15min_bars(n=8, open_=100.0, high_mult=1.10, low_mult=0.99)
        entry_price = 105.0
        signals = [make_signal("TSLA", strategy, entry_price=entry_price, atr=3.0)]

        result = refine_entry_prices(signals, {"TSLA": bars}, EMPTY_CONFIG)
        ref = result[0]

        assert ref.order_type == "market"
        assert ref.reason == "breakout confirmed"
        assert ref.refined_entry == entry_price
        assert ref.limit_price is None

    @pytest.mark.parametrize("strategy", ["momentum_breakout", "trend_following"])
    def test_no_breakout_limit(self, strategy):
        """30-min high <= entry_price → no breakout, limit at opening range high."""
        # Opening high is 101, entry is 105 → no breakout yet
        bars = make_15min_bars(n=8, open_=100.0, high_mult=1.01, low_mult=0.99)
        entry_price = 105.0
        signals = [make_signal("TSLA", strategy, entry_price=entry_price, atr=3.0)]

        result = refine_entry_prices(signals, {"TSLA": bars}, EMPTY_CONFIG)
        ref = result[0]

        assert ref.order_type == "limit"
        assert ref.limit_price is not None
        assert ref.limit_price > 0.0
        assert "breakout not confirmed" in ref.reason


# ─────────────────────────────────────────────────────────────────────────────
# Default / unknown strategies → market-on-open
# ─────────────────────────────────────────────────────────────────────────────

class TestDefaultStrategy:
    @pytest.mark.parametrize(
        "strategy", ["sector_rotation", "opening_gap", "unknown_strat", ""]
    )
    def test_default_market_on_open(self, strategy):
        """Unrecognised strategies use market-on-open."""
        bars = make_15min_bars()
        signals = [make_signal("AAPL", strategy, atr=2.0)]

        result = refine_entry_prices(signals, {"AAPL": bars}, EMPTY_CONFIG)
        ref = result[0]

        assert ref.order_type == "market"
        assert ref.reason == "default: market-on-open"
        assert ref.refined_entry == ref.original_entry


# ─────────────────────────────────────────────────────────────────────────────
# get_opening_range
# ─────────────────────────────────────────────────────────────────────────────

class TestGetOpeningRange:
    def test_empty_df_returns_zeros(self):
        """Empty DataFrame returns all-zero dict."""
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        result = get_opening_range(empty)
        assert result == {"open": 0.0, "high_30m": 0.0, "low_30m": 0.0, "range": 0.0}

    def test_none_returns_zeros(self):
        """None bars returns all-zero dict."""
        result = get_opening_range(None)
        assert result == {"open": 0.0, "high_30m": 0.0, "low_30m": 0.0, "range": 0.0}

    def test_basic_values(self):
        """Correct open/high/low/range from regular 15-min bars."""
        bars = make_15min_bars(n=4, open_=100.0, high_mult=1.02, low_mult=0.98)
        result = get_opening_range(bars, minutes=30)

        assert result["open"] == pytest.approx(100.0)
        assert result["high_30m"] == pytest.approx(102.0, rel=0.01)
        assert result["low_30m"] == pytest.approx(98.0, rel=0.01)
        assert result["range"] == pytest.approx(
            result["high_30m"] - result["low_30m"], rel=0.01
        )

    def test_custom_minutes_slicing(self):
        """minutes parameter correctly limits the window."""
        start = pd.Timestamp("2026-01-05 09:30:00", tz="America/New_York")
        index = pd.DatetimeIndex(
            [start + timedelta(minutes=15 * i) for i in range(8)],
            tz="America/New_York",
        )
        # First 2 bars (9:30, 9:45) → high=105, low=95
        # Later bars (10:00+)        → high=110, low=90
        highs = [105.0, 105.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0]
        lows  = [95.0,  95.0,  90.0,  90.0,  90.0,  90.0,  90.0,  90.0]
        df = pd.DataFrame(
            {"open": [100.0] * 8, "high": highs, "low": lows,
             "close": [100.0] * 8, "volume": [10_000] * 8},
            index=index,
        )

        r30 = get_opening_range(df, minutes=30)
        r60 = get_opening_range(df, minutes=60)

        # 30-min: only bars 9:30, 9:45 (570 ≤ bar_min < 600)
        assert r30["high_30m"] == pytest.approx(105.0)
        assert r30["low_30m"]  == pytest.approx(95.0)

        # 60-min: bars 9:30, 9:45, 10:00, 10:15 (570 ≤ bar_min < 630)
        assert r60["high_30m"] == pytest.approx(110.0)
        assert r60["low_30m"]  == pytest.approx(90.0)

    def test_range_is_high_minus_low(self):
        """range field equals high_30m - low_30m."""
        bars = make_15min_bars(n=2, open_=50.0, high_mult=1.05, low_mult=0.95)
        result = get_opening_range(bars, minutes=30)
        assert result["range"] == pytest.approx(
            result["high_30m"] - result["low_30m"], abs=1e-4
        )


# ─────────────────────────────────────────────────────────────────────────────
# Mixed-strategy bulk call
# ─────────────────────────────────────────────────────────────────────────────

class TestMixedStrategies:
    def test_multiple_signals_correct_types(self):
        """Bulk call returns one RefinedEntry per signal with correct order types."""
        bars_aapl = make_15min_bars(n=8, open_=100.0, high_mult=1.01, low_mult=0.99)
        bars_tsla = make_15min_bars(n=8, open_=200.0, high_mult=1.10, low_mult=0.99)

        signals = [
            # MR → limit
            make_signal("AAPL", "mean_reversion",   entry_price=100.0, atr=2.0),
            # Momentum, confirmed → market
            make_signal("TSLA", "momentum_breakout", entry_price=195.0, atr=4.0),
            # Default → market
            make_signal("MSFT", "sector_rotation",  entry_price=300.0, atr=5.0),
            # No intraday → market fallback
            make_signal("GOOGL", "mean_reversion",  entry_price=150.0, atr=3.0),
        ]
        intraday = {"AAPL": bars_aapl, "TSLA": bars_tsla}

        result = refine_entry_prices(signals, intraday, EMPTY_CONFIG)
        assert len(result) == 4

        aapl, tsla, msft, googl = result

        assert aapl.ticker == "AAPL" and aapl.order_type == "limit"
        assert tsla.ticker == "TSLA" and tsla.order_type == "market"
        assert msft.ticker == "MSFT" and msft.order_type == "market"
        assert googl.ticker == "GOOGL" and googl.order_type == "market"
        assert googl.reason == "no intraday data"

    def test_output_length_matches_input(self):
        """Output always has same length as input signals list."""
        signals = [make_signal(f"T{i}", "mean_reversion", atr=float(i + 1))
                   for i in range(10)]
        result = refine_entry_prices(signals, {}, EMPTY_CONFIG)
        assert len(result) == 10
