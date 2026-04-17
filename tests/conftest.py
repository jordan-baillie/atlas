"""Shared test fixtures for Atlas test suite."""
import copy
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Ensure project root is on path
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from strategies.base import Signal  # noqa: E402


# ---------------------------------------------------------------------------
# Test-log isolation — prevent pytest output from polluting prod atlas.log
# ---------------------------------------------------------------------------
import logging as _logging
import os as _os

@pytest.fixture(scope="session", autouse=True)
def _isolate_test_logs():
    """Redirect root logger file output to tests/logs/pytest.log for the session.

    Background: utils.logging_config.setup_logging() attaches a FileHandler
    pointing at logs/atlas.log. Importing modules that call setup_logging()
    during pytest causes test-time errors (mock failures, intentional bad
    inputs) to leak into the production log, where atlas-error-watchdog
    picks them up as real alerts.

    This fixture: at session start, removes any FileHandler whose baseFilename
    points at the prod atlas.log; replaces it with a FileHandler at
    tests/logs/pytest.log. At teardown, restores the original handlers.
    """
    project_root = Path(__file__).resolve().parent.parent
    prod_log = (project_root / "logs" / "atlas.log").resolve()
    test_log_dir = project_root / "tests" / "logs"
    test_log_dir.mkdir(parents=True, exist_ok=True)
    test_log = test_log_dir / "pytest.log"

    root = _logging.getLogger()
    original_handlers = list(root.handlers)
    removed = []
    for h in list(root.handlers):
        if isinstance(h, _logging.FileHandler):
            try:
                if Path(h.baseFilename).resolve() == prod_log:
                    root.removeHandler(h)
                    removed.append(h)
                    try:
                        h.close()
                    except Exception:
                        pass
            except Exception:
                pass

    test_handler = _logging.FileHandler(test_log, mode="a")
    test_handler.setFormatter(_logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(test_handler)

    # Also intercept future setup_logging() calls during the session by
    # marking the module as already-set-up. Prevents import-time re-attach.
    try:
        from utils import logging_config as _lc
        _lc._setup_done = True
    except Exception:
        pass

    yield

    # Teardown — restore original handlers
    root.removeHandler(test_handler)
    try:
        test_handler.close()
    except Exception:
        pass
    # Re-add any handlers we removed (in case parallel test discovery needs them)
    for h in removed:
        try:
            root.addHandler(h)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Minimal config that satisfies all strategy constructors (no network calls)
# ---------------------------------------------------------------------------

MINIMAL_CONFIG: dict = {
    "version": "test-v1.0",
    "market": "sp500",
    "description": "Test configuration",
    "risk": {
        "starting_equity": 10_000.0,
        "max_risk_per_trade_pct": 0.01,
        "min_confidence": 0.65,
        "max_open_positions": 5,
        "max_sector_concentration": 2,
        "max_daily_drawdown_pct": 0.05,
        "require_stop_loss": True,
        "require_planned_exit": True,
    },
    "fees": {
        "commission_per_trade": 0,
        "commission_pct": 0.0,
        "slippage_pct": 0.0005,
        "min_position_value": 100.0,
        "flat_fee_threshold": 0,
    },
    "trading": {
        "mode": "paper",
        "broker": "alpaca",
        "live_enabled": False,
        "live_safety": {
            "max_order_value": 5000,
            "max_daily_orders": 10,
        },
    },
    "strategies": {
        "mean_reversion": {
            "enabled": True,
            "rsi_period": 14,
            "rsi_oversold": 35,
            "zscore_lookback": 30,
            "zscore_entry": -2.0,
            "atr_period": 20,
            "atr_stop_mult": 1.5,
            "profit_target_atr_mult": 2.5,
            "max_hold_days": 20,
            "sma200_filter": False,
            "ibs_max": 1.0,  # disabled
            "volume": {
                "lookback": 20,
                "min_ratio": 0.5,
                "surge_threshold": 1.5,
                "surge_boost": 0.0,
                "dry_penalty": 0.0,
            },
            "earnings_blackout": {"enabled": False},
        },
        "momentum_breakout": {
            "enabled": True,
            "lookback_days": 15,
            "atr_period": 20,
            "atr_stop_mult": 1.5,
            "max_hold_days": 15,
            "trend_ma_period": 20,
        },
        "trend_following": {
            "enabled": True,
            "fast_ma": 15,
            "slow_ma": 20,
            "pullback_pct": 0.04,
            "atr_period": 14,
            "atr_stop_mult": 2.0,
            "trailing_stop_atr_mult": 2.5,
            "max_hold_days": 15,
            "sma200_filter": False,
            "volume": {
                "lookback": 20,
                "min_ratio": 0.5,
                "boost_threshold": 1.5,
                "boost_amount": 0.1,
                "penalty_amount": 0.05,
            },
        },
        "opening_gap": {
            "enabled": True,
            "gap_threshold": -0.008,
            "ibs_confirm": 0.7,
            "rsi14_max": 35,
            "vol_surge_threshold": 1.5,
            "atr_period": 25,
            "atr_stop_mult": 1.0,
            "sma_exit_period": 7,
            "ibs_exit_threshold": 0.8,
            "max_hold_days": 10,
            "sma200_filter": False,
            "earnings_blackout": {"enabled": False},
        },
        "sector_rotation": {
            "enabled": True,
            "sector_momentum_period": 60,
            "top_sectors": 3,
            "bottom_sectors": 2,
            "rebalance_days": 20,
            "atr_period": 14,
            "atr_stop_mult": 3.0,
        },
        "short_term_mr": {
            "enabled": True,
            "rsi_period": 2,
            "rsi_oversold": 15,
            "ibs_oversold": 0.2,
            "sma_period": 5,
            "atr_period": 14,
            "atr_stop_mult": 1.5,
            "profit_target_atr_mult": 1.0,
            "max_hold_days": 5,
            "rsi_overbought_exit": 70,
            "volume": {"lookback": 20, "min_ratio": 0.5},
            "earnings_blackout": {"enabled": False},
        },
        "connors_rsi2": {
            "enabled": True,
            "rsi_period": 4,
            "rsi_entry": 40,
            "sma_trend_period": 150,
            "sma200_filter": False,
            "min_consecutive_down": 1,
            "ibs_max": 0.5,
            "ibs_filter_enabled": False,
            "volume": {"lookback": 20, "min_ratio": 0.5},
            "sma_exit_period": 5,
            "rsi_exit": 65,
            "exit_mode": "sma",
            "max_hold_days": 10,
            "atr_period": 14,
            "atr_stop_mult": 1.2,
        },
    },
    "backtest": {
        "train_window_days": 252,
        "test_window_days": 63,
        "step_days": 21,
        "min_history_days": 60,
    },
    "data": {
        "source": "yfinance",
        "history_years": 7,
        "cache_dir": "data/cache",
        "raw_dir": "data/raw",
        "processed_dir": "data/processed",
    },
    "allocation": {
        "enabled": False,  # disabled in tests for simplicity
        "mode": "soft_pool",
        "overflow_enabled": True,
        "pools": {
            "mean_reversion": {"max_positions": 2, "weight": 0.2},
            "momentum_breakout": {"max_positions": 1, "weight": 0.1},
            "trend_following": {"max_positions": 2, "weight": 0.2},
            "opening_gap": {"max_positions": 2, "weight": 0.2},
            "sector_rotation": {"max_positions": 2, "weight": 0.2},
            "short_term_mr": {"max_positions": 1, "weight": 0.05},
            "connors_rsi2": {"max_positions": 1, "weight": 0.05},
            "_other": {"max_positions": 1},
        },
    },
    "universe": {
        "method": "top_liquid",
        "top_n": 100,
        "min_median_daily_value": 5_000_000,
        "min_price": 5.0,
        "min_market_cap": 2_000_000_000,
        "exclusions": [],
        "benchmark_ticker": "SPY",
    },
}


# ---------------------------------------------------------------------------
# Helper: build a synthetic OHLCV DataFrame
# ---------------------------------------------------------------------------

def make_ohlcv_df(
    ticker: str = "TEST",
    n_days: int = 252,
    base_price: float = 100.0,
    seed: int = 42,
    trend: float = 0.0005,
    daily_vol: float = 0.015,
) -> pd.DataFrame:
    """Create a synthetic OHLCV DataFrame with DatetimeIndex.

    Prices are lognormal with drift *trend* and daily vol *daily_vol*.
    OHLCV invariant: low <= min(open, close) and high >= max(open, close).
    """
    rng = np.random.default_rng(seed)
    dates = pd.date_range(end="2024-12-31", periods=n_days, freq="B")

    # Log-normal returns
    returns = rng.normal(trend, daily_vol, n_days)
    close = base_price * np.exp(np.cumsum(returns))

    # Intraday scatter
    open_ = close * np.exp(rng.normal(0, 0.004, n_days))
    raw_high = np.maximum(open_, close) * np.exp(rng.uniform(0, 0.008, n_days))
    raw_low = np.minimum(open_, close) * np.exp(-rng.uniform(0, 0.008, n_days))

    # Enforce invariants
    high = np.maximum(raw_high, np.maximum(open_, close))
    low = np.minimum(raw_low, np.minimum(open_, close))

    volume = rng.integers(1_000_000, 5_000_000, n_days).astype(float)

    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "ticker": ticker},
        index=dates,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_config() -> dict:
    """Return a deep copy of MINIMAL_CONFIG (mutate freely in tests)."""
    return copy.deepcopy(MINIMAL_CONFIG)


@pytest.fixture
def mock_ohlcv_data():
    """Factory fixture: call with (n_tickers, n_days) to get OHLCV dict."""
    def _factory(n_tickers: int = 5, n_days: int = 252) -> dict[str, pd.DataFrame]:
        tickers = [f"TICK{i}" for i in range(n_tickers)]
        return {
            t: make_ohlcv_df(t, n_days=n_days, base_price=50 + 30 * i, seed=i * 7)
            for i, t in enumerate(tickers)
        }
    return _factory


@pytest.fixture
def mock_positions():
    """Factory fixture: call with (n) to get list of position dicts."""
    def _factory(n: int = 3) -> list[dict]:
        pool = ["AAPL", "MSFT", "GOOG", "META", "AMZN"]
        positions = []
        for i in range(n):
            ticker = pool[i % len(pool)]
            entry_price = 100.0 + i * 10
            positions.append(
                {
                    "ticker": ticker,
                    "strategy": "mean_reversion",
                    "direction": "long",
                    "entry_date": (datetime.now() - timedelta(days=i + 1)).strftime("%Y-%m-%d"),
                    "fill_price": entry_price,
                    "entry_price": entry_price,
                    "shares": 10,
                    "position_value": entry_price * 10,
                    "stop_price": entry_price * 0.95,
                    "confidence": 0.75,
                    "features": {"rsi": 28.0, "zscore": -2.3},
                    "sector": "Technology",
                }
            )
        return positions
    return _factory


@pytest.fixture
def mock_signal() -> Signal:
    """Return a valid Signal object."""
    return Signal(
        ticker="AAPL",
        strategy="mean_reversion",
        direction="long",
        entry_price=150.0,
        stop_price=145.0,
        take_profit=165.0,
        position_size=10,
        position_value=1500.0,
        risk_amount=50.0,
        confidence=0.75,
        rationale="RSI oversold test signal",
        features={"rsi": 28.0, "zscore": -2.5},
    )
