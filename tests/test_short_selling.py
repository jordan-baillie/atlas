"""Tests for short selling support across Signal, MeanReversion strategy, and broker."""

import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from strategies.base import Signal
from strategies.mean_reversion import MeanReversion


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

def _make_signal(**kwargs):
    """Helper to create a Signal with sensible defaults."""
    defaults = dict(
        ticker="TEST",
        strategy="mean_reversion",
        direction="long",
        entry_price=100.0,
        stop_price=95.0,
        take_profit=110.0,
        position_size=10,
        position_value=1000.0,
        risk_amount=50.0,
        confidence=0.8,
        rationale="test signal",
        features={},
    )
    defaults.update(kwargs)
    return Signal(**defaults)


def _make_ohlcv(
    n: int = 250,
    base_price: float = 100.0,
    rsi_mode: str = "normal",  # "oversold", "overbought", "normal"
) -> pd.DataFrame:
    """Create synthetic OHLCV DataFrame with configurable RSI regime."""
    np.random.seed(42)
    # Use a fixed Monday start so business-day range always yields exactly n dates
    dates = pd.date_range(start="2020-01-06", periods=n, freq="B")

    if rsi_mode == "oversold":
        # Consistent downtrend to create oversold RSI < 30
        changes = np.random.uniform(-0.025, -0.005, n)
    elif rsi_mode == "overbought":
        # Consistent uptrend to create overbought RSI > 70
        changes = np.random.uniform(0.005, 0.025, n)
    else:
        changes = np.random.normal(0, 0.01, n)

    prices = [base_price]
    for c in changes[1:]:
        prices.append(max(1.0, prices[-1] * (1 + c)))

    prices = np.array(prices)
    high = prices * 1.01
    low = prices * 0.99
    volume = np.full(n, 1_000_000)

    df = pd.DataFrame(
        {"open": prices, "high": high, "low": low, "close": prices, "adj_close": prices, "volume": volume},
        index=dates,
    )
    df["ticker"] = "TEST"
    return df


def _make_config(short_enabled: bool = False, sma200_filter: bool = False) -> dict:
    """Create minimal Atlas config for MeanReversion strategy."""
    return {
        "strategies": {
            "mean_reversion": {
                "rsi_period": 14,
                "rsi_oversold": 30,
                "zscore_lookback": 20,
                "zscore_entry": -2.0,
                "atr_period": 14,
                "atr_stop_mult": 2.0,
                "profit_target_atr_mult": 1.5,
                "max_hold_days": 5,
                "sma200_filter": sma200_filter,
                "ibs_max": 1.0,
                "volume_entry_min": 0.0,
                "short_enabled": short_enabled,
                "volume": {"lookback": 20, "min_ratio": 0.5, "surge_threshold": 2.0,
                           "surge_boost": 0.1, "dry_penalty": 0.15},
                "earnings_blackout": {"enabled": False},
            }
        },
        "risk": {
            "max_open_positions": 10,
            "max_sector_concentration": 3,
            "max_risk_per_trade_pct": 0.005,
        },
        "fees": {
            "commission_per_trade": 0.0,
            "commission_pct": 0.0,
            "min_position_value": 0.0,
        },
        "trading": {"live_safety": {"max_order_value": 0.0}},
    }


# ═══════════════════════════════════════════════════════════════
# Signal direction validation
# ═══════════════════════════════════════════════════════════════

class TestSignalDirectionValidation:
    def test_long_signal_valid(self):
        """Standard long signal with stop below entry and TP above entry."""
        s = _make_signal(direction="long", entry_price=100.0, stop_price=95.0, take_profit=110.0)
        assert s.direction == "long"
        assert s.stop_price < s.entry_price
        assert s.take_profit > s.entry_price

    def test_short_signal_valid(self):
        """Short signal with stop ABOVE entry and TP BELOW entry."""
        s = _make_signal(direction="short", entry_price=100.0, stop_price=105.0, take_profit=90.0)
        assert s.direction == "short"
        assert s.stop_price > s.entry_price
        assert s.take_profit < s.entry_price

    def test_short_stop_below_entry_raises(self):
        """Short stop below entry is invalid (would never be hit on adverse move)."""
        with pytest.raises(ValueError, match="must be above entry"):
            _make_signal(direction="short", entry_price=100.0, stop_price=95.0, take_profit=90.0)

    def test_short_take_profit_above_entry_raises(self):
        """Short TP above entry makes no sense (we profit when price falls)."""
        with pytest.raises(ValueError, match="must be below entry"):
            _make_signal(direction="short", entry_price=100.0, stop_price=105.0, take_profit=110.0)

    def test_short_no_take_profit(self):
        """Short signal with take_profit=None is valid."""
        s = _make_signal(direction="short", entry_price=100.0, stop_price=105.0, take_profit=None)
        assert s.direction == "short"
        assert s.take_profit is None

    def test_long_stop_above_entry_raises(self):
        """Long stop above entry is still invalid."""
        with pytest.raises(ValueError, match="must be below entry"):
            _make_signal(direction="long", entry_price=100.0, stop_price=105.0, take_profit=110.0)

    def test_long_take_profit_below_entry_raises(self):
        """Long TP below entry is still invalid."""
        with pytest.raises(ValueError, match="must be above entry"):
            _make_signal(direction="long", entry_price=100.0, stop_price=95.0, take_profit=90.0)

    def test_invalid_direction_raises(self):
        """Non-recognized direction raises ValueError."""
        with pytest.raises(ValueError, match="direction must be one of"):
            _make_signal(direction="invalid")

    def test_to_dict_includes_direction(self):
        """to_dict() must include direction for downstream serialization."""
        s = _make_signal(direction="short", entry_price=100.0, stop_price=105.0, take_profit=90.0)
        d = s.to_dict()
        assert d["direction"] == "short"

    def test_long_to_dict_includes_direction(self):
        d = _make_signal(direction="long").to_dict()
        assert d["direction"] == "long"


# ═══════════════════════════════════════════════════════════════
# MeanReversion short signal generation
# ═══════════════════════════════════════════════════════════════

class TestMeanReversionShortSignals:
    """Tests for _generate_short_signals() and the gate in generate_signals()."""

    def _make_injected_df(self, rsi_value: float, zscore_value: float,
                          close: float = 100.0, mean_20: float = 90.0) -> pd.DataFrame:
        """Build a DataFrame with pre-injected indicator columns.

        This bypasses RSI/z-score calculation issues (e.g. NaN on pure trends)
        by directly injecting the desired indicator values into precomputed columns.
        """
        n = 50
        dates = pd.date_range(start="2020-01-06", periods=n, freq="B")
        arr = np.full(n, close)
        df = pd.DataFrame({
            "open": arr, "high": arr * 1.005, "low": arr * 0.995,
            "close": arr, "adj_close": arr, "volume": np.full(n, 1_000_000),
        }, index=dates)
        df["ticker"] = "TEST"
        # Inject precomputed indicators directly
        df["_mr_rsi"] = rsi_value
        df["_mr_zscore"] = zscore_value
        df["_mr_atr"] = 2.0  # 2-point ATR for predictable stop distances
        df["_mr_vol_ratio"] = 1.0
        df["_mr_mean_target"] = mean_20
        return df

    def _run_generate_with_df(self, df: pd.DataFrame, short_enabled: bool,
                               equity: float = 100_000.0):
        """Run generate_signals() with a precomputed DataFrame."""
        config = _make_config(short_enabled=short_enabled)
        strategy = MeanReversion(config)
        strategy._precomputed = True
        data = {"TEST": df}
        return strategy.generate_signals(data, equity=equity, existing_positions=[])

    def test_short_enabled_false_no_short_signals(self):
        """short_enabled=False must never produce short signals even for overbought data."""
        df = self._make_injected_df(rsi_value=80.0, zscore_value=2.5, mean_20=90.0)
        signals = self._run_generate_with_df(df, short_enabled=False)
        short_signals = [s for s in signals if s.direction == "short"]
        assert len(short_signals) == 0, "Should not generate short signals when disabled"

    def test_overbought_data_generates_short_signals_when_enabled(self):
        """With overbought conditions and short_enabled=True, shorts should appear."""
        # RSI=80 > 70 (rsi_overbought), z-score=2.5 > 2.0, mean_20=90 < entry=100
        df = self._make_injected_df(rsi_value=80.0, zscore_value=2.5, close=100.0, mean_20=90.0)
        signals = self._run_generate_with_df(df, short_enabled=True)
        short_signals = [s for s in signals if s.direction == "short"]
        assert len(short_signals) == 1, (
            f"Expected 1 short signal for overbought data, got {len(short_signals)}"
        )

    def test_short_signal_stop_above_entry(self):
        """All short signals must have stop_price > entry_price."""
        df = self._make_injected_df(rsi_value=80.0, zscore_value=2.5, close=100.0, mean_20=90.0)
        signals = self._run_generate_with_df(df, short_enabled=True)
        short_signals = [s for s in signals if s.direction == "short"]
        assert len(short_signals) > 0
        for sig in short_signals:
            assert sig.stop_price > sig.entry_price, (
                f"Short stop {sig.stop_price} must be ABOVE entry {sig.entry_price}"
            )

    def test_short_signal_take_profit_below_entry(self):
        """All short signals must have take_profit < entry_price."""
        df = self._make_injected_df(rsi_value=80.0, zscore_value=2.5, close=100.0, mean_20=90.0)
        signals = self._run_generate_with_df(df, short_enabled=True)
        short_signals = [s for s in signals if s.direction == "short"]
        assert len(short_signals) > 0
        for sig in short_signals:
            if sig.take_profit is not None:
                assert sig.take_profit < sig.entry_price, (
                    f"Short TP {sig.take_profit} must be BELOW entry {sig.entry_price}"
                )

    def test_long_signals_unchanged_when_short_enabled(self):
        """Enabling shorts must not change long signals for oversold data."""
        # Oversold: RSI < 30, z-score < -2.0, mean_20 above close (for long)
        df = self._make_injected_df(rsi_value=20.0, zscore_value=-2.5, close=100.0, mean_20=110.0)
        sigs_long_only = self._run_generate_with_df(df, short_enabled=False)
        sigs_with_short = self._run_generate_with_df(df, short_enabled=True)

        long_only = [s for s in sigs_long_only if s.direction == "long"]
        with_short = [s for s in sigs_with_short if s.direction == "long"]
        assert len(long_only) == len(with_short), (
            "Long signal count must not change when short_enabled=True"
        )

    def test_oversold_data_no_short_signals(self):
        """Oversold conditions (RSI < 30, z < -2) should not trigger short signals."""
        df = self._make_injected_df(rsi_value=20.0, zscore_value=-2.5, close=100.0, mean_20=110.0)
        signals = self._run_generate_with_df(df, short_enabled=True)
        short_signals = [s for s in signals if s.direction == "short"]
        assert len(short_signals) == 0, (
            "Oversold data should NOT produce short signals"
        )

    def test_short_signal_confidence_in_range(self):
        """Short signal confidence must be in [0, 1]."""
        df = self._make_injected_df(rsi_value=80.0, zscore_value=2.5, close=100.0, mean_20=90.0)
        signals = self._run_generate_with_df(df, short_enabled=True)
        short_signals = [s for s in signals if s.direction == "short"]
        assert len(short_signals) > 0
        for sig in short_signals:
            assert 0.0 <= sig.confidence <= 1.0


# ═══════════════════════════════════════════════════════════════
# MeanReversion check_exits — short position handling
# ═══════════════════════════════════════════════════════════════

class TestMeanReversionShortExits:
    """Tests for short position exit logic in check_exits()."""

    def _make_strategy(self):
        config = _make_config(short_enabled=True)
        strategy = MeanReversion(config)
        strategy._precomputed = True
        return strategy

    def _make_df(self, prices: list) -> pd.DataFrame:
        """Build a DataFrame from a list of close prices."""
        n = len(prices)
        # Fixed Monday start ensures exactly n business-day dates
        dates = pd.date_range(start="2020-01-06", periods=n, freq="B")
        arr = np.array(prices, dtype=float)
        df = pd.DataFrame({
            "open": arr,
            "high": arr * 1.005,
            "low": arr * 0.995,
            "close": arr,
            "adj_close": arr,
            "volume": np.ones(n) * 1_000_000,
        }, index=dates)
        mean_20 = pd.Series(arr).rolling(20, min_periods=1).mean().values
        df["_mr_mean_target"] = mean_20
        return df

    # Last date in the fixed-date DataFrames (31 business days from 2020-01-06)
    _DF_END_DATE = pd.date_range(start="2020-01-06", periods=31, freq="B")[-1]

    def _make_short_position(self, entry_price=100.0, stop_price=105.0, take_profit=90.0, days_ago=2):
        entry = self._DF_END_DATE - timedelta(days=days_ago)
        return {
            "ticker": "TEST",
            "strategy": "mean_reversion",
            "direction": "short",
            "entry_price": entry_price,
            "stop_price": stop_price,
            "take_profit": take_profit,
            "shares": 10,
            "entry_date": entry.strftime("%Y-%m-%d"),
        }

    def test_short_stop_hit_when_price_rises(self):
        """Short position triggers stop_hit when price closes above stop_price."""
        strategy = self._make_strategy()
        # Current price 106 > stop 105
        prices = [100.0] * 30 + [106.0]
        df = self._make_df(prices)
        pos = self._make_short_position(entry_price=100.0, stop_price=105.0)
        exits = strategy.check_exits({"TEST": df}, [pos])
        assert len(exits) == 1
        assert exits[0]["reason"] == "stop_hit"

    def test_short_take_profit_when_price_falls(self):
        """Short position triggers take_profit when price drops to TP."""
        strategy = self._make_strategy()
        # Current price 89 <= TP 90
        prices = [100.0] * 30 + [89.0]
        df = self._make_df(prices)
        pos = self._make_short_position(entry_price=100.0, stop_price=105.0, take_profit=90.0)
        exits = strategy.check_exits({"TEST": df}, [pos])
        assert len(exits) == 1
        assert exits[0]["reason"] == "take_profit"

    def test_short_no_exit_when_between_limits(self):
        """No exit when price is between entry and stop/TP."""
        strategy = self._make_strategy()
        # Price at 97 — between TP (90) and stop (105), no reversion to mean yet
        prices = [100.0] * 30 + [97.0]
        df = self._make_df(prices)
        # Mean of the 30 prices = 100, so 97 < 100: mean reversion is close
        # Make mean higher than entry to avoid signal_exit
        prices_high_mean = [110.0] * 30 + [97.0]
        df2 = self._make_df(prices_high_mean)
        pos = self._make_short_position(entry_price=100.0, stop_price=105.0, take_profit=90.0, days_ago=1)
        exits = strategy.check_exits({"TEST": df2}, [pos])
        assert len(exits) == 0, f"Expected no exit, got: {exits}"

    def test_short_signal_exit_when_price_drops_to_mean(self):
        """Short exits via signal_exit when price drops to 20d mean from above."""
        strategy = self._make_strategy()
        # Build data where mean_20 ≈ 90, current price ≈ 89 (at/below mean)
        # entry_price = 100 (above mean_20)
        prices = [90.0] * 29 + [89.0]
        df = self._make_df(prices)
        # entry_price > mean_20 and today_close <= mean_20
        pos = self._make_short_position(entry_price=100.0, stop_price=110.0, take_profit=80.0, days_ago=2)
        # Override mean_20 column to be ~90
        df["_mr_mean_target"] = 90.0
        # today_close (89) <= mean_20 (90) and entry (100) > mean_20 (90) → signal_exit
        exits = strategy.check_exits({"TEST": df}, [pos])
        assert len(exits) == 1
        assert exits[0]["reason"] == "signal_exit"

    def test_short_time_exit(self):
        """Short position triggers time_exit when held too long."""
        strategy = self._make_strategy()
        # Price is 98 — no stop, TP, or mean reversion exit
        prices = [100.0] * 30 + [98.0]
        df = self._make_df(prices)
        df["_mr_mean_target"] = 50.0  # mean far below — no signal_exit
        # 10 days_ago > max_hold_days=5
        pos = self._make_short_position(
            entry_price=100.0, stop_price=110.0, take_profit=80.0, days_ago=10
        )
        exits = strategy.check_exits({"TEST": df}, [pos])
        assert len(exits) == 1
        assert exits[0]["reason"] == "time_exit"

    def test_long_position_unchanged_by_short_logic(self):
        """Long position stop_hit still works when short logic is present."""
        strategy = self._make_strategy()
        prices = [100.0] * 30 + [90.0]  # drops below long stop
        df = self._make_df(prices)
        entry = self._DF_END_DATE - timedelta(days=2)
        long_pos = {
            "ticker": "TEST",
            "strategy": "mean_reversion",
            "direction": "long",
            "entry_price": 100.0,
            "stop_price": 95.0,
            "take_profit": 115.0,
            "shares": 10,
            "entry_date": entry.strftime("%Y-%m-%d"),
        }
        exits = strategy.check_exits({"TEST": df}, [long_pos])
        assert len(exits) == 1
        assert exits[0]["reason"] == "stop_hit"

    def test_position_without_direction_defaults_to_long(self):
        """Positions without direction key default to long exit logic."""
        strategy = self._make_strategy()
        prices = [100.0] * 30 + [90.0]
        df = self._make_df(prices)
        entry = self._DF_END_DATE - timedelta(days=2)
        pos = {
            "ticker": "TEST",
            "strategy": "mean_reversion",
            # No 'direction' key
            "entry_price": 100.0,
            "stop_price": 95.0,
            "take_profit": 115.0,
            "shares": 10,
            "entry_date": entry.strftime("%Y-%m-%d"),
        }
        exits = strategy.check_exits({"TEST": df}, [pos])
        assert len(exits) == 1
        assert exits[0]["reason"] == "stop_hit"


# ═══════════════════════════════════════════════════════════════
# Broker: verify_shorting_enabled
# ═══════════════════════════════════════════════════════════════

class TestBrokerShortingEnabled:
    """Tests for AlpacaBroker.verify_shorting_enabled()."""

    def _make_broker(self):
        """Create a broker instance without connecting to Alpaca."""
        try:
            from brokers.alpaca.broker import AlpacaBroker
        except ImportError:
            pytest.skip("AlpacaBroker not available")

        config = {"alpaca": {"paper": True}, "risk": {}, "fees": {}}
        broker = AlpacaBroker(config)
        return broker

    def test_shorting_enabled_true(self):
        """Returns True when account.shorting_enabled is True."""
        broker = self._make_broker()
        mock_account = MagicMock()
        mock_account.shorting_enabled = True
        mock_client = MagicMock()
        mock_client.get_account.return_value = mock_account
        broker._trade_client = mock_client

        assert broker.verify_shorting_enabled() is True

    def test_shorting_enabled_false(self):
        """Returns False and logs warning when account.shorting_enabled is False."""
        broker = self._make_broker()
        mock_account = MagicMock()
        mock_account.shorting_enabled = False
        mock_client = MagicMock()
        mock_client.get_account.return_value = mock_account
        broker._trade_client = mock_client

        assert broker.verify_shorting_enabled() is False

    def test_shorting_enabled_attribute_missing(self):
        """Returns False when shorting_enabled attribute doesn't exist on account."""
        broker = self._make_broker()
        mock_account = MagicMock(spec=[])  # No attributes
        mock_client = MagicMock()
        mock_client.get_account.return_value = mock_account
        broker._trade_client = mock_client

        result = broker.verify_shorting_enabled()
        assert result is False

    def test_shorting_enabled_exception(self):
        """Returns False gracefully when get_account raises an exception."""
        broker = self._make_broker()
        mock_client = MagicMock()
        mock_client.get_account.side_effect = RuntimeError("API error")
        broker._trade_client = mock_client

        assert broker.verify_shorting_enabled() is False
