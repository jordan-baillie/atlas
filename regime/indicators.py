"""
regime/indicators.py — Individual indicator scoring functions for the Atlas regime model.

Each function converts one macro dimension into a normalised float score
in the range [-1.0, +1.0] where:
    +1.0  → fully bullish / risk-on
     0.0  → neutral / uncertain
    -1.0  → fully bearish / risk-off

All thresholds and weights are read from ``config/active/regime.json`` — no
magic numbers live in this module.  Missing or non-finite indicator values
return 0.0 (neutral) rather than crashing.

Usage
-----
    import json
    from regime.indicators import compute_all_scores

    config = json.load(open("config/active/regime.json"))
    scores = compute_all_scores(macro_row, config)
    # → {"trend": 0.32, "risk": 0.84, ..., "composite": 0.61}
"""
from __future__ import annotations

import math
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# Private helpers
# ──────────────────────────────────────────────────────────────────────────────


def _safe_float(value: object) -> Optional[float]:
    """
    Convert *value* to a finite float, returning None for missing / non-finite data.

    Handles None, NaN, infinity, and any type that can be coerced via float().
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp *value* to the closed interval [*min_val*, *max_val*]."""
    return max(min_val, min(max_val, value))


def _linear_map(
    value: float,
    in_low: float,
    in_high: float,
    out_low: float,
    out_high: float,
) -> float:
    """
    Linearly interpolate *value* from the input range to the output range.

    Points outside the input range are extrapolated (caller should clamp
    afterwards if hard boundaries are required).

    When ``in_high == in_low`` (degenerate range) the midpoint of the output
    range is returned.
    """
    if in_high == in_low:
        return (out_low + out_high) / 2.0
    t = (value - in_low) / (in_high - in_low)
    return out_low + t * (out_high - out_low)


# ──────────────────────────────────────────────────────────────────────────────
# Individual indicator scoring functions
# ──────────────────────────────────────────────────────────────────────────────


def trend_score(indicators: dict, config: dict) -> float:
    """
    Score market trend based on SPY's position relative to its 200-day moving
    average and the slope of that average.

    Sub-scores
    ----------
    above_score : float
        +0.5 when SPY is above the 200-DMA (bullish trend intact),
        -0.5 when below (trend broken).  Source: ``spy_above_200dma`` (0/1).

    slope_score : float
        Continuous score in [-1, 1] derived via ``math.tanh`` of the
        normalised 200-DMA slope.  A positive slope confirms an uptrend;
        negative confirms a downtrend.  Source: ``spy_200dma_slope``.

    The two sub-scores are combined with the weights taken from
    ``config["trend_thresholds"]["spy_above_200dma_weight"]`` (60%) and
    ``spy_200dma_slope_positive_weight`` (40%).

    Returns
    -------
    float
        Score in [-1.0, +1.0].  Returns 0.0 if both inputs are missing.
    """
    tt = config["trend_thresholds"]
    w_above = float(tt["spy_above_200dma_weight"])
    w_slope = float(tt["spy_200dma_slope_positive_weight"])
    slope_threshold = float(tt["slope_threshold"])

    # --- Sub-score 1: binary above/below 200-DMA ---------------------------
    above_raw = _safe_float(indicators.get("spy_above_200dma"))
    if above_raw is None:
        above_score = 0.0
    else:
        above_score = 0.5 if above_raw >= 0.5 else -0.5

    # --- Sub-score 2: slope magnitude and direction ------------------------
    slope = _safe_float(indicators.get("spy_200dma_slope"))
    if slope is None:
        slope_score = 0.0
    else:
        # math.tanh naturally bounds to (-1, 1) and preserves sign + magnitude.
        # Subtracting slope_threshold shifts the neutral point (default 0.0).
        slope_score = math.tanh(slope - slope_threshold)

    combined = w_above * above_score + w_slope * slope_score
    return _clamp(combined, -1.0, 1.0)


def risk_score(indicators: dict, config: dict) -> float:
    """
    Score market risk appetite using VIX level and VIX term structure.

    VIX level (60% weight)
    ----------------------
    Linearly mapped from ``[vix_low, vix_extreme]`` → ``[+1.0, -1.0]``.
    VIX below *vix_low* (calm) → clamped to +1.0.
    VIX above *vix_extreme* (panic) → clamped to -1.0.

    VIX term structure (40% weight)
    --------------------------------
    Ratio = VIX / VIX3M (``vix_term_ratio`` indicator).
    - Ratio < 1.0 → contango → bullish (forward vol priced lower than spot).
    - Ratio > 1.0 → backwardation → bearish (market stressed).

    Mapped so that ratio = 1.0 → 0.0 and ratio = ``vix_term_ratio_danger`` → -1.0.
    The symmetric bullish extreme is achieved at the same distance below 1.0.

    Returns
    -------
    float
        Score in [-1.0, +1.0].  Returns 0.0 if both inputs are missing.
    """
    rt = config["risk_thresholds"]
    vix_low = float(rt["vix_low"])
    vix_extreme = float(rt["vix_extreme"])
    danger_ratio = float(rt["vix_term_ratio_danger"])

    # --- VIX level score ---------------------------------------------------
    vix = _safe_float(indicators.get("vix"))
    if vix is None:
        vix_level_score = 0.0
    else:
        raw = _linear_map(vix, vix_low, vix_extreme, 1.0, -1.0)
        vix_level_score = _clamp(raw, -1.0, 1.0)

    # --- VIX term-structure score ------------------------------------------
    term_ratio = _safe_float(indicators.get("vix_term_ratio"))
    if term_ratio is None:
        term_score = 0.0
    else:
        # Scale: at ratio = danger_ratio the score is -1.0; symmetric above/below 1.0.
        # Denominator = (danger_ratio - 1.0) so the distance from neutral is consistent.
        denom = danger_ratio - 1.0 if danger_ratio != 1.0 else 0.2
        term_score = _clamp((1.0 - term_ratio) / denom, -1.0, 1.0)

    # Fixed design weights: 60% VIX level, 40% term structure.
    combined = 0.6 * vix_level_score + 0.4 * term_score
    return _clamp(combined, -1.0, 1.0)


def credit_score(indicators: dict, config: dict) -> float:
    """
    Score credit conditions using the Investment-Grade credit OAS spread
    (BAMLC0A0CM, in percentage points).

    A tighter spread (low OAS) signals easy credit conditions → bullish.
    A wider spread (high OAS) signals financial stress → bearish.

    Linearly mapped from ``[oas_normal, oas_crisis]`` → ``[+1.0, -1.0]``.
    OAS below *oas_normal* is clamped to +1.0; above *oas_crisis* to -1.0.

    Returns
    -------
    float
        Score in [-1.0, +1.0].  Returns 0.0 if the indicator is missing.
    """
    ct = config["credit_thresholds"]
    oas_normal = float(ct["oas_normal"])
    oas_crisis = float(ct["oas_crisis"])

    oas = _safe_float(indicators.get("credit_oas"))
    if oas is None:
        return 0.0

    raw = _linear_map(oas, oas_normal, oas_crisis, 1.0, -1.0)
    return _clamp(raw, -1.0, 1.0)


def yield_curve_score(indicators: dict, config: dict) -> float:
    """
    Score the yield curve shape using two spread measures.

    10-year minus 2-year spread (``yield_curve_10y2y``)
    10-year minus 3-month spread (``yield_curve_10y3m``)

    Both spreads are individually mapped from
    ``[-steep_threshold, +steep_threshold]`` → ``[-1.0, +1.0]``:
    - Positive spread → upward-sloping curve → bullish.
    - Negative (inverted) spread → bearish.
    - Spread at or above *steep_threshold* → clamped to +1.0.
    - Spread at or below *-steep_threshold* → clamped to -1.0.

    The two scores are averaged.  If only one series is available, that
    single score is returned.  Returns 0.0 when both are missing.

    Returns
    -------
    float
        Score in [-1.0, +1.0].
    """
    yc = config["yield_curve_thresholds"]
    steep = float(yc["steep_threshold"])

    def _score_spread(spread_key: str) -> Optional[float]:
        spread = _safe_float(indicators.get(spread_key))
        if spread is None:
            return None
        raw = _linear_map(spread, -steep, steep, -1.0, 1.0)
        return _clamp(raw, -1.0, 1.0)

    s1 = _score_spread("yield_curve_10y2y")
    s2 = _score_spread("yield_curve_10y3m")

    if s1 is None and s2 is None:
        return 0.0
    if s1 is None:
        return s2  # type: ignore[return-value]
    if s2 is None:
        return s1

    return (s1 + s2) / 2.0


def dollar_score(indicators: dict, config: dict) -> float:
    """
    Score dollar strength using the DXY index level.

    A strong dollar (high DXY) acts as a headwind for global equities and
    commodities → slightly bearish for risk assets.
    A weak dollar (low DXY) is typically risk-on for equities and commodities.

    Linearly mapped from ``[dxy_weak, dxy_strong]`` → ``[+1.0, -1.0]``.
    DXY below *dxy_weak* → clamped to +1.0 (very risk-on).
    DXY above *dxy_strong* → clamped to -1.0 (risk-off signal).

    Returns
    -------
    float
        Score in [-1.0, +1.0].  Returns 0.0 if the indicator is missing.
    """
    dt = config["dollar_thresholds"]
    dxy_weak = float(dt["dxy_weak"])
    dxy_strong = float(dt["dxy_strong"])

    dxy = _safe_float(indicators.get("dxy"))
    if dxy is None:
        return 0.0

    raw = _linear_map(dxy, dxy_weak, dxy_strong, 1.0, -1.0)
    return _clamp(raw, -1.0, 1.0)


def commodity_score(indicators: dict, config: dict) -> float:
    """
    Score commodity market risk appetite using the gold/copper ratio.

    Gold is a safe-haven asset; copper is an industrial metal tied to global
    growth.  A high gold/copper ratio indicates gold outperforming copper →
    risk-off.  A low ratio indicates copper outperforming gold → risk-on.

    Linearly mapped from
    ``[gold_copper_ratio_risk_on_below, gold_copper_ratio_risk_off_above]``
    → ``[+1.0, -1.0]``.

    Returns
    -------
    float
        Score in [-1.0, +1.0].  Returns 0.0 if the indicator is missing.
    """
    ct = config["commodity_thresholds"]
    risk_on_below = float(ct["gold_copper_ratio_risk_on_below"])
    risk_off_above = float(ct["gold_copper_ratio_risk_off_above"])

    ratio = _safe_float(indicators.get("gold_copper_ratio"))
    if ratio is None:
        return 0.0

    raw = _linear_map(ratio, risk_on_below, risk_off_above, 1.0, -1.0)
    return _clamp(raw, -1.0, 1.0)


# ──────────────────────────────────────────────────────────────────────────────
# Composite scorer
# ──────────────────────────────────────────────────────────────────────────────


def compute_all_scores(indicators: dict, config: dict) -> dict:
    """
    Compute all six indicator scores and a weighted composite score.

    Each individual score is computed independently; if an indicator is
    missing or non-finite, the corresponding score defaults to 0.0 (neutral)
    without raising an exception.

    The composite is the weighted sum defined in
    ``config["weights"]``:
        composite = sum(weight_i * score_i for i in dimensions)

    Parameters
    ----------
    indicators : dict
        A single-date macro indicator row, typically from
        ``db.atlas_db.get_macro_indicators()``.  Expected keys:
        ``spy_above_200dma``, ``spy_200dma_slope``, ``vix``, ``vix3m``,
        ``vix_term_ratio``, ``credit_oas``, ``yield_curve_10y2y``,
        ``yield_curve_10y3m``, ``dxy``, ``gold_copper_ratio``.

    config : dict
        Contents of ``config/active/regime.json`` (parsed JSON dict).

    Returns
    -------
    dict
        Keys: ``"trend"``, ``"risk"``, ``"credit"``, ``"yield_curve"``,
        ``"dollar"``, ``"commodity"``, ``"composite"``.  All values are
        floats in [-1.0, +1.0].

    Examples
    --------
    >>> import json
    >>> from regime.indicators import compute_all_scores
    >>> config = json.load(open("config/active/regime.json"))
    >>> bull = {
    ...     "spy_above_200dma": 1, "spy_200dma_slope": 0.05,
    ...     "vix": 15, "vix_term_ratio": 0.88,
    ...     "credit_oas": 0.8,
    ...     "yield_curve_10y2y": 1.5, "yield_curve_10y3m": 2.0,
    ...     "dxy": 100, "gold_copper_ratio": 16,
    ... }
    >>> scores = compute_all_scores(bull, config)
    >>> scores["composite"] > 0.3
    True
    """
    scores: dict[str, float] = {
        "trend":       trend_score(indicators, config),
        "risk":        risk_score(indicators, config),
        "credit":      credit_score(indicators, config),
        "yield_curve": yield_curve_score(indicators, config),
        "dollar":      dollar_score(indicators, config),
        "commodity":   commodity_score(indicators, config),
    }

    weights = config["weights"]
    composite = (
        float(weights["trend"])       * scores["trend"]
        + float(weights["risk"])        * scores["risk"]
        + float(weights["credit"])      * scores["credit"]
        + float(weights["yield_curve"]) * scores["yield_curve"]
        + float(weights["dollar"])      * scores["dollar"]
        + float(weights["commodity"])   * scores["commodity"]
    )
    scores["composite"] = _clamp(composite, -1.0, 1.0)
    return scores
