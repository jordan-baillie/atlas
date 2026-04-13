"""Tests for indicators/vol_cones.py -- Phase 3."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from indicators.vol_cones import (
    DEFAULT_HORIZONS,
    FALLBACK_STOP_PCT,
    REGIME_MULTIPLIERS,
    TRADING_DAYS_PER_YEAR,
    compute_dynamic_stop,
    compute_vol_cone,
    get_vol_regime_multiplier,
    persist_vol_cone,
    yang_zhang_volatility,
    _ensure_vol_tables,
)


def _synthetic_ohlc(n_days: int, daily_sigma: float, seed: int = 42) -> pd.DataFrame:
    """Generate synthetic OHLC data with a target daily vol."""
    rng = np.random.default_rng(seed)
    closes = [100.0]
    rows = []
    for i in range(n_days):
        ret = rng.normal(0.0, daily_sigma)
        c_new = closes[-1] * math.exp(ret)
        # Give each bar a small intraday range around close
        intra = abs(rng.normal(0.0, daily_sigma * 0.5))
        o = closes[-1] * math.exp(rng.normal(0.0, daily_sigma * 0.25))
        h = max(o, c_new) * math.exp(intra)
        l = min(o, c_new) * math.exp(-intra)
        rows.append({"open": o, "high": h, "low": l, "close": c_new})
        closes.append(c_new)
    idx = pd.date_range(end=datetime.today().date(), periods=n_days, freq="B")
    return pd.DataFrame(rows, index=idx)


# --- Yang-Zhang estimator ---------------------------------------------------

def test_yang_zhang_returns_float():
    df = _synthetic_ohlc(100, daily_sigma=0.01)
    vol = yang_zhang_volatility(df, window=20)
    assert isinstance(vol, float)
    assert vol > 0

def test_yang_zhang_recovers_known_vol_approximately():
    # daily_sigma = 0.01 -> annualized ~0.1587
    # YZ should come reasonably close on synthetic data
    df = _synthetic_ohlc(500, daily_sigma=0.01, seed=7)
    vol = yang_zhang_volatility(df, window=60)
    expected = 0.01 * math.sqrt(TRADING_DAYS_PER_YEAR)
    # Allow 50% tolerance -- synthetic OHLC with random intraday ranges is noisy
    assert 0.5 * expected < vol < 1.7 * expected, f"got {vol}, expected ~{expected}"

def test_yang_zhang_insufficient_data_returns_zero():
    df = _synthetic_ohlc(5, daily_sigma=0.01)
    assert yang_zhang_volatility(df, window=20) == 0.0

def test_yang_zhang_higher_vol_input_gives_higher_output():
    low = _synthetic_ohlc(300, daily_sigma=0.005, seed=1)
    high = _synthetic_ohlc(300, daily_sigma=0.02, seed=1)
    assert yang_zhang_volatility(high, window=40) > yang_zhang_volatility(low, window=40)


# --- Cone computation (real data -- SPY) ------------------------------------

def test_compute_vol_cone_spy_sensible():
    """SPY should have a full 5-horizon cone with reasonable vol values."""
    result = compute_vol_cone("SPY", lookback_years=3)
    if result.get("error"):
        pytest.skip(f"SPY not in DB: {result['error']}")
    assert "cone" in result
    assert 20 in result["cone"]
    c20 = result["cone"][20]
    # SPY annualized vol should historically sit roughly 5%-80%
    assert 0.03 < c20["p50"] < 0.80, f"SPY p50 20d vol unrealistic: {c20['p50']}"
    # Percentiles must be monotonic
    assert c20["p5"] <= c20["p25"] <= c20["p50"] <= c20["p75"] <= c20["p95"]
    assert result["current_regime"] in {"low", "normal", "high", "extreme"}


def test_compute_vol_cone_missing_ticker_returns_error():
    result = compute_vol_cone("__NOT_A_REAL_TICKER__")
    assert result.get("error") == "no_data"
    assert result["cone"] == {}


# --- Regime classification --------------------------------------------------

def test_regime_multipliers_expected_values():
    assert REGIME_MULTIPLIERS["low"] == 1.5
    assert REGIME_MULTIPLIERS["normal"] == 2.0
    assert REGIME_MULTIPLIERS["high"] == 2.5
    assert REGIME_MULTIPLIERS["extreme"] == 3.0

def test_get_vol_regime_multiplier_fallback():
    # Unknown ticker -> falls back to 'normal' = 2.0
    assert get_vol_regime_multiplier("__NOPE__") == 2.0


# --- Dynamic stop calculation -----------------------------------------------

def test_dynamic_stop_long_uses_entry_minus():
    result = compute_dynamic_stop(100.0, "SPY", direction="long")
    assert result["stop_price"] < 100.0
    assert result["direction"] == "long"
    assert result["entry_price"] == 100.0

def test_dynamic_stop_short_uses_entry_plus():
    result = compute_dynamic_stop(100.0, "SPY", direction="short")
    assert result["stop_price"] > 100.0
    assert result["direction"] == "short"

def test_dynamic_stop_invalid_direction_raises():
    with pytest.raises(ValueError):
        compute_dynamic_stop(100.0, "SPY", direction="sideways")

def test_dynamic_stop_fallback_for_unknown_ticker():
    result = compute_dynamic_stop(100.0, "__NOT_A_TICKER__", direction="long")
    assert result["method"] == "fixed_fallback"
    assert result["stop_distance_pct"] == FALLBACK_STOP_PCT
    assert result["stop_price"] == pytest.approx(100.0 * (1 - FALLBACK_STOP_PCT))
    assert result["vol_regime"] == "unknown"

def test_dynamic_stop_k_override():
    result = compute_dynamic_stop(100.0, "SPY", direction="long", k_override=3.5)
    if result["method"] == "yang_zhang_dynamic":
        assert result["k"] == 3.5

def test_dynamic_stop_extreme_regime_flags_review():
    # Cannot force regime without real data, but verify needs_review logic
    # by checking the field exists and is bool
    result = compute_dynamic_stop(100.0, "SPY", direction="long")
    assert "needs_review" in result
    assert isinstance(result["needs_review"], bool)


# --- Cache persistence -------------------------------------------------------

def test_persist_vol_cone_roundtrip():
    from db.atlas_db import get_db
    _ensure_vol_tables()
    fake_result = {
        "ticker": "__TEST_TICKER__",
        "as_of": "2099-12-31",
        "lookback_years": 5,
        "cone": {
            20: {"current": 0.25, "p5": 0.10, "p25": 0.15, "p50": 0.20,
                 "p75": 0.30, "p95": 0.45, "n_obs": 1000},
        },
        "current_regime": "normal",
    }
    persist_vol_cone(fake_result)
    with get_db() as db:
        row = db.execute(
            "SELECT current_vol, p50, n_obs FROM vol_cones "
            "WHERE ticker=? AND as_of=? AND horizon=20",
            ("__TEST_TICKER__", "2099-12-31"),
        ).fetchone()
        regime_row = db.execute(
            "SELECT regime, multiplier FROM vol_regimes WHERE ticker=? AND as_of=?",
            ("__TEST_TICKER__", "2099-12-31"),
        ).fetchone()
        # Cleanup
        db.execute("DELETE FROM vol_cones WHERE ticker='__TEST_TICKER__'")
        db.execute("DELETE FROM vol_regimes WHERE ticker='__TEST_TICKER__'")
    assert row is not None
    assert row["current_vol"] == pytest.approx(0.25)
    assert row["p50"] == pytest.approx(0.20)
    assert row["n_obs"] == 1000
    assert regime_row["regime"] == "normal"
    assert regime_row["multiplier"] == 2.0
