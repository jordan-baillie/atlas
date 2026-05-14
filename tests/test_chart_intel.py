"""
Tests for research.chart_intel — array-based chart analysis helpers.

All tests use synthetic data — no parquet files or network calls.
"""

from __future__ import annotations

import numpy as np
import pytest

from research.chart_intel import (
    _build_summary,
    _compute_obv_slope,
    _detect_price_volume_divergence,
    _find_multi_month_resistance,
)


# ── Test 1: OBV slope positive on accumulation ───────────────────────────────


def test_obv_slope_positive_accumulation():
    """Volume increases on up-days → OBV trend is up → positive slope."""
    n = 40
    rng = np.random.RandomState(42)
    # Mostly rising price
    prices = np.linspace(100.0, 115.0, n) + rng.normal(0, 0.3, n)
    # Volume large on up-days, small on down-days → accumulation
    volumes = np.where(
        np.diff(prices, prepend=prices[0]) >= 0,
        2_000_000.0,
        300_000.0,
    )
    slope = _compute_obv_slope(prices, volumes, lookback=20)
    assert isinstance(slope, float)
    assert slope > 0, f"Expected positive OBV slope for accumulation scenario, got {slope}"


# ── Test 2: OBV slope negative on distribution ────────────────────────────────


def test_obv_slope_negative_distribution():
    """Volume increases on down-days → OBV trend is down → negative slope."""
    n = 40
    rng = np.random.RandomState(7)
    # Mostly falling price
    prices = np.linspace(115.0, 100.0, n) + rng.normal(0, 0.3, n)
    # Volume large on down-days, small on up-days → distribution
    volumes = np.where(
        np.diff(prices, prepend=prices[0]) < 0,
        2_000_000.0,
        300_000.0,
    )
    slope = _compute_obv_slope(prices, volumes, lookback=20)
    assert isinstance(slope, float)
    assert slope < 0, f"Expected negative OBV slope for distribution scenario, got {slope}"


def test_obv_slope_returns_zero_for_insufficient_data():
    """Fewer than lookback+1 bars → returns 0.0 without raising."""
    prices = [100.0, 101.0, 99.5]
    volumes = [1_000_000.0, 1_000_000.0, 1_000_000.0]
    result = _compute_obv_slope(prices, volumes, lookback=20)
    assert result == 0.0


# ── Test 3: Multi-month resistance finds ceiling ──────────────────────────────


def test_multi_month_resistance_finds_ceiling():
    """Synth data with a clear $100 ceiling → resistance ≈ $100."""
    n = 90  # ~4 months of trading days
    # Prices mostly around $85 with a spike to $100 at index 60
    prices = np.full(n, 85.0)
    prices[60] = 100.0  # clear ceiling
    prices[61] = 98.0   # another near-resistance touch
    resistance = _find_multi_month_resistance(prices, lookback_months=3)
    assert isinstance(resistance, float)
    assert resistance == pytest.approx(100.0, abs=0.01), (
        f"Expected resistance ~$100, got {resistance}"
    )


def test_multi_month_resistance_returns_last_price_when_short():
    """Fewer bars than lookback → uses full history, returns max."""
    prices = [90.0, 95.0, 88.0, 92.0, 94.0]
    resistance = _find_multi_month_resistance(prices, lookback_months=3)
    # Max of entire short history = 95.0
    assert resistance == pytest.approx(95.0, abs=0.01)


# ── Test 4: Price-volume divergence bearish detection ─────────────────────────


def test_price_volume_divergence_bearish():
    """Rising price + falling volume → bearish divergence detected, magnitude > 0."""
    n = 25
    prices = np.linspace(100.0, 110.0, n)  # price up +10%
    # Volume declining steeply: 2M → 400K (≈ 3% / day decline in relative terms)
    volumes = np.linspace(2_000_000.0, 400_000.0, n)

    detected, magnitude = _detect_price_volume_divergence(prices, volumes, window=20)
    assert detected is True, "Expected bearish divergence to be detected"
    assert magnitude > 0.0, f"Expected positive magnitude, got {magnitude}"


def test_price_volume_divergence_false_when_volume_rising():
    """Price up + volume also rising → healthy move, no divergence."""
    n = 25
    prices = np.linspace(100.0, 110.0, n)
    volumes = np.linspace(1_000_000.0, 1_500_000.0, n)  # volume rising

    detected, magnitude = _detect_price_volume_divergence(prices, volumes, window=20)
    assert detected is False, "No divergence expected when volume is rising"
    assert magnitude == 0.0


def test_price_volume_divergence_false_when_price_falling():
    """Falling price → divergence requires rising price → always False."""
    n = 25
    prices = np.linspace(110.0, 100.0, n)  # price down
    volumes = np.linspace(2_000_000.0, 400_000.0, n)  # volume also down

    detected, magnitude = _detect_price_volume_divergence(prices, volumes, window=20)
    assert detected is False


# ── Test 5: Suppression guard in _build_summary ───────────────────────────────


def test_summary_surfaces_overlay_suppression():
    """overlay_context with sizing_override=0.3 → summary contains ⚠️ Overlay suppressed."""
    results = {
        "spy": {
            "trend": "bullish",
            "above_200sma": True,
            "above_50sma": True,
            "rsi_status": "neutral",
            "volume_ratio": 1.2,
            "distribution_signal": False,
        }
    }
    overlay_context = {
        "sizing_override": 0.3,
        "sizing_reason": "VIX spike detected",
    }
    summary = _build_summary(results, overlay_context=overlay_context)
    assert "⚠️ Overlay suppressed" in summary, (
        f"Expected suppression warning in summary, got: {summary!r}"
    )
    assert "VIX spike detected" in summary, (
        f"Expected reason string in summary, got: {summary!r}"
    )


def test_summary_no_suppression_when_override_above_threshold():
    """sizing_override=0.8 (>0.5 threshold) → normal summary, no suppression prefix."""
    results = {
        "spy": {
            "trend": "bullish",
            "above_200sma": True,
            "above_50sma": True,
            "rsi_status": "neutral",
            "volume_ratio": 1.2,
        }
    }
    overlay_context = {"sizing_override": 0.8, "sizing_reason": "mild caution"}
    summary = _build_summary(results, overlay_context=overlay_context)
    assert "⚠️ Overlay suppressed" not in summary
    assert summary.startswith("Broadly bullish"), f"Got: {summary!r}"


def test_summary_no_overlay_context_is_normal():
    """No overlay_context → standard SPY-anchored summary."""
    results = {
        "spy": {
            "trend": "bullish",
            "above_200sma": True,
            "above_50sma": True,
            "rsi_status": "neutral",
            "volume_ratio": 1.1,
        }
    }
    summary = _build_summary(results)
    assert summary.startswith("Broadly bullish"), f"Got: {summary!r}"
    assert "⚠️" not in summary


def test_summary_distribution_signal_via_overlay_context():
    """distribution_signal=True in overlay_context → resistance language."""
    results = {
        "spy": {
            "trend": "bullish",
            "above_200sma": True,
            "above_50sma": True,
            "rsi_status": "neutral",
            "volume_ratio": 0.4,
        }
    }
    overlay_context = {
        "distribution_signal": True,
        "sizing_override": 0.3,
        "sizing_reason": "distribution top",
    }
    summary = _build_summary(results, overlay_context=overlay_context)
    assert "distribution" in summary.lower() or "resistance" in summary.lower(), (
        f"Expected distribution/resistance language, got: {summary!r}"
    )
