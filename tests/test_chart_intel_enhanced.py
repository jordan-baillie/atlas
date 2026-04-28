"""
Tests for enhanced chart_intel indicators (ATLAS_ENHANCED_CHART_INTEL=1).

All tests use synthetic DataFrames — NO real parquet files from data/cache/.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_df(
    closes: np.ndarray,
    volumes: np.ndarray,
    high_mult: float = 1.005,
    low_mult: float = 0.995,
) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from close + volume arrays."""
    n = len(closes)
    return pd.DataFrame(
        {
            "close": closes,
            "high": closes * high_mult,
            "low": closes * low_mult,
            "open": closes * 0.999,
            "volume": volumes,
        }
    )


@pytest.fixture
def healthy_uptrend_df() -> pd.DataFrame:
    """Rising price + rising volume — accumulation scenario."""
    n = 100
    rng = np.random.RandomState(42)
    closes = np.linspace(100, 130, n) + rng.normal(0, 0.5, n)
    volumes = np.linspace(1e6, 1.5e6, n) + rng.normal(0, 5e4, n)
    return _make_df(closes, volumes)


@pytest.fixture
def distribution_df() -> pd.DataFrame:
    """Price at 60d high + low volume — distribution-top scenario."""
    n = 100
    # Price rises to 130 then stalls at the high for the last 5 days
    closes = np.concatenate([np.linspace(100, 130, 95), np.full(5, 129.5)])
    # Volume: normal for most of the run, then collapses to 20% of avg
    avg_vol = 1_000_000.0
    volumes = np.concatenate([np.full(95, avg_vol), np.full(5, avg_vol * 0.2)])
    return _make_df(closes, volumes, high_mult=1.002, low_mult=0.998)


@pytest.fixture
def bearish_df() -> pd.DataFrame:
    """Falling price + rising volume — sell-off scenario."""
    n = 100
    rng = np.random.RandomState(7)
    closes = np.linspace(130, 100, n) + rng.normal(0, 0.3, n)
    volumes = np.linspace(1e6, 2e6, n) + rng.normal(0, 5e4, n)
    return _make_df(closes, volumes)


# ---------------------------------------------------------------------------
# Test 1: OBV slope detects divergence (price flat/down = OBV flat ≈ 0)
# ---------------------------------------------------------------------------


def test_obv_slope_detects_divergence(bearish_df):
    """Falling price → OBV slope should be ≤ 0."""
    from overlay.sources.chart_intel import _obv_slope

    slope = _obv_slope(bearish_df, window=20)
    assert isinstance(slope, float)
    assert slope <= 0.05, (
        f"Expected non-positive OBV slope for falling price, got {slope}"
    )


# ---------------------------------------------------------------------------
# Test 2: OBV slope positive when accumulation (rising price + rising volume)
# ---------------------------------------------------------------------------


def test_obv_slope_positive_when_accumulation(healthy_uptrend_df):
    """Rising price + rising volume → OBV slope > 0."""
    from overlay.sources.chart_intel import _obv_slope

    slope = _obv_slope(healthy_uptrend_df, window=20)
    assert isinstance(slope, float)
    assert slope > 0, f"Expected positive OBV slope for accumulation, got {slope}"


def test_obv_slope_returns_zero_for_insufficient_data():
    """Fewer than window+1 rows → returns 0.0 without error."""
    from overlay.sources.chart_intel import _obv_slope

    tiny = _make_df(np.array([100.0, 101.0, 99.0]), np.array([1e6, 1e6, 1e6]))
    assert _obv_slope(tiny, window=20) == 0.0


# ---------------------------------------------------------------------------
# Test 3: Resistance anchor finds 60d high with correct touch count
# ---------------------------------------------------------------------------


def test_resistance_anchor_finds_60d_high():
    """resistance_anchor returns the max high of last 60 rows + ≥1 touch."""
    from overlay.sources.chart_intel import _resistance_anchor

    n = 80
    # Spike to 150 at index 70; everything else ~100
    closes = np.full(n, 100.0)
    closes[70] = 150.0
    highs = closes * 1.005
    highs[70] = 150.0
    df = pd.DataFrame(
        {
            "close": closes,
            "high": highs,
            "low": closes * 0.995,
            "open": closes,
            "volume": np.full(n, 1e6),
        }
    )

    resistance, touches = _resistance_anchor(df, window=60, touch_tolerance=0.02)
    assert resistance == pytest.approx(150.0, abs=0.01)
    assert touches >= 1, f"Expected ≥1 touch, got {touches}"


def test_resistance_anchor_uses_full_df_when_shorter_than_window():
    """When df has fewer rows than window, uses entire df."""
    from overlay.sources.chart_intel import _resistance_anchor

    closes = np.array([100.0, 110.0, 90.0, 105.0, 108.0])
    df = _make_df(closes, np.full(len(closes), 1e6))
    resistance, touches = _resistance_anchor(df, window=60)
    # Max high = 110 * high_mult
    assert resistance == pytest.approx(110.0 * 1.005, abs=0.05)
    assert touches >= 1


# ---------------------------------------------------------------------------
# Test 4: Price-volume divergence detection
# ---------------------------------------------------------------------------


def test_price_volume_divergence_detected():
    """Price rising + volume declining → True."""
    from overlay.sources.chart_intel import _price_volume_divergence

    n = 25
    closes = np.linspace(100, 110, n)  # price up +10%
    # Volume declining steeply: 2e6 → 5e5 (>0.5% per day decline in relative terms)
    volumes = np.linspace(2_000_000, 500_000, n)
    df = _make_df(closes, volumes)
    assert _price_volume_divergence(df, window=20) is True


def test_price_volume_divergence_false_when_volume_up():
    """Price up + volume also up → False (healthy move)."""
    from overlay.sources.chart_intel import _price_volume_divergence

    n = 25
    closes = np.linspace(100, 110, n)
    volumes = np.linspace(1_000_000, 1_500_000, n)  # volume rising
    df = _make_df(closes, volumes)
    assert _price_volume_divergence(df, window=20) is False


def test_price_volume_divergence_false_when_price_down():
    """Price falling → always False (divergence requires rising price)."""
    from overlay.sources.chart_intel import _price_volume_divergence

    n = 25
    closes = np.linspace(110, 100, n)
    volumes = np.linspace(2_000_000, 500_000, n)
    df = _make_df(closes, volumes)
    assert _price_volume_divergence(df, window=20) is False


# ---------------------------------------------------------------------------
# Test 5: _at_resistance_low_volume predicate
# ---------------------------------------------------------------------------


def test_at_resistance_low_volume_true_for_distribution_top(distribution_df):
    """df where last close ≈ 60d high + last volume ≈ 20% of avg → True."""
    from overlay.sources.chart_intel import _at_resistance_low_volume

    result = _at_resistance_low_volume(distribution_df)
    assert result is True, (
        f"Expected True for distribution-top scenario, got {result}"
    )


def test_at_resistance_low_volume_false_with_healthy_volume(healthy_uptrend_df):
    """df at recent high but with normal/rising volume → False."""
    from overlay.sources.chart_intel import _at_resistance_low_volume

    # The healthy_uptrend_df ends near its high with rising volume → should be False
    result = _at_resistance_low_volume(healthy_uptrend_df)
    assert result is False, (
        f"Expected False for healthy-volume uptrend, got {result}"
    )


def test_at_resistance_low_volume_false_when_far_from_resistance():
    """Last close 15% below 60d high → not at resistance → False."""
    from overlay.sources.chart_intel import _at_resistance_low_volume

    n = 80
    closes = np.concatenate([np.full(10, 150.0), np.full(70, 127.0)])
    volumes = np.full(n, 500_000.0)  # low volume, but not at resistance
    df = _make_df(closes, volumes)
    result = _at_resistance_low_volume(df)
    assert result is False


def test_at_resistance_low_volume_false_for_short_df():
    """Fewer than 60 rows → False (insufficient history)."""
    from overlay.sources.chart_intel import _at_resistance_low_volume

    closes = np.linspace(100, 105, 30)
    df = _make_df(closes, np.full(30, 1e6))
    assert _at_resistance_low_volume(df) is False


# ---------------------------------------------------------------------------
# Test 6: Suppression guard changes _build_summary
# ---------------------------------------------------------------------------


def test_suppression_guard_changes_summary(monkeypatch):
    """With flag ON + SPY distribution_signal=True → summary does NOT say 'Broadly bullish'."""
    import overlay.sources.chart_intel as ci

    monkeypatch.setattr(ci, "ENHANCED_CHART_INTEL_ENABLED", True)

    spy_result = {
        "trend": "bullish",
        "above_200sma": True,
        "above_50sma": True,
        "rsi_status": "neutral",
        "volume_ratio": 0.8,
        "distribution_signal": True,  # key trigger
    }
    results = {"spy": spy_result}
    summary = ci._build_summary(results)

    assert "Broadly bullish" not in summary, (
        f"Expected suppression guard to override 'Broadly bullish', got: {summary!r}"
    )
    # Should contain distribution/resistance language
    assert any(kw in summary.lower() for kw in ("distribution", "resistance")), (
        f"Expected distribution/resistance language in summary, got: {summary!r}"
    )


def test_suppression_guard_inactive_when_no_distribution_signal(monkeypatch):
    """With flag ON but distribution_signal=False → normal 'Broadly bullish' summary."""
    import overlay.sources.chart_intel as ci

    monkeypatch.setattr(ci, "ENHANCED_CHART_INTEL_ENABLED", True)

    spy_result = {
        "trend": "bullish",
        "above_200sma": True,
        "above_50sma": True,
        "rsi_status": "neutral",
        "volume_ratio": 1.2,
        "distribution_signal": False,
    }
    results = {"spy": spy_result}
    summary = ci._build_summary(results)

    assert summary.startswith("Broadly bullish"), (
        f"Expected 'Broadly bullish' prefix when no distribution, got: {summary!r}"
    )


# ---------------------------------------------------------------------------
# Test 7: Feature flag OFF preserves original behavior
# ---------------------------------------------------------------------------


def test_feature_flag_off_preserves_original_keys(monkeypatch, tmp_path):
    """With flag OFF, _analyse_ticker returns only original keys — no enhanced keys."""
    import overlay.sources.chart_intel as ci

    monkeypatch.setattr(ci, "ENHANCED_CHART_INTEL_ENABLED", False)

    # Build a synthetic parquet so _load_ohlcv returns real data without network
    n = 120
    rng = np.random.RandomState(99)
    closes = np.linspace(100, 115, n) + rng.normal(0, 0.3, n)
    df = pd.DataFrame(
        {
            "open": closes * 0.999,
            "high": closes * 1.005,
            "low": closes * 0.995,
            "close": closes,
            "volume": np.full(n, 1_000_000.0),
        }
    )
    # Write to a temp parquet, patch _load_ohlcv to return it
    monkeypatch.setattr(ci, "_load_ohlcv", lambda ticker: df)

    result = ci._analyse_ticker("FAKE")
    assert result is not None

    # All original keys must be present
    original_keys = {
        "trend", "above_200sma", "above_50sma", "above_20sma",
        "sma20", "sma50", "sma200", "rsi", "rsi_status",
        "volume_ratio", "momentum_20d", "support", "resistance", "last_close",
    }
    assert original_keys.issubset(result.keys()), (
        f"Missing original keys: {original_keys - result.keys()}"
    )

    # No enhanced keys when flag is OFF
    enhanced_keys = {
        "obv_slope_20d", "resistance_60d", "resistance_60d_touches",
        "price_volume_divergence", "distribution_signal",
    }
    leaked = enhanced_keys & result.keys()
    assert not leaked, f"Enhanced keys leaked with flag OFF: {leaked}"


def test_feature_flag_on_adds_enhanced_keys(monkeypatch):
    """With flag ON, _analyse_ticker adds 5 enhanced keys to output."""
    import overlay.sources.chart_intel as ci

    monkeypatch.setattr(ci, "ENHANCED_CHART_INTEL_ENABLED", True)

    n = 120
    rng = np.random.RandomState(77)
    closes = np.linspace(100, 115, n) + rng.normal(0, 0.3, n)
    df = pd.DataFrame(
        {
            "open": closes * 0.999,
            "high": closes * 1.005,
            "low": closes * 0.995,
            "close": closes,
            "volume": np.full(n, 1_000_000.0),
        }
    )
    monkeypatch.setattr(ci, "_load_ohlcv", lambda ticker: df)

    result = ci._analyse_ticker("FAKE")
    assert result is not None

    enhanced_keys = {
        "obv_slope_20d", "resistance_60d", "resistance_60d_touches",
        "price_volume_divergence", "distribution_signal",
    }
    missing = enhanced_keys - result.keys()
    assert not missing, f"Expected enhanced keys missing: {missing}"

    # Types
    assert isinstance(result["obv_slope_20d"], float)
    assert isinstance(result["resistance_60d"], float)
    assert isinstance(result["resistance_60d_touches"], int)
    assert isinstance(result["price_volume_divergence"], bool)
    assert isinstance(result["distribution_signal"], bool)


def test_feature_flag_off_summary_unchanged(monkeypatch):
    """With flag OFF, _build_summary is byte-identical to original behavior."""
    import overlay.sources.chart_intel as ci

    monkeypatch.setattr(ci, "ENHANCED_CHART_INTEL_ENABLED", False)

    spy_result = {
        "trend": "bullish",
        "above_200sma": True,
        "above_50sma": True,
        "rsi_status": "neutral",
        "volume_ratio": 1.5,
        # distribution_signal key absent — as in original behavior
    }
    results = {"spy": spy_result}
    summary = ci._build_summary(results)

    assert summary.startswith("Broadly bullish"), (
        f"Flag-OFF summary should start with 'Broadly bullish', got: {summary!r}"
    )
    assert "distribution" not in summary.lower(), (
        f"distribution language must not appear when flag is OFF: {summary!r}"
    )
