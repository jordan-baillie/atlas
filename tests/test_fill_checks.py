"""Tests for volume-based fill rejection (Task 1).

Verifies that BacktestEngine skips entry signals when the requested
position size exceeds ``volume_participation_limit * daily_volume``.

Design:
  - Synthetic OHLCV data with known volume (1 000 shares/bar)
  - Strategy mock that emits exactly one signal for 50 shares
  - Test the three relevant limit cases:
      limit=0.01 (1 %)  → 50 shares > 10 shares (1 % × 1 000) → REJECTED
      limit=0.10 (10 %) → 50 shares ≤ 100 shares (10 % × 1 000) → ACCEPTED
      limit=0           → disabled → ACCEPTED

All tests use only synthetic data, no network calls.
"""
import copy
import sys
from pathlib import Path
from typing import Dict, List
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from backtest.engine import BacktestEngine
from strategies.base import BaseStrategy, Signal

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# With equity=100_000, risk_pct=0.02, price=100, stop=95:
#   risk_per_share=5, risk_budget=2000, shares=400
# Volume thresholds:
#   limit=0.01 → max=10   shares  → 400 > 10   → REJECTED
#   limit=0.10 → max=4000 shares  → need vol>=4000 to allow 400 shares
TIGHT_LIMIT = 0.01   # 1 %  → threshold = vol * 0.01
LOOSE_LIMIT = 0.10   # 10 % → threshold = vol * 0.10

TIGHT_VOLUME = 1_000    # 1 000 × 0.01 = 10   → 400 shares  > 10  → REJECTED
LOOSE_VOLUME = 5_000    # 5 000 × 0.10 = 500  → 400 shares <= 500 → ACCEPTED

_N_DAYS = 120               # enough for min_history


def _make_ohlcv(n_days: int = _N_DAYS, volume: float = TIGHT_VOLUME) -> pd.DataFrame:
    """Create a simple flat-price OHLCV DataFrame."""
    dates = pd.date_range("2023-01-02", periods=n_days, freq="B")
    price = 100.0
    return pd.DataFrame(
        {
            "open": price,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "volume": volume,
            "ticker": "TEST",
        },
        index=dates,
    )


def _make_data(volume: float = TIGHT_VOLUME) -> Dict[str, pd.DataFrame]:
    """Return single-ticker data dict with specified bar volume."""
    return {"TEST": _make_ohlcv(volume=volume)}


def _make_config(volume_participation_limit: float) -> dict:
    """Minimal engine config with the specified participation limit."""
    return {
        "market": "sp500",
        "risk": {
            "starting_equity": 100_000.0,
            "leverage": 1.0,
            "max_risk_per_trade_pct": 0.02,   # generous so size-based rejection doesn't fire
            "max_open_positions": 5,
            "max_sector_concentration": 5,
            "max_daily_drawdown_pct": 0.10,
            "require_stop_loss": True,
            "require_planned_exit": True,
            "min_confidence": 0.0,
        },
        "fees": {
            "commission_per_trade": 0,
            "commission_pct": 0.0,
            "slippage_pct": 0.0,
            "slippage_model": "fixed",          # keep slippage simple for this test
            "min_position_value": 0.0,
            "flat_fee_threshold": 0,
        },
        "trading": {
            "mode": "paper",
            "broker": "alpaca",
            "live_enabled": False,
            "live_safety": {"max_order_value": 0, "max_daily_orders": 100},
        },
        "backtest": {
            "train_window_days": 60,
            "test_window_days": 30,
            "step_days": 10,
            "min_history_days": 60,
            "volume_participation_limit": volume_participation_limit,
        },
        "data": {
            "source": "yfinance",
            "history_years": 1,
            "cache_dir": "data/cache",
            "raw_dir": "data/raw",
            "processed_dir": "data/processed",
        },
        "allocation": {
            "enabled": False,
            "mode": "soft_pool",
            "overflow_enabled": True,
            "pools": {},
        },
        "universe": {
            "method": "top_liquid",
            "top_n": 10,
            "min_median_daily_value": 0,
            "min_price": 0.0,
            "min_market_cap": 0,
            "exclusions": [],
            "benchmark_ticker": "SPY",
        },
    }


class FixedSharesStrategy(BaseStrategy):
    """Always emits one long signal requesting SIGNAL_SHARES_TARGET shares."""

    @property
    def name(self) -> str:
        return "fixed_shares"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        open_positions: list,
    ) -> List[Signal]:
        signals = []
        for ticker, df in data.items():
            if not df.empty:
                last_close = float(df["close"].iloc[-1])
                # Stop is 5 % below entry so risk_per_share > 0
                stop = last_close * 0.95
                signals.append(
                    Signal(
                        ticker=ticker,
                        strategy=self.name,
                        direction="long",
                        entry_price=last_close,
                        stop_price=stop,
                        take_profit=last_close * 1.10,
                        position_size=100,
                        position_value=last_close * 100,
                        risk_amount=(last_close - stop) * 100,
                        confidence=0.80,
                        rationale="test",
                        features={"sector": "Technology"},
                    )
                )
        return signals

    def check_exits(self, data, open_positions):
        return []

    def precompute(self, data):
        pass


def _run_and_count_trades(
    volume_participation_limit: float,
    bar_volume: float = TIGHT_VOLUME,
) -> int:
    """Run a short backtest and return how many trades were executed."""
    config = _make_config(volume_participation_limit)
    data = _make_data(volume=bar_volume)

    # Patch download_ticker so engine can get benchmark data without network
    dummy_bench = _make_ohlcv(n_days=_N_DAYS)
    with patch("backtest.engine.download_ticker", return_value=dummy_bench):
        engine = BacktestEngine(config, market_id="sp500")
        strategy = FixedSharesStrategy(config)
        result = engine.run_walkforward(data, [strategy])

    return len(result.trades)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_volume_participation_rejects_entry_with_tight_limit():
    """limit=0.01, volume=1000 → threshold=10 shares; engine sizes to 400 → REJECTED.

    With equity=100_000, risk_pct=2%, price=100, stop=95:
      risk_budget=2000, risk_per_share=5, shares=400
    Volume threshold = 1000 * 0.01 = 10  →  400 > 10  →  all entries rejected.
    """
    n_trades = _run_and_count_trades(volume_participation_limit=TIGHT_LIMIT,
                                     bar_volume=TIGHT_VOLUME)
    assert n_trades == 0, (
        f"Expected 0 trades (volume limit too tight: 400 shares > {TIGHT_VOLUME*TIGHT_LIMIT:.0f}), "
        f"got {n_trades}"
    )


def test_volume_participation_accepts_entry_with_loose_limit():
    """limit=0.10, volume=5000 → threshold=500 shares; engine sizes to 400 → ACCEPTED.

    Volume threshold = 5000 * 0.10 = 500  →  400 ≤ 500  →  entries allowed.
    """
    n_trades = _run_and_count_trades(volume_participation_limit=LOOSE_LIMIT,
                                     bar_volume=LOOSE_VOLUME)
    assert n_trades > 0, (
        f"Expected trades (limit=10% × 5000 vol = 500 threshold >= 400 shares), got 0"
    )


def test_volume_participation_disabled_when_zero():
    """limit=0 → feature disabled → same trades as loose-limit scenario."""
    n_trades_disabled = _run_and_count_trades(volume_participation_limit=0.0,
                                              bar_volume=LOOSE_VOLUME)
    n_trades_loose = _run_and_count_trades(volume_participation_limit=LOOSE_LIMIT,
                                           bar_volume=LOOSE_VOLUME)
    # Both should produce trades; disabled should not reject more than loose
    assert n_trades_disabled > 0, "Disabled limit should allow trades"
    assert n_trades_disabled >= n_trades_loose, (
        "Disabled limit should allow at least as many trades as a loose limit"
    )


def test_volume_participation_check_uses_bar_volume():
    """Verify the check compares shares to the bar's actual volume column."""
    config = _make_config(volume_participation_limit=0.05)  # 5 % → 50 shares threshold

    # With volume=1000 and limit=0.05 → threshold=50 → signal wants 50 → borderline
    # shares (int of risk_budget / risk_per_share) may land at 50 → should still pass
    # Use very high volume so participation is tiny → entries allowed
    high_vol_data = {"TEST": _make_ohlcv(volume=100_000)}
    dummy_bench = _make_ohlcv(n_days=_N_DAYS, volume=100_000)

    with patch("backtest.engine.download_ticker", return_value=dummy_bench):
        engine = BacktestEngine(config, market_id="sp500")
        strategy = FixedSharesStrategy(config)
        result = engine.run_walkforward(high_vol_data, [strategy])

    # With 100 000 vol and 5 % limit → threshold = 5 000 shares >> 50 → allowed
    assert len(result.trades) > 0, (
        "High volume should allow entries under participation limit"
    )


def test_volume_participation_attribute_loaded_from_config():
    """BacktestEngine reads volume_participation_limit from backtest config."""
    config = _make_config(volume_participation_limit=0.07)
    with patch("backtest.engine.download_ticker", return_value=_make_ohlcv()):
        engine = BacktestEngine(config, market_id="sp500")
    assert engine.volume_participation_limit == pytest.approx(0.07)


def test_volume_participation_default_is_zero():
    """Default (no key in config) → volume_participation_limit = 0 (disabled)."""
    config = _make_config(volume_participation_limit=0.0)
    # Remove the key entirely to test default
    del config["backtest"]["volume_participation_limit"]
    with patch("backtest.engine.download_ticker", return_value=_make_ohlcv()):
        engine = BacktestEngine(config, market_id="sp500")
    assert engine.volume_participation_limit == 0.0
