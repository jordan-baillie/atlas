"""
research.chart_intel — Array-based chart analysis helpers for the research pipeline.

Distinct from ``overlay.sources.chart_intel`` (which operates on DataFrames and
drives the overlay engine).  This module exposes lightweight array-in/scalar-out
functions that research strategies can call without loading full DataFrames.

Public API
----------
    _compute_obv_slope(prices, volumes, lookback=20) -> float
    _find_multi_month_resistance(prices, lookback_months=3) -> float
    _detect_price_volume_divergence(prices, volumes) -> tuple[bool, float]
    _build_summary(results, overlay_context=None) -> str

All inputs accept array-like (list, np.ndarray) — no DataFrame required.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Trading days per calendar month (approximate).
_TRADING_DAYS_PER_MONTH: int = 21

# Minimum normalised volume slope to flag bearish divergence (0.5 %/day decline).
_DIVERGENCE_SLOPE_THRESHOLD: float = -0.005

# Sizing-override multiplier below which the suppression guard fires.
_SUPPRESSION_THRESHOLD: float = 0.5


# ── OBV Slope ────────────────────────────────────────────────────────────────


def _compute_obv_slope(
    prices: Sequence[float],
    volumes: Sequence[float],
    lookback: int = 20,
) -> float:
    """Return the normalised OBV regression slope over *lookback* bars.

    Positive → accumulation (volume flowing in on up-days).
    Negative → distribution (volume flowing in on down-days).
    Returns 0.0 when insufficient data or volumes are degenerate.

    Parameters
    ----------
    prices:   Close prices, ascending chronological order.
    volumes:  Corresponding bar volumes.
    lookback: Number of bars for the trailing regression (default 20).
    """
    p = np.asarray(prices, dtype=float)
    v = np.asarray(volumes, dtype=float)

    if len(p) < lookback + 1 or len(p) != len(v):
        return 0.0

    # Build OBV: cumulative sum of signed volume.
    diff = np.diff(p)  # length n-1
    direction = np.sign(diff)  # +1, -1, 0

    # OBV values aligned to *v[1:]* (we need prices[i-1] vs prices[i]).
    obv_increments = direction * v[1:]
    obv = np.cumsum(obv_increments)  # length n-1

    # Take the trailing *lookback* values of OBV.
    recent = obv[-lookback:]
    if len(recent) < lookback or np.any(np.isnan(recent)):
        return 0.0

    # Linear regression slope.
    x = np.arange(len(recent), dtype=float)
    x_mean = x.mean()
    y_mean = recent.mean()
    num = float(((x - x_mean) * (recent - y_mean)).sum())
    den = float(((x - x_mean) ** 2).sum())
    if den == 0.0:
        return 0.0

    slope = num / den
    # Normalise by mean OBV magnitude for cross-ticker comparability.
    norm = abs(y_mean) if abs(y_mean) > 1e-9 else 1.0
    return float(slope / norm)


# ── Multi-Month Resistance ─────────────────────────────────────────────────────


def _find_multi_month_resistance(
    prices: Sequence[float],
    lookback_months: int = 3,
) -> float:
    """Return the price ceiling (resistance level) from the past *lookback_months*.

    Strategy: the maximum closing price over the lookback window.  This is the
    simplest defensible definition of resistance for a research signal — a level
    where sellers previously dominated.

    Parameters
    ----------
    prices:          Close prices, ascending chronological order.
    lookback_months: Calendar months to look back (default 3 ≈ 63 trading days).

    Returns the resistance price, or ``prices[-1]`` if history is shorter than the
    lookback window (safe fallback — treat current price as the ceiling).
    """
    p = np.asarray(prices, dtype=float)
    if len(p) == 0:
        return 0.0

    lookback_bars = lookback_months * _TRADING_DAYS_PER_MONTH
    window = p[-lookback_bars:] if len(p) >= lookback_bars else p

    # Filter NaN before computing max.
    valid = window[~np.isnan(window)]
    if len(valid) == 0:
        return float(p[-1])

    return float(valid.max())


# ── Price-Volume Divergence ───────────────────────────────────────────────────


def _detect_price_volume_divergence(
    prices: Sequence[float],
    volumes: Sequence[float],
    window: int = 20,
) -> tuple[bool, float]:
    """Detect bearish price-volume divergence over *window* bars.

    Divergence = price rising while volume is declining (distribution signal).

    Returns
    -------
    (detected: bool, magnitude: float)
        ``detected`` is True when:
            * price change over window > 0 (rising price), AND
            * normalised volume slope < -0.005 (>0.5 %/day decline).
        ``magnitude`` is the absolute value of the normalised volume slope when
        divergence is detected, else 0.0.
    """
    p = np.asarray(prices, dtype=float)
    v = np.asarray(volumes, dtype=float)

    if len(p) != len(v) or len(p) < window + 1:
        return False, 0.0

    recent_p = p[-window:]
    recent_v = v[-window:].astype(float)

    # Price must be rising over the window.
    price_change = (recent_p[-1] - recent_p[0]) / recent_p[0] if recent_p[0] != 0 else 0.0
    if price_change <= 0:
        return False, 0.0

    # Volume slope via linear regression.
    x = np.arange(len(recent_v), dtype=float)
    x_mean = x.mean()
    v_mean = recent_v.mean()
    if v_mean == 0:
        return False, 0.0

    num = float(((x - x_mean) * (recent_v - v_mean)).sum())
    den = float(((x - x_mean) ** 2).sum())
    if den == 0.0:
        return False, 0.0

    norm_slope = (num / den) / v_mean  # fractional per-bar rate of change

    if norm_slope < _DIVERGENCE_SLOPE_THRESHOLD:
        return True, abs(norm_slope)
    return False, 0.0


# ── Summary Builder ────────────────────────────────────────────────────────────


def _build_summary(
    results: dict[str, Any],
    overlay_context: Optional[dict[str, Any]] = None,
) -> str:
    """Build a plain-English narrative summary of chart analysis results.

    Suppression guard
    -----------------
    If *overlay_context* contains ``sizing_override`` with a value below
    ``_SUPPRESSION_THRESHOLD`` (0.5), the summary is prefixed with a
    suppression warning that includes the reason (if provided).

    Parameters
    ----------
    results:         Dict of ticker → analysis dict (from _analyse_ticker or
                     similar).  A ``"spy"`` key is used as the primary anchor.
    overlay_context: Optional dict from the overlay engine.  Recognised keys:
                       - ``sizing_override``: float multiplier (0–1)
                       - ``sizing_reason``:   human-readable reason string
                       - ``distribution_signal``: bool — at-resistance low volume
    """
    # ── Suppression guard ──────────────────────────────────────────────────
    suppression_prefix = ""
    if overlay_context:
        sizing_mult = overlay_context.get("sizing_override")
        if sizing_mult is not None and float(sizing_mult) < _SUPPRESSION_THRESHOLD:
            reason = overlay_context.get("sizing_reason", "overlay signal")
            suppression_prefix = f"⚠️ Overlay suppressed: {reason} (multiplier={sizing_mult:.2f}) — "

    # ── SPY-anchored narrative ─────────────────────────────────────────────
    spy = results.get("spy")
    ticker_results = {k: v for k, v in results.items() if isinstance(v, dict) and k != "spy"}

    if spy is None:
        # Fallback: simple breadth count.
        if not ticker_results:
            return suppression_prefix + "Insufficient data for market summary"
        bullish = sum(1 for v in ticker_results.values() if v.get("trend") == "bullish")
        total = len(ticker_results)
        if bullish >= total * 0.6:
            core = f"Broadly bullish ({bullish}/{total} tickers)"
        elif bullish <= total * 0.3:
            core = f"Broadly bearish ({bullish}/{total} tickers bullish)"
        else:
            core = f"Mixed market conditions ({bullish}/{total} tickers bullish)"
        return suppression_prefix + core

    spy_trend = spy.get("trend", "neutral")
    above_200 = spy.get("above_200sma", False)
    above_50 = spy.get("above_50sma", False)
    rsi_stat = spy.get("rsi_status", "neutral")
    vol_ratio = spy.get("volume_ratio", 1.0)

    bullish_count = sum(1 for v in ticker_results.values() if v.get("trend") == "bullish")
    total_others = len(ticker_results)

    parts: list[str] = []

    # Distribution / suppression overrides bullish prefix.
    at_distribution = spy.get("distribution_signal", False) or (
        overlay_context is not None and overlay_context.get("distribution_signal", False)
    )

    if at_distribution:
        prefix = "At resistance on low volume — possible distribution"
    elif spy_trend == "bullish":
        ma_desc = []
        if above_200:
            ma_desc.append("200")
        if above_50:
            ma_desc.append("50")
        parts.append(f"SPY above {'/'.join(ma_desc)}SMA" if ma_desc else "SPY trending bullish")
        prefix = "Broadly bullish"
    elif spy_trend == "bearish":
        parts.append("SPY in downtrend")
        prefix = "Broadly bearish"
    else:
        parts.append("SPY neutral/consolidating")
        prefix = "Mixed market"

    if rsi_stat == "overbought":
        parts.append("RSI overbought — watch for pullback")
    elif rsi_stat == "oversold":
        parts.append("RSI oversold — potential bounce")

    if vol_ratio > 1.3:
        parts.append("high volume confirming move")
    elif vol_ratio < 0.7:
        parts.append("low-volume — conviction suspect")

    if total_others > 0:
        if bullish_count >= int(total_others * 0.6):
            parts.append(f"broad participation ({bullish_count}/{total_others} ETFs bullish)")
        elif bullish_count <= int(total_others * 0.3):
            parts.append(f"limited breadth ({bullish_count}/{total_others} ETFs bullish)")

    detail = ", ".join(parts)
    core = f"{prefix} — {detail}" if detail else prefix
    return suppression_prefix + core
