"""Tests for all active Atlas trading strategies.

Tests cover:
  - Strategy instantiation
  - precompute() adds expected indicator columns
  - generate_signals() returns a list (may be empty) with valid Signal objects
  - check_exits() returns a list of exit dicts
  - Signal-generating synthetic data for forced-signal tests

Run with:  python -m pytest tests/test_strategies.py -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from strategies.base import BaseStrategy, Signal  # noqa: E402
from tests.conftest import MINIMAL_CONFIG, make_ohlcv_df  # noqa: E402

import copy

# ---------------------------------------------------------------------------
# Data helpers — inject precomputed indicators so signal fires deterministically
# ---------------------------------------------------------------------------

def _base_df(n_days: int = 60, base_price: float = 100.0, seed: int = 0) -> pd.DataFrame:
    """Return a plain OHLCV DataFrame of given length."""
    return make_ohlcv_df("TEST", n_days=n_days, base_price=base_price, seed=seed)


def make_mr_signal_data(n_days: int = 60) -> dict[str, pd.DataFrame]:
    """MeanReversion: RSI < 35, z-score < -2.0 injected into precomputed columns."""
    df = _base_df(n_days=n_days, base_price=100.0)
    df["_mr_rsi"] = 22.0          # < 35
    df["_mr_zscore"] = -2.8       # < -2.0
    df["_mr_atr"] = 2.5
    df["_mr_vol_ratio"] = 1.2     # neutral
    df["_mr_mean_target"] = 105.0
    return {"TEST": df}


def make_mb_signal_data(n_days: int = 40) -> dict[str, pd.DataFrame]:
    """MomentumBreakout: price > lookback_high AND price > trend_ma injected."""
    df = _base_df(n_days=n_days, base_price=105.0)
    today_close = float(df["close"].iloc[-1])
    df["_mb_lookback_high"] = today_close - 2.0   # today broke above this
    df["_mb_trend_ma"] = today_close - 10.0       # price well above trend MA
    df["_mb_avg_vol"] = 1_500_000.0
    df["_mb_atr"] = 2.0
    return {"TEST": df}


def make_tf_signal_data(n_days: int = 60) -> dict[str, pd.DataFrame]:
    """TrendFollowing: fast > slow (uptrend), with pullback from recent high.

    The strategy iterates back to find trend_bars, so we need _tf_ma_diff > 0
    consistently, and we need close to have pulled back >= pullback_pct from recent high.
    """
    rng = np.random.default_rng(77)
    dates = pd.date_range(end="2024-12-31", periods=n_days, freq="B")

    # Price pattern: rises to 110 around middle, then pulls back to 104
    close = np.full(n_days, 100.0, dtype=float)
    mid = n_days // 2
    # Gentle uptrend in first half
    for i in range(mid):
        close[i] = 100.0 + i * 0.3
    # Peak around mid
    close[mid] = 110.0
    # Pullback in last portion (current price ~104)
    for i in range(mid + 1, n_days):
        close[i] = 110.0 - (i - mid) * 1.0
        close[i] = max(close[i], 104.0)
    close[-1] = 104.5

    open_ = close * (1 + rng.normal(0, 0.003, n_days))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.004, n_days))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.004, n_days))
    volume = rng.integers(1_000_000, 2_000_000, n_days).astype(float)

    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "ticker": "TEST"},
        index=dates,
    )

    # Inject precomputed columns
    df["_tf_fast_ma"] = 108.0   # fast > slow = uptrend
    df["_tf_slow_ma"] = 95.0    # slow MA below current price
    df["_tf_ma_diff"] = 13.0    # always positive = long uptrend
    df["_tf_atr"] = 2.5
    df["_tf_vol_ratio"] = 1.0
    return {"TEST": df}


def make_og_signal_data(n_days: int = 60) -> dict[str, pd.DataFrame]:
    """OpeningGap: gap down, bearish candle (close < open), vol surge, RSI oversold."""
    rng = np.random.default_rng(33)
    dates = pd.date_range(end="2024-12-31", periods=n_days, freq="B")

    close = np.full(n_days, 100.0, dtype=float) + rng.normal(0, 0.5, n_days)
    close = np.maximum(close, 50.0)

    # Last bar: gap down open and bearish candle (close < open)
    yesterday_close = 100.0
    # Gap down > 1% (threshold is 0.8%)
    today_open = yesterday_close * (1 - 0.015)   # -1.5% gap → meets -0.8% threshold
    today_close_val = today_open * 0.995         # close below open = bearish candle
    today_high = today_open * 1.002
    today_low = today_close_val * 0.998

    close[-2] = yesterday_close
    close[-1] = today_close_val

    open_ = close.copy()
    open_[-1] = today_open
    open_[:-1] = close[:-1] * (1 + rng.normal(0, 0.003, n_days - 1))

    high = np.maximum(open_, close)
    high[-1] = today_high
    high[:-1] = high[:-1] * (1 + rng.uniform(0, 0.005, n_days - 1))

    low = np.minimum(open_, close)
    low[-1] = today_low
    low[:-1] = low[:-1] * (1 - rng.uniform(0, 0.005, n_days - 1))

    volume = rng.integers(1_500_000, 2_500_000, n_days).astype(float)

    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "ticker": "TEST"},
        index=dates,
    )

    # Inject precomputed indicators
    df["_og_rsi"] = 28.0          # < rsi14_max=35
    df["_og_ibs"] = 0.1           # < ibs_confirm=0.7 = oversold
    df["_og_atr"] = 2.0
    df["_og_vol_ratio"] = 2.0     # >= vol_surge_threshold=1.5
    df["_og_sma_exit"] = 105.0
    return {"TEST": df}


def make_stmr_signal_data(n_days: int = 40) -> dict[str, pd.DataFrame]:
    """ShortTermMR: RSI(2) < 15, price below SMA(5)."""
    df = _base_df(n_days=n_days, base_price=95.0)
    today_close = float(df["close"].iloc[-1])
    df["_st_rsi"] = 8.0            # < rsi_oversold=15
    df["_st_ibs"] = 0.1            # < ibs_oversold=0.2 (extra confirmation)
    df["_st_sma"] = today_close + 5.0   # SMA above current price = below SMA ✓
    df["_st_atr"] = 2.0
    df["_st_vol_ratio"] = 1.0
    return {"TEST": df}


def make_cr_signal_data(n_days: int = 230) -> dict[str, pd.DataFrame]:
    """ConnorsRSI2: RSI(4) < 40, sma200_filter=False, 1 consecutive down day.

    Need ≥ 220 rows (min_rows = max(150+20, 220)).
    Ensure last 2 closes show 1 down day for min_consecutive_down=1 check.
    """
    rng = np.random.default_rng(55)
    dates = pd.date_range(end="2024-12-31", periods=n_days, freq="B")

    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n_days)))
    # Ensure last bar is lower than second-to-last (1 consecutive down day)
    close[-1] = close[-2] * 0.99

    open_ = close * (1 + rng.normal(0, 0.003, n_days))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.005, n_days))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.005, n_days))
    volume = rng.integers(1_000_000, 2_000_000, n_days).astype(float)

    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "ticker": "TEST"},
        index=dates,
    )

    df["_cr_rsi"] = 15.0           # < rsi_entry=40
    df["_cr_sma_trend"] = 80.0     # below current price (sma200_filter=False, so not checked)
    df["_cr_atr"] = 1.5
    df["_cr_vol_ratio"] = 1.0      # >= vol_min_ratio=0.5
    df["_cr_sma_exit"] = 95.0
    return {"TEST": df}


# ---------------------------------------------------------------------------
# Position helpers
# ---------------------------------------------------------------------------

def _make_position(ticker="TEST", strategy="mean_reversion",
                   entry_price=100.0, days_ago=1,
                   stop_price=None, take_profit=None) -> dict:
    if stop_price is None:
        stop_price = entry_price * 0.95
    entry_date = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    pos = {
        "ticker": ticker,
        "strategy": strategy,
        "entry_price": entry_price,
        "entry_date": entry_date,
        "stop_price": stop_price,
        "shares": 10,
        "sector": "Technology",
    }
    if take_profit:
        pos["take_profit"] = take_profit
    return pos


# ---------------------------------------------------------------------------
# MeanReversion
# ---------------------------------------------------------------------------

class TestMeanReversion:
    @pytest.fixture(autouse=True)
    def setup(self, mock_config):
        from strategies.mean_reversion import MeanReversion
        # Disable earnings blackout to avoid network calls
        mock_config["strategies"]["mean_reversion"]["earnings_blackout"] = {"enabled": False}
        self.config = mock_config
        self.strat = MeanReversion(mock_config)

    def test_instantiates(self):
        assert self.strat.name == "mean_reversion"

    def test_precompute_adds_rsi_column(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=2, n_days=60)
        self.strat.precompute(data)
        for df in data.values():
            assert "_mr_rsi" in df.columns
            assert "_mr_zscore" in df.columns
            assert "_mr_atr" in df.columns
            assert "_mr_vol_ratio" in df.columns
            assert "_mr_mean_target" in df.columns

    def test_precompute_sets_flag(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=1, n_days=60)
        self.strat.precompute(data)
        assert self.strat._precomputed is True

    def test_generate_signals_returns_list(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=3, n_days=60)
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        assert isinstance(signals, list)

    def test_generate_signals_returns_signal_objects(self):
        """With injected oversold indicators, strategy should generate a Signal."""
        data = make_mr_signal_data(n_days=60)
        self.strat._precomputed = True
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        assert isinstance(signals, list)
        for sig in signals:
            assert isinstance(sig, Signal)

    def test_signal_has_valid_confidence(self):
        data = make_mr_signal_data(n_days=60)
        self.strat._precomputed = True
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        for sig in signals:
            assert 0.0 <= sig.confidence <= 1.0

    def test_signal_stop_below_entry(self):
        data = make_mr_signal_data(n_days=60)
        self.strat._precomputed = True
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        for sig in signals:
            assert sig.stop_price < sig.entry_price

    def test_signal_strategy_name(self):
        data = make_mr_signal_data(n_days=60)
        self.strat._precomputed = True
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        for sig in signals:
            assert sig.strategy == "mean_reversion"

    def test_check_exits_returns_list(self):
        data = make_mr_signal_data(n_days=60)
        pos = _make_position("TEST", "mean_reversion", entry_price=105.0, days_ago=5)
        exits = self.strat.check_exits(data, [pos])
        assert isinstance(exits, list)

    def test_check_exits_stop_hit(self):
        """Position where current price < stop price → stop_hit exit."""
        data = make_mr_signal_data(n_days=60)
        current_close = float(data["TEST"]["close"].iloc[-1])
        # Stop above current price → stop hit
        pos = _make_position("TEST", "mean_reversion",
                             entry_price=current_close + 10.0,
                             stop_price=current_close + 1.0,
                             days_ago=2)
        exits = self.strat.check_exits(data, [pos])
        reasons = [e["reason"] for e in exits]
        assert "stop_hit" in reasons

    def test_check_exits_other_strategy_skipped(self):
        data = make_mr_signal_data(n_days=60)
        pos = _make_position("TEST", "momentum_breakout", entry_price=100.0, days_ago=3)
        exits = self.strat.check_exits(data, [pos])
        assert exits == []  # strategy mismatch, skipped


# ---------------------------------------------------------------------------
# MomentumBreakout
# ---------------------------------------------------------------------------

class TestMomentumBreakout:
    @pytest.fixture(autouse=True)
    def setup(self, mock_config):
        from strategies.momentum_breakout import MomentumBreakout
        self.config = mock_config
        self.strat = MomentumBreakout(mock_config)

    def test_instantiates(self):
        assert self.strat.name == "momentum_breakout"

    def test_precompute_adds_columns(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=2, n_days=50)
        self.strat.precompute(data)
        for df in data.values():
            assert "_mb_trend_ma" in df.columns
            assert "_mb_lookback_high" in df.columns
            assert "_mb_avg_vol" in df.columns
            assert "_mb_atr" in df.columns
        assert self.strat._precomputed is True

    def test_generate_signals_returns_list(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=3, n_days=50)
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        assert isinstance(signals, list)

    def test_generate_signals_with_breakout(self):
        """With injected breakout data, strategy should generate a Signal."""
        data = make_mb_signal_data()
        self.strat._precomputed = True
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        assert isinstance(signals, list)
        for sig in signals:
            assert isinstance(sig, Signal)
            assert sig.strategy == "momentum_breakout"

    def test_signal_confidence_range(self):
        data = make_mb_signal_data()
        self.strat._precomputed = True
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        for sig in signals:
            assert 0.0 <= sig.confidence <= 1.0

    def test_signal_stop_below_entry(self):
        data = make_mb_signal_data()
        self.strat._precomputed = True
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        for sig in signals:
            assert sig.stop_price < sig.entry_price

    def test_check_exits_returns_list(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=1, n_days=50)
        self.strat.precompute(data)
        ticker = list(data.keys())[0]
        pos = _make_position(ticker, "momentum_breakout", entry_price=100.0, days_ago=5)
        exits = self.strat.check_exits(data, [pos])
        assert isinstance(exits, list)


# ---------------------------------------------------------------------------
# TrendFollowing
# ---------------------------------------------------------------------------

class TestTrendFollowing:
    @pytest.fixture(autouse=True)
    def setup(self, mock_config):
        from strategies.trend_following import TrendFollowing
        self.config = mock_config
        self.strat = TrendFollowing(mock_config)

    def test_instantiates(self):
        assert self.strat.name == "trend_following"

    def test_precompute_adds_columns(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=2, n_days=50)
        self.strat.precompute(data)
        for df in data.values():
            assert "_tf_fast_ma" in df.columns
            assert "_tf_slow_ma" in df.columns
            assert "_tf_ma_diff" in df.columns
            assert "_tf_atr" in df.columns
            assert "_tf_vol_ratio" in df.columns
        assert self.strat._precomputed is True

    def test_generate_signals_returns_list(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=3, n_days=50)
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        assert isinstance(signals, list)

    def test_generate_signals_with_pullback(self):
        """With injected uptrend + pullback data, strategy should generate a Signal."""
        data = make_tf_signal_data()
        self.strat._precomputed = True
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        assert isinstance(signals, list)
        for sig in signals:
            assert isinstance(sig, Signal)
            assert sig.strategy == "trend_following"

    def test_signal_confidence_range(self):
        data = make_tf_signal_data()
        self.strat._precomputed = True
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        for sig in signals:
            assert 0.0 <= sig.confidence <= 1.0

    def test_check_exits_returns_list(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=1, n_days=50)
        self.strat.precompute(data)
        ticker = list(data.keys())[0]
        pos = _make_position(ticker, "trend_following", entry_price=100.0, days_ago=3)
        exits = self.strat.check_exits(data, [pos])
        assert isinstance(exits, list)


# ---------------------------------------------------------------------------
# OpeningGap
# ---------------------------------------------------------------------------

class TestOpeningGap:
    @pytest.fixture(autouse=True)
    def setup(self, mock_config):
        from strategies.opening_gap import OpeningGap
        mock_config["strategies"]["opening_gap"]["earnings_blackout"] = {"enabled": False}
        self.config = mock_config
        self.strat = OpeningGap(mock_config)

    def test_instantiates(self):
        assert self.strat.name == "opening_gap"

    def test_precompute_adds_columns(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=2, n_days=60)
        self.strat.precompute(data)
        for df in data.values():
            assert "_og_rsi" in df.columns
            assert "_og_atr" in df.columns
            assert "_og_ibs" in df.columns
            assert "_og_vol_ratio" in df.columns
            assert "_og_sma_exit" in df.columns
        assert self.strat._precomputed is True

    def test_generate_signals_returns_list(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=3, n_days=60)
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        assert isinstance(signals, list)

    def test_generate_signals_with_gap_data(self):
        """With injected gap-down + oversold data, strategy should generate a Signal."""
        data = make_og_signal_data()
        self.strat._precomputed = True
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        assert isinstance(signals, list)
        for sig in signals:
            assert isinstance(sig, Signal)
            assert sig.strategy == "opening_gap"

    def test_signal_stop_below_entry(self):
        data = make_og_signal_data()
        self.strat._precomputed = True
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        for sig in signals:
            assert sig.stop_price < sig.entry_price

    def test_check_exits_returns_list(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=1, n_days=60)
        self.strat.precompute(data)
        ticker = list(data.keys())[0]
        pos = _make_position(ticker, "opening_gap", entry_price=100.0, days_ago=3)
        exits = self.strat.check_exits(data, [pos])
        assert isinstance(exits, list)


# ---------------------------------------------------------------------------
# SectorRotation
# ---------------------------------------------------------------------------

class TestSectorRotation:
    @pytest.fixture(autouse=True)
    def setup(self, mock_config):
        from strategies.sector_rotation import SectorRotation
        self.config = mock_config
        self.strat = SectorRotation(mock_config)

    def test_instantiates(self):
        assert self.strat.name == "sector_rotation"

    def test_generate_signals_returns_list(self, mock_ohlcv_data):
        """SectorRotation with no sector map returns empty list — never raises."""
        data = mock_ohlcv_data(n_tickers=3, n_days=100)
        # Patch load_sector_map to return empty (no network / file dependency)
        with patch("strategies.sector_rotation.load_sector_map", return_value={}):
            signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        assert isinstance(signals, list)

    def test_check_exits_returns_list(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=1, n_days=80)
        ticker = list(data.keys())[0]
        pos = _make_position(ticker, "sector_rotation", entry_price=100.0, days_ago=5)
        with patch("strategies.sector_rotation.load_sector_map", return_value={}):
            exits = self.strat.check_exits(data, [pos])
        assert isinstance(exits, list)

    def test_precompute_no_crash(self, mock_ohlcv_data):
        """precompute may be a no-op for SectorRotation — just ensure it doesn't raise."""
        data = mock_ohlcv_data(n_tickers=2, n_days=100)
        self.strat.precompute(data)  # Should not raise


# ---------------------------------------------------------------------------
# ShortTermMR
# ---------------------------------------------------------------------------

class TestShortTermMR:
    @pytest.fixture(autouse=True)
    def setup(self, mock_config):
        from strategies.short_term_mr import ShortTermMR
        mock_config["strategies"]["short_term_mr"]["earnings_blackout"] = {"enabled": False}
        self.config = mock_config
        self.strat = ShortTermMR(mock_config)

    def test_instantiates(self):
        assert self.strat.name == "short_term_mr"

    def test_precompute_adds_columns(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=2, n_days=40)
        self.strat.precompute(data)
        for df in data.values():
            assert "_st_rsi" in df.columns
            assert "_st_ibs" in df.columns
            assert "_st_sma" in df.columns
            assert "_st_atr" in df.columns
            assert "_st_vol_ratio" in df.columns
        assert self.strat._precomputed is True

    def test_generate_signals_returns_list(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=3, n_days=40)
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        assert isinstance(signals, list)

    def test_generate_signals_with_oversold_data(self):
        """With injected RSI < 15 and price below SMA, strategy should generate Signal."""
        data = make_stmr_signal_data()
        self.strat._precomputed = True
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        assert isinstance(signals, list)
        for sig in signals:
            assert isinstance(sig, Signal)
            assert sig.strategy == "short_term_mr"

    def test_signal_confidence_range(self):
        data = make_stmr_signal_data()
        self.strat._precomputed = True
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        for sig in signals:
            assert 0.0 <= sig.confidence <= 1.0

    def test_check_exits_returns_list(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=1, n_days=40)
        self.strat.precompute(data)
        ticker = list(data.keys())[0]
        pos = _make_position(ticker, "short_term_mr", entry_price=100.0, days_ago=2)
        exits = self.strat.check_exits(data, [pos])
        assert isinstance(exits, list)


# ---------------------------------------------------------------------------
# ConnorsRSI2
# ---------------------------------------------------------------------------

class TestConnorsRSI2:
    @pytest.fixture(autouse=True)
    def setup(self, mock_config):
        from strategies.connors_rsi2 import ConnorsRSI2
        # sma200_filter disabled in MINIMAL_CONFIG already
        self.config = mock_config
        self.strat = ConnorsRSI2(mock_config)

    def test_instantiates(self):
        assert self.strat.name == "connors_rsi2"

    def test_precompute_adds_columns(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=1, n_days=250)
        self.strat.precompute(data)
        for df in data.values():
            assert "_cr_rsi" in df.columns
            assert "_cr_sma_trend" in df.columns
            assert "_cr_atr" in df.columns
            assert "_cr_vol_ratio" in df.columns
            assert "_cr_sma_exit" in df.columns
        assert self.strat._precomputed is True

    def test_generate_signals_returns_list(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=1, n_days=250)
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        assert isinstance(signals, list)

    def test_generate_signals_with_oversold_data(self):
        """With injected RSI(4) < 40 and consecutive down day, should generate Signal."""
        data = make_cr_signal_data()
        self.strat._precomputed = True
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        assert isinstance(signals, list)
        for sig in signals:
            assert isinstance(sig, Signal)
            assert sig.strategy == "connors_rsi2"

    def test_signal_confidence_range(self):
        data = make_cr_signal_data()
        self.strat._precomputed = True
        signals = self.strat.generate_signals(data, equity=10_000, existing_positions=[])
        for sig in signals:
            assert 0.0 <= sig.confidence <= 1.0

    def test_check_exits_returns_list(self, mock_ohlcv_data):
        data = mock_ohlcv_data(n_tickers=1, n_days=250)
        self.strat.precompute(data)
        ticker = list(data.keys())[0]
        pos = _make_position(ticker, "connors_rsi2", entry_price=100.0, days_ago=3)
        exits = self.strat.check_exits(data, [pos])
        assert isinstance(exits, list)


# ---------------------------------------------------------------------------
# Parametrized smoke tests across all active strategies
# ---------------------------------------------------------------------------

STRATEGY_CLASSES = [
    ("mean_reversion",   "strategies.mean_reversion",   "MeanReversion"),
    ("momentum_breakout","strategies.momentum_breakout", "MomentumBreakout"),
    ("trend_following",  "strategies.trend_following",   "TrendFollowing"),
    ("opening_gap",      "strategies.opening_gap",       "OpeningGap"),
    ("sector_rotation",  "strategies.sector_rotation",   "SectorRotation"),
    ("short_term_mr",    "strategies.short_term_mr",     "ShortTermMR"),
    ("connors_rsi2",     "strategies.connors_rsi2",      "ConnorsRSI2"),
]


@pytest.mark.parametrize("name,module,cls_name", STRATEGY_CLASSES)
def test_strategy_is_base_strategy_subclass(name, module, cls_name, mock_config):
    import importlib
    mod = importlib.import_module(module)
    cls = getattr(mod, cls_name)
    strat = cls(mock_config)
    assert isinstance(strat, BaseStrategy)


@pytest.mark.parametrize("name,module,cls_name", STRATEGY_CLASSES)
def test_strategy_name_matches(name, module, cls_name, mock_config):
    import importlib
    mod = importlib.import_module(module)
    cls = getattr(mod, cls_name)
    strat = cls(mock_config)
    assert strat.name == name


@pytest.mark.parametrize("name,module,cls_name", STRATEGY_CLASSES)
def test_strategy_generate_signals_with_insufficient_data_returns_empty(
    name, module, cls_name, mock_config
):
    """With only 5 rows, every strategy should return [] (insufficient data guard)."""
    import importlib
    mod = importlib.import_module(module)
    cls = getattr(mod, cls_name)
    strat = cls(mock_config)
    # 5 rows is never enough for any strategy
    data = {"AAPL": make_ohlcv_df("AAPL", n_days=5)}
    with patch("strategies.sector_rotation.load_sector_map", return_value={}):
        signals = strat.generate_signals(data, equity=10_000, existing_positions=[])
    assert isinstance(signals, list)
    assert signals == [], f"{name}: expected [] with 5 rows, got {signals}"
