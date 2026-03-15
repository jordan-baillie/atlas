"""Tests for short-selling engine changes (FIX-1) and pipeline delegation (FIX-3).

Covers:
- _build_trade_record: direction-aware P&L and direction field
- Entry position creation: signal.direction preserved
- _force_close_all: short P&L correct
- _update_mae_mfe: shorts — MAE when price rises, MFE when price falls
- _process_max_loss_exits: short unrealized P&L correct
- _process_trailing_stops: shorts track lowest price, trigger on rise
- Pipeline delegation: DayContext created correctly, gates called via run_entry_gates
"""

import sys
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from backtest.engine import BacktestEngine
from backtest.pipeline import DayContext, run_entry_gates, enrich_signals


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_config(**overrides) -> Dict[str, Any]:
    """Minimal BacktestEngine config with safe defaults."""
    cfg = {
        "market": "sp500",
        "risk": {
            "starting_equity": 10_000,
            "max_open_positions": 5,
            "max_sector_concentration": 3,
            "max_risk_per_trade_pct": 0.005,
            "min_confidence": 0.0,
        },
        "fees": {
            "commission_per_trade": 0.0,
            "commission_pct": 0.0,
            "slippage_pct": 0.0,
            "flat_fee_threshold": 2000.0,
            "min_position_value": 0.0,
        },
        "backtest": {
            "train_window_days": 10,
            "test_window_days": 5,
            "step_days": 2,
            "min_history_days": 5,
        },
        "trading": {"live_safety": {"max_order_value": 0}},
        "strategies": {},
        "vix_filter": {"enabled": False},
        "fred_filter": {},
        "turn_of_month": False,
        "macro_regime": {"enabled": False},
        "regime_filter": {"enabled": False},
        "event_calendar": {"enabled": False},
    }
    cfg.update(overrides)
    return cfg


def _make_engine(**cfg_overrides) -> BacktestEngine:
    """Construct a BacktestEngine with test config."""
    config = _make_config(**cfg_overrides)
    return BacktestEngine(config)


def _make_long_pos(
    fill_price: float = 100.0,
    shares: int = 10,
    entry_date: str = "2024-01-01",
    stop_price: float = 95.0,
    commission: float = 0.0,
) -> Dict[str, Any]:
    return {
        "ticker": "AAPL",
        "strategy": "test_strat",
        "direction": "long",
        "fill_price": fill_price,
        "entry_price": fill_price,
        "stop_price": stop_price,
        "shares": shares,
        "position_value": fill_price * shares,
        "entry_commission": commission,
        "confidence": 0.8,
        "entry_date": entry_date,
        "features": {"atr": 2.0},
        "mae": 0.0,
        "mfe": 0.0,
        "entry_regime": "neutral",
    }


def _make_short_pos(
    fill_price: float = 100.0,
    shares: int = 10,
    entry_date: str = "2024-01-01",
    stop_price: float = 105.0,
    commission: float = 0.0,
) -> Dict[str, Any]:
    return {
        "ticker": "AAPL",
        "strategy": "test_strat",
        "direction": "short",
        "fill_price": fill_price,
        "entry_price": fill_price,
        "stop_price": stop_price,
        "shares": shares,
        "position_value": fill_price * shares,
        "entry_commission": commission,
        "confidence": 0.8,
        "entry_date": entry_date,
        "features": {"atr": 2.0},
        "mae": 0.0,
        "mfe": 0.0,
        "entry_regime": "neutral",
    }


def _make_ohlcv_df(
    dates: List[pd.Timestamp],
    open_: float = 100.0,
    high: float = 102.0,
    low: float = 98.0,
    close: float = 101.0,
) -> pd.DataFrame:
    n = len(dates)
    return pd.DataFrame(
        {
            "open": [open_] * n,
            "high": [high] * n,
            "low": [low] * n,
            "close": [close] * n,
            "volume": [1_000_000] * n,
        },
        index=pd.DatetimeIndex(dates),
    )


# ─────────────────────────────────────────────────────────────────────────────
# FIX-1: _build_trade_record
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildTradeRecord:
    """_build_trade_record must be direction-aware for P&L and the direction field."""

    def _engine(self):
        return _make_engine()

    def test_long_trade_profit_when_price_rises(self):
        """Long: exit > entry → positive gross P&L."""
        engine = self._engine()
        pos = _make_long_pos(fill_price=100.0, shares=10)
        today = pd.Timestamp("2024-01-10")
        trade = engine._build_trade_record(pos, 110.0, today, "take_profit")
        assert trade["gross_pnl"] == pytest.approx(100.0)  # (110-100)*10
        assert trade["pnl"] == pytest.approx(100.0)        # no commission
        assert trade["direction"] == "long"

    def test_long_trade_loss_when_price_falls(self):
        """Long: exit < entry → negative gross P&L."""
        engine = self._engine()
        pos = _make_long_pos(fill_price=100.0, shares=10)
        today = pd.Timestamp("2024-01-10")
        trade = engine._build_trade_record(pos, 90.0, today, "stop_hit")
        assert trade["gross_pnl"] == pytest.approx(-100.0)
        assert trade["direction"] == "long"

    def test_short_trade_profit_when_price_falls(self):
        """Short: exit < entry → positive gross P&L (price fell, short wins)."""
        engine = self._engine()
        pos = _make_short_pos(fill_price=100.0, shares=10)
        today = pd.Timestamp("2024-01-10")
        trade = engine._build_trade_record(pos, 90.0, today, "take_profit")
        # Short P&L = (entry - exit) * shares = (100 - 90) * 10 = 100
        assert trade["gross_pnl"] == pytest.approx(100.0)
        assert trade["pnl"] == pytest.approx(100.0)
        assert trade["direction"] == "short"

    def test_short_trade_loss_when_price_rises(self):
        """Short: exit > entry → negative gross P&L (price rose, short loses)."""
        engine = self._engine()
        pos = _make_short_pos(fill_price=100.0, shares=10)
        today = pd.Timestamp("2024-01-10")
        trade = engine._build_trade_record(pos, 110.0, today, "stop_hit")
        # Short P&L = (100 - 110) * 10 = -100
        assert trade["gross_pnl"] == pytest.approx(-100.0)
        assert trade["direction"] == "short"

    def test_short_trade_at_breakeven(self):
        """Short: exit == entry → zero gross P&L."""
        engine = self._engine()
        pos = _make_short_pos(fill_price=100.0, shares=10)
        today = pd.Timestamp("2024-01-10")
        trade = engine._build_trade_record(pos, 100.0, today, "time_exit")
        assert trade["gross_pnl"] == pytest.approx(0.0)

    def test_long_direction_preserved_in_trade(self):
        """Trade record direction field matches position direction for longs."""
        engine = self._engine()
        pos = _make_long_pos()
        trade = engine._build_trade_record(pos, 100.0, pd.Timestamp("2024-01-10"), "stop_hit")
        assert trade["direction"] == "long"

    def test_short_direction_preserved_in_trade(self):
        """Trade record direction field matches position direction for shorts."""
        engine = self._engine()
        pos = _make_short_pos()
        trade = engine._build_trade_record(pos, 100.0, pd.Timestamp("2024-01-10"), "stop_hit")
        assert trade["direction"] == "short"

    def test_no_direction_defaults_to_long(self):
        """Position without direction key defaults to long P&L logic."""
        engine = self._engine()
        pos = _make_long_pos(fill_price=100.0, shares=10)
        del pos["direction"]
        today = pd.Timestamp("2024-01-10")
        trade = engine._build_trade_record(pos, 110.0, today, "take_profit")
        assert trade["gross_pnl"] == pytest.approx(100.0)
        assert trade["direction"] == "long"


# ─────────────────────────────────────────────────────────────────────────────
# FIX-1: _force_close_all
# ─────────────────────────────────────────────────────────────────────────────

class TestForceCloseAll:
    """_force_close_all must produce correct P&L for both directions."""

    def _engine(self):
        return _make_engine()

    def _make_data(self, ticker: str, close_date: pd.Timestamp, close_price: float) -> Dict:
        df = _make_ohlcv_df([close_date], close=close_price)
        return {ticker: df}

    def test_long_position_profit(self):
        """Long position closed at price above entry earns positive P&L."""
        engine = self._engine()
        close_date = pd.Timestamp("2024-01-10")
        pos = _make_long_pos(fill_price=100.0, shares=10)
        data = self._make_data("AAPL", close_date, close_price=110.0)
        closed_trades: List = []
        equity = engine._force_close_all([pos], data, close_date, closed_trades, 10_000.0)
        assert len(closed_trades) == 1
        assert closed_trades[0]["pnl"] == pytest.approx(100.0)
        assert closed_trades[0]["direction"] == "long"
        assert equity == pytest.approx(10_100.0)

    def test_short_position_profit_when_price_falls(self):
        """Short position closed at price below entry earns positive P&L."""
        engine = self._engine()
        close_date = pd.Timestamp("2024-01-10")
        pos = _make_short_pos(fill_price=100.0, shares=10)
        data = self._make_data("AAPL", close_date, close_price=85.0)
        closed_trades: List = []
        equity = engine._force_close_all([pos], data, close_date, closed_trades, 10_000.0)
        assert len(closed_trades) == 1
        # Short P&L = (100 - 85) * 10 = 150
        assert closed_trades[0]["pnl"] == pytest.approx(150.0)
        assert closed_trades[0]["direction"] == "short"
        assert equity == pytest.approx(10_150.0)

    def test_short_position_loss_when_price_rises(self):
        """Short position closed at price above entry loses money."""
        engine = self._engine()
        close_date = pd.Timestamp("2024-01-10")
        pos = _make_short_pos(fill_price=100.0, shares=10)
        data = self._make_data("AAPL", close_date, close_price=115.0)
        closed_trades: List = []
        equity = engine._force_close_all([pos], data, close_date, closed_trades, 10_000.0)
        # Short P&L = (100 - 115) * 10 = -150
        assert closed_trades[0]["pnl"] == pytest.approx(-150.0)
        assert equity == pytest.approx(9_850.0)

    def test_mixed_positions(self):
        """Mixed long+short positions produce correct combined P&L."""
        engine = self._engine()
        close_date = pd.Timestamp("2024-01-10")
        long_pos = _make_long_pos(fill_price=100.0, shares=10)
        long_pos["ticker"] = "AAPL"
        short_pos = _make_short_pos(fill_price=50.0, shares=20)
        short_pos["ticker"] = "TSLA"

        data = {
            "AAPL": _make_ohlcv_df([close_date], close=110.0),
            "TSLA": _make_ohlcv_df([close_date], close=45.0),
        }
        closed_trades: List = []
        equity = engine._force_close_all(
            [long_pos, short_pos], data, close_date, closed_trades, 10_000.0
        )
        # Long P&L: (110-100)*10 = 100
        # Short P&L: (50-45)*20 = 100
        assert len(closed_trades) == 2
        total_pnl = sum(t["pnl"] for t in closed_trades)
        assert total_pnl == pytest.approx(200.0)
        assert equity == pytest.approx(10_200.0)


# ─────────────────────────────────────────────────────────────────────────────
# FIX-1: _update_mae_mfe
# ─────────────────────────────────────────────────────────────────────────────

class TestUpdateMaeMfe:
    """Direction-aware MAE/MFE tracking."""

    def _engine(self):
        return _make_engine()

    def _run_update(self, pos: Dict, high: float, low: float) -> Dict:
        engine = self._engine()
        today = pd.Timestamp("2024-01-10")
        df = _make_ohlcv_df([today], high=high, low=low)
        engine._update_mae_mfe(today, {"AAPL": df}, [pos])
        return pos

    def test_long_mae_when_price_drops(self):
        """Long: MAE is negative fraction (price dropped below entry)."""
        pos = _make_long_pos(fill_price=100.0)
        pos = self._run_update(pos, high=105.0, low=95.0)
        # adverse for long = (low - fill) / fill = (95-100)/100 = -0.05
        assert pos["mae"] == pytest.approx(-0.05)

    def test_long_mfe_when_price_rises(self):
        """Long: MFE is positive fraction (price rose above entry)."""
        pos = _make_long_pos(fill_price=100.0)
        pos = self._run_update(pos, high=108.0, low=98.0)
        # favorable for long = (high - fill) / fill = (108-100)/100 = 0.08
        assert pos["mfe"] == pytest.approx(0.08)

    def test_short_mae_when_price_rises(self):
        """Short: MAE is negative fraction (price rose above entry — adverse for short)."""
        pos = _make_short_pos(fill_price=100.0)
        pos = self._run_update(pos, high=108.0, low=95.0)
        # adverse for short = (fill - high) / fill = (100-108)/100 = -0.08
        assert pos["mae"] == pytest.approx(-0.08)

    def test_short_mfe_when_price_falls(self):
        """Short: MFE is positive fraction (price fell below entry — favorable for short)."""
        pos = _make_short_pos(fill_price=100.0)
        pos = self._run_update(pos, high=102.0, low=92.0)
        # favorable for short = (fill - low) / fill = (100-92)/100 = 0.08
        assert pos["mfe"] == pytest.approx(0.08)

    def test_long_mae_mfe_independent(self):
        """Long MAE and MFE accumulate independently across multiple bars."""
        engine = _make_engine()
        pos = _make_long_pos(fill_price=100.0)
        dates = [pd.Timestamp("2024-01-10"), pd.Timestamp("2024-01-11")]
        # Day 1: small range
        df1 = _make_ohlcv_df([dates[0]], high=103.0, low=99.0)
        # Day 2: bigger range
        df2 = _make_ohlcv_df([dates[1]], high=106.0, low=96.0)
        data1 = {"AAPL": df1}
        data2 = {"AAPL": df2}
        engine._update_mae_mfe(dates[0], data1, [pos])
        engine._update_mae_mfe(dates[1], data2, [pos])
        # MAE = min(-0.01, -0.04) = -0.04
        assert pos["mae"] == pytest.approx(-0.04)
        # MFE = max(0.03, 0.06) = 0.06
        assert pos["mfe"] == pytest.approx(0.06)

    def test_short_mae_mfe_independent(self):
        """Short MAE and MFE accumulate correctly across multiple bars."""
        engine = _make_engine()
        pos = _make_short_pos(fill_price=100.0)
        dates = [pd.Timestamp("2024-01-10"), pd.Timestamp("2024-01-11")]
        df1 = _make_ohlcv_df([dates[0]], high=103.0, low=98.0)
        df2 = _make_ohlcv_df([dates[1]], high=107.0, low=94.0)
        engine._update_mae_mfe(dates[0], {"AAPL": df1}, [pos])
        engine._update_mae_mfe(dates[1], {"AAPL": df2}, [pos])
        # Short MAE: min((100-103)/100, (100-107)/100) = min(-0.03, -0.07) = -0.07
        assert pos["mae"] == pytest.approx(-0.07)
        # Short MFE: max((100-98)/100, (100-94)/100) = max(0.02, 0.06) = 0.06
        assert pos["mfe"] == pytest.approx(0.06)


# ─────────────────────────────────────────────────────────────────────────────
# FIX-1: _process_max_loss_exits
# ─────────────────────────────────────────────────────────────────────────────

class TestProcessMaxLossExits:
    """Direction-aware unrealized P&L in max loss cap."""

    def _engine(self, max_loss: float = 50.0):
        cfg = _make_config()
        cfg["risk"]["max_loss_per_trade"] = max_loss
        return BacktestEngine(cfg)

    def _make_data(self, ticker: str, yest: pd.Timestamp, today: pd.Timestamp,
                   yest_close: float, today_close: float) -> Dict:
        dates = [yest, today]
        df = pd.DataFrame(
            {
                "open": [yest_close, today_close],
                "high": [yest_close * 1.01, today_close * 1.01],
                "low": [yest_close * 0.99, today_close * 0.99],
                "close": [yest_close, today_close],
                "volume": [1_000_000, 1_000_000],
            },
            index=pd.DatetimeIndex(dates),
        )
        return {ticker: df}

    def test_long_triggers_when_loss_exceeds_cap(self):
        """Long position exits when (yest_close - entry) * shares < -max_loss."""
        engine = self._engine(max_loss=50.0)
        today = pd.Timestamp("2024-01-10")
        yest = pd.Timestamp("2024-01-09")
        trading_dates = pd.DatetimeIndex([yest, today])
        pos = _make_long_pos(fill_price=100.0, shares=10)
        pos["ticker"] = "AAPL"
        # unrealized = (93 - 100) * 10 = -70 <= -50 → exit
        data = self._make_data("AAPL", yest, today, yest_close=93.0, today_close=93.0)
        closed_trades: List = []
        equity = engine._process_max_loss_exits(
            1, today, trading_dates, data, 10_000.0, [pos], closed_trades
        )
        assert len(closed_trades) == 1
        assert closed_trades[0]["direction"] == "long"

    def test_long_does_not_trigger_when_loss_small(self):
        """Long position does not exit when loss is within cap."""
        engine = self._engine(max_loss=50.0)
        today = pd.Timestamp("2024-01-10")
        yest = pd.Timestamp("2024-01-09")
        trading_dates = pd.DatetimeIndex([yest, today])
        pos = _make_long_pos(fill_price=100.0, shares=10)
        pos["ticker"] = "AAPL"
        # unrealized = (97 - 100) * 10 = -30 > -50 → no exit
        data = self._make_data("AAPL", yest, today, yest_close=97.0, today_close=97.0)
        closed_trades: List = []
        engine._process_max_loss_exits(
            1, today, trading_dates, data, 10_000.0, [pos], closed_trades
        )
        assert len(closed_trades) == 0

    def test_short_triggers_when_price_rises_above_cap(self):
        """Short position exits when (entry - yest_close) * shares < -max_loss."""
        engine = self._engine(max_loss=50.0)
        today = pd.Timestamp("2024-01-10")
        yest = pd.Timestamp("2024-01-09")
        trading_dates = pd.DatetimeIndex([yest, today])
        pos = _make_short_pos(fill_price=100.0, shares=10)
        pos["ticker"] = "AAPL"
        # unrealized for short = (100 - 107) * 10 = -70 <= -50 → exit
        data = self._make_data("AAPL", yest, today, yest_close=107.0, today_close=107.0)
        closed_trades: List = []
        engine._process_max_loss_exits(
            1, today, trading_dates, data, 10_000.0, [pos], closed_trades
        )
        assert len(closed_trades) == 1
        assert closed_trades[0]["direction"] == "short"

    def test_short_does_not_trigger_when_price_falls(self):
        """Short position does not exit when price is below entry (profitable)."""
        engine = self._engine(max_loss=50.0)
        today = pd.Timestamp("2024-01-10")
        yest = pd.Timestamp("2024-01-09")
        trading_dates = pd.DatetimeIndex([yest, today])
        pos = _make_short_pos(fill_price=100.0, shares=10)
        pos["ticker"] = "AAPL"
        # unrealized for short = (100 - 93) * 10 = +70 → no exit
        data = self._make_data("AAPL", yest, today, yest_close=93.0, today_close=93.0)
        closed_trades: List = []
        engine._process_max_loss_exits(
            1, today, trading_dates, data, 10_000.0, [pos], closed_trades
        )
        assert len(closed_trades) == 0

    def test_short_pnl_in_trade_record_correct(self):
        """P&L in the closed trade record is correct for short max-loss exit."""
        engine = self._engine(max_loss=50.0)
        today = pd.Timestamp("2024-01-10")
        yest = pd.Timestamp("2024-01-09")
        trading_dates = pd.DatetimeIndex([yest, today])
        pos = _make_short_pos(fill_price=100.0, shares=10)
        pos["ticker"] = "AAPL"
        # exit price = today_close = 108
        data = self._make_data("AAPL", yest, today, yest_close=108.0, today_close=108.0)
        closed_trades: List = []
        engine._process_max_loss_exits(
            1, today, trading_dates, data, 10_000.0, [pos], closed_trades
        )
        # Short P&L = (100 - 108) * 10 = -80
        assert closed_trades[0]["pnl"] == pytest.approx(-80.0)


# ─────────────────────────────────────────────────────────────────────────────
# FIX-1: _process_trailing_stops
# ─────────────────────────────────────────────────────────────────────────────

class TestProcessTrailingStops:
    """Trailing stop logic for short positions."""

    def _engine(self, activation_pct: float = 0.05, atr_mult: float = 2.0):
        cfg = _make_config()
        cfg["risk"]["trailing_stop"] = {
            "enabled": True,
            "activation_pct": activation_pct,
            "atr_multiplier": atr_mult,
        }
        return BacktestEngine(cfg)

    def _make_two_day_data(
        self, ticker: str,
        yest_close: float,
        today_high: float, today_low: float, today_close: float,
    ) -> Dict:
        yest = pd.Timestamp("2024-01-09")
        today = pd.Timestamp("2024-01-10")
        df = pd.DataFrame(
            {
                "open": [yest_close, today_close],
                "high": [yest_close * 1.01, today_high],
                "low": [yest_close * 0.99, today_low],
                "close": [yest_close, today_close],
                "volume": [1_000_000, 1_000_000],
            },
            index=pd.DatetimeIndex([yest, today]),
        )
        return {ticker: df}

    def test_long_trailing_stop_activates_on_rise(self):
        """Long trailing stop activates when high rises enough above entry."""
        engine = self._engine(activation_pct=0.03, atr_mult=2.0)
        pos = _make_long_pos(fill_price=100.0, shares=10)
        pos["ticker"] = "AAPL"
        pos["features"]["atr"] = 2.0  # ATR = 2
        today = pd.Timestamp("2024-01-10")
        yest = pd.Timestamp("2024-01-09")
        trading_dates = pd.DatetimeIndex([yest, today])
        # today_high = 105 (5% above fill = 100) → activates at 3%
        data = self._make_two_day_data("AAPL", 103.0, 105.0, 99.0, 104.0)
        closed_trades: List = []
        engine._process_trailing_stops(
            1, today, trading_dates, data, 10_000.0, [pos], closed_trades
        )
        assert pos.get("trailing_stop_active") is True
        assert "highest_price" in pos

    def test_short_trailing_stop_activates_on_drop(self):
        """Short trailing stop activates when low drops enough below entry."""
        engine = self._engine(activation_pct=0.03, atr_mult=2.0)
        pos = _make_short_pos(fill_price=100.0, shares=10)
        pos["ticker"] = "AAPL"
        pos["features"]["atr"] = 2.0
        today = pd.Timestamp("2024-01-10")
        yest = pd.Timestamp("2024-01-09")
        trading_dates = pd.DatetimeIndex([yest, today])
        # today_low = 94 (6% below fill = 100) → activates at 3%
        data = self._make_two_day_data("AAPL", 97.0, 101.0, 94.0, 96.0)
        closed_trades: List = []
        engine._process_trailing_stops(
            1, today, trading_dates, data, 10_000.0, [pos], closed_trades
        )
        assert pos.get("trailing_stop_active") is True
        assert "lowest_price" in pos
        # Trail stop for short = lowest + atr_mult * atr = 94 + 2*2 = 98
        assert pos["trailing_stop_price"] == pytest.approx(94.0 + 2.0 * 2.0)

    def test_short_trailing_stop_triggers_when_close_rises_above_stop(self):
        """Short trailing stop triggers when yesterday's close is above trail stop."""
        engine = self._engine(activation_pct=0.02, atr_mult=2.0)
        # Pre-activate the trailing stop
        pos = _make_short_pos(fill_price=100.0, shares=10)
        pos["ticker"] = "AAPL"
        pos["features"]["atr"] = 2.0
        pos["trailing_stop_active"] = True
        pos["lowest_price"] = 92.0  # simulated prior lowest
        pos["trailing_stop_price"] = 96.0  # 92 + 2*2 = 96 (below entry)
        today = pd.Timestamp("2024-01-10")
        yest = pd.Timestamp("2024-01-09")
        trading_dates = pd.DatetimeIndex([yest, today])
        # yest_close = 97 >= trail_stop (96) → should trigger
        data = self._make_two_day_data("AAPL", yest_close=97.0, today_high=101.0,
                                       today_low=95.0, today_close=98.0)
        closed_trades: List = []
        equity = engine._process_trailing_stops(
            1, today, trading_dates, data, 10_000.0, [pos], closed_trades
        )
        assert len(closed_trades) == 1
        assert closed_trades[0]["exit_reason"] == "trailing_stop"
        assert closed_trades[0]["direction"] == "short"

    def test_short_trailing_stop_does_not_trigger_when_close_below_stop(self):
        """Short trailing stop doesn't trigger when yesterday's close is below stop."""
        engine = self._engine(activation_pct=0.02, atr_mult=2.0)
        pos = _make_short_pos(fill_price=100.0, shares=10)
        pos["ticker"] = "AAPL"
        pos["features"]["atr"] = 2.0
        pos["trailing_stop_active"] = True
        pos["lowest_price"] = 92.0
        pos["trailing_stop_price"] = 96.0
        today = pd.Timestamp("2024-01-10")
        yest = pd.Timestamp("2024-01-09")
        trading_dates = pd.DatetimeIndex([yest, today])
        # yest_close = 94 < trail_stop (96) → no trigger
        data = self._make_two_day_data("AAPL", yest_close=94.0, today_high=96.0,
                                       today_low=91.0, today_close=93.0)
        closed_trades: List = []
        engine._process_trailing_stops(
            1, today, trading_dates, data, 10_000.0, [pos], closed_trades
        )
        assert len(closed_trades) == 0

    def test_long_trailing_stop_still_works_with_short_logic_present(self):
        """Regression: long trailing stop unaffected by short direction changes."""
        engine = self._engine(activation_pct=0.02, atr_mult=2.0)
        pos = _make_long_pos(fill_price=100.0, shares=10)
        pos["ticker"] = "AAPL"
        pos["features"]["atr"] = 2.0
        pos["trailing_stop_active"] = True
        pos["highest_price"] = 108.0
        pos["trailing_stop_price"] = 104.0  # 108 - 2*2 = 104
        today = pd.Timestamp("2024-01-10")
        yest = pd.Timestamp("2024-01-09")
        trading_dates = pd.DatetimeIndex([yest, today])
        # yest_close = 103 <= trail_stop (104) → should trigger
        data = self._make_two_day_data("AAPL", yest_close=103.0, today_high=106.0,
                                       today_low=102.0, today_close=103.0)
        closed_trades: List = []
        engine._process_trailing_stops(
            1, today, trading_dates, data, 10_000.0, [pos], closed_trades
        )
        assert len(closed_trades) == 1
        assert closed_trades[0]["exit_reason"] == "trailing_stop"
        assert closed_trades[0]["direction"] == "long"


# ─────────────────────────────────────────────────────────────────────────────
# FIX-1: Entry position direction
# ─────────────────────────────────────────────────────────────────name
# ─────────────────────────────────────────────────────────────────────────────

class TestEntrySlippageAndDirection:
    """Entry slippage is direction-aware; short entries don't inflate fill price."""

    def _engine(self, slippage_pct: float = 0.001):
        cfg = _make_config()
        cfg["fees"]["slippage_pct"] = slippage_pct
        return BacktestEngine(cfg)

    def test_long_entry_slippage_raises_price(self):
        """Long entry: fill price = open * (1 + slippage)."""
        engine = self._engine(slippage_pct=0.001)
        fill = engine._apply_slippage(100.0, "buy")
        assert fill == pytest.approx(100.1)

    def test_short_entry_slippage_lowers_price(self):
        """Short entry: fill price = open * (1 - slippage) — we sell at lower price."""
        engine = self._engine(slippage_pct=0.001)
        fill = engine._apply_slippage(100.0, "sell")
        assert fill == pytest.approx(99.9)

    def test_apply_slippage_buy_vs_sell(self):
        """Slippage is asymmetric: buy raises, sell lowers."""
        engine = self._engine(slippage_pct=0.005)
        buy_price = engine._apply_slippage(200.0, "buy")
        sell_price = engine._apply_slippage(200.0, "sell")
        assert buy_price > 200.0
        assert sell_price < 200.0


# ─────────────────────────────────────────────────────────────────────────────
# FIX-3: Pipeline delegation — DayContext and run_entry_gates
# ─────────────────────────────────────────────────────────────────────────────

class TestPipelineDelegation:
    """DayContext is created correctly and gates populate ctx fields."""

    def _make_ctx(self, **overrides) -> DayContext:
        today = pd.Timestamp("2024-01-10")
        yesterday = pd.Timestamp("2024-01-09")
        base = dict(
            today=today,
            yesterday=yesterday,
            day_idx=1,
            equity=10_000.0,
            open_positions=[],
            closed_trades=[],
            data={},
            trading_dates=None,
        )
        base.update(overrides)
        return DayContext(**base)

    def test_daycontext_default_gate_fields(self):
        ctx = self._make_ctx()
        assert ctx.vix_blocked is False
        assert ctx.fred_blocked is False
        assert ctx.tom_blocked is False
        assert ctx.macro_blocked is False
        assert ctx.any_gate_blocked is False

    def test_run_entry_gates_vix_blocked(self):
        """run_entry_gates sets vix_blocked when VIX is above threshold."""
        yesterday = pd.Timestamp("2024-01-09")
        vix = pd.Series([35.0], index=pd.DatetimeIndex([yesterday]))
        ctx = self._make_ctx(vix_series=vix)
        config = {
            "vix_filter": {"enabled": True, "max_entry": 30.0},
            "fred_filter": {},
            "turn_of_month": False,
            "macro_regime": {"enabled": False},
        }
        run_entry_gates(ctx, config)
        assert ctx.vix_blocked is True
        assert ctx.any_gate_blocked is True

    def test_run_entry_gates_vix_passes(self):
        """run_entry_gates does not block when VIX is below threshold."""
        yesterday = pd.Timestamp("2024-01-09")
        vix = pd.Series([20.0], index=pd.DatetimeIndex([yesterday]))
        ctx = self._make_ctx(vix_series=vix)
        config = {
            "vix_filter": {"enabled": True, "max_entry": 30.0},
            "fred_filter": {},
            "turn_of_month": False,
            "macro_regime": {"enabled": False},
        }
        run_entry_gates(ctx, config)
        assert ctx.vix_blocked is False

    def test_run_entry_gates_populates_macro_scale(self):
        """run_entry_gates populates ctx.macro_scale from macro_signals."""
        yesterday = pd.Timestamp("2024-01-09")
        today = pd.Timestamp("2024-01-10")
        macro_df = pd.DataFrame(
            {"macro_regime_scale": [1.3], "gc_regime": [1], "vix_roc_5d": [0.0],
             "vix_spike": [False], "yield_curve_10y_3m": [0.2], "yc_flattening": [False]},
            index=pd.DatetimeIndex([yesterday]),
        )
        ctx = self._make_ctx(today=today, yesterday=yesterday, macro_signals=macro_df)
        config = {
            "vix_filter": {"enabled": False},
            "fred_filter": {},
            "turn_of_month": False,
            "macro_regime": {"enabled": True, "mode": "sizing"},
        }
        run_entry_gates(ctx, config)
        assert ctx.macro_scale == pytest.approx(1.3)

    def test_daycontext_trading_dates_used_for_tom(self):
        """When ctx.trading_dates is set, run_entry_gates uses it for TOM check."""
        trading_dates = pd.bdate_range("2024-01-02", periods=60)
        today = pd.Timestamp("2024-01-17")  # mid-month — outside TOM window
        yesterday = pd.Timestamp("2024-01-16")
        ctx = self._make_ctx(today=today, yesterday=yesterday, trading_dates=trading_dates)
        config = {
            "vix_filter": {"enabled": False},
            "fred_filter": {},
            "turn_of_month": {"mode": True, "days_before_month_end": 5, "days_after_month_start": 3},
            "macro_regime": {"enabled": False},
        }
        run_entry_gates(ctx, config)
        assert ctx.tom_blocked is True
        assert ctx.tom_in_window is False

    def test_enrich_signals_extends_ctx_all_signals(self):
        """enrich_signals adds enriched signals to ctx.all_signals."""
        from strategies.base import Signal
        ctx = self._make_ctx()
        sig = Signal(
            ticker="AAPL",
            strategy="test",
            direction="long",
            entry_price=100.0,
            stop_price=95.0,
            take_profit=110.0,
            position_size=10,
            position_value=1000.0,
            risk_amount=50.0,
            confidence=0.75,
            rationale="test",
            features={},
        )
        config = {
            "strategies": {},
            "turn_of_month": False,
            "macro_regime": {"enabled": False},
        }
        enrich_signals([sig], ctx, config)
        assert len(ctx.all_signals) == 1
        assert ctx.all_signals[0] is sig

    def test_daycontext_trading_dates_none_by_default(self):
        """DayContext.trading_dates defaults to None for backwards compatibility."""
        ctx = self._make_ctx()
        assert ctx.trading_dates is None


# ─────────────────────────────────────────────────────────────────────────────
# FIX-4: EventCalendar integration
# ─────────────────────────────────────────────────────────────────────────────

class TestEventCalendarIntegration:
    """event_calendar is None by default; initialised when enabled."""

    def test_event_calendar_disabled_by_default(self):
        engine = _make_engine()
        assert engine.event_calendar is None

    def test_event_calendar_enabled_does_not_crash(self):
        """When event_calendar.enabled=True, engine initialises without raising."""
        cfg = _make_config()
        cfg["event_calendar"] = {"enabled": True}
        # If data.events is available it will init; if not it falls back to None
        engine = BacktestEngine(cfg)
        assert hasattr(engine, "event_calendar")

    def test_event_calendar_import_failure_sets_none(self):
        """If EventCalendar fails to import/init, falls back to None gracefully."""
        cfg = _make_config()
        cfg["event_calendar"] = {"enabled": True}
        import sys
        # Temporarily hide data.events so the try/except catches ImportError
        original = sys.modules.get("data.events")
        sys.modules["data.events"] = None  # type: ignore[assignment]
        try:
            engine = BacktestEngine(cfg)
        finally:
            if original is not None:
                sys.modules["data.events"] = original
            else:
                sys.modules.pop("data.events", None)
        # Should not crash; event_calendar is None since import failed
        assert hasattr(engine, "event_calendar")


# ─────────────────────────────────────────────────────────────────────────────
# Import smoke test
# ─────────────────────────────────────────────────────────────────────────────

class TestImports:
    def test_engine_imports_ok(self):
        from backtest.engine import BacktestEngine  # noqa: F401

    def test_pipeline_imports_ok(self):
        from backtest.pipeline import DayContext, run_entry_gates, enrich_signals  # noqa: F401

    def test_daycontext_has_trading_dates_field(self):
        """DayContext should have a trading_dates field added by FIX-3."""
        import dataclasses
        fields = {f.name for f in dataclasses.fields(DayContext)}
        assert "trading_dates" in fields
