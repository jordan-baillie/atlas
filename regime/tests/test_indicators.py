"""
regime/tests/test_indicators.py — Unit tests for regime/indicators.py.

Run with:
    cd /root/atlas && python -m pytest regime/tests/test_indicators.py -v

Coverage
--------
- Each scoring function with known, hand-calculated inputs and expected outputs
- Extreme values that force saturation to ±1.0
- Missing / None indicator values (must return 0.0, never raise)
- NaN and infinity values (must return 0.0)
- Composite score calculation with explicit weight arithmetic
- All individual and composite scores are clamped to [-1.0, +1.0]
- Monotonicity: more bullish inputs produce higher scores
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

# ── Project root on path ───────────────────────────────────────────────────────
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from regime.indicators import (
    _clamp,
    _linear_map,
    _safe_float,
    commodity_score,
    compute_all_scores,
    credit_score,
    dollar_score,
    risk_score,
    trend_score,
    yield_curve_score,
)

# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

CONFIG_PATH = PROJECT / "config" / "active" / "regime.json"


@pytest.fixture(scope="session")
def cfg() -> dict:
    """Load the real regime.json config once per session."""
    with CONFIG_PATH.open() as f:
        return json.load(f)


@pytest.fixture(scope="session")
def bull_indicators() -> dict:
    """Macro row representing a textbook bull / risk-on environment."""
    return {
        "spy_close": 500,
        "spy_200dma": 450,
        "spy_above_200dma": 1,
        "spy_200dma_slope": 0.05,
        "vix": 15,
        "vix3m": 17,
        "vix_term_ratio": 0.88,
        "credit_oas": 0.8,
        "yield_curve_10y2y": 1.5,
        "yield_curve_10y3m": 2.0,
        "dxy": 100,
        "gold_copper_ratio": 16,
    }


@pytest.fixture(scope="session")
def bear_indicators() -> dict:
    """Macro row representing a textbook bear / risk-off environment."""
    return {
        "spy_close": 350,
        "spy_200dma": 450,
        "spy_above_200dma": 0,
        "spy_200dma_slope": -0.08,
        "vix": 40,
        "vix3m": 30,
        "vix_term_ratio": 1.33,
        "credit_oas": 3.0,
        "yield_curve_10y2y": -0.8,
        "yield_curve_10y3m": -1.2,
        "dxy": 108,
        # Updated to 700 (> risk_off threshold of 650) to reflect real-world
        # bear values; old value of 25 was below the corrected 400/650 scale.
        "gold_copper_ratio": 700,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Helper tests
# ──────────────────────────────────────────────────────────────────────────────


class TestHelpers:
    # --- _safe_float --------------------------------------------------------

    def test_safe_float_none_returns_none(self):
        assert _safe_float(None) is None

    def test_safe_float_nan_returns_none(self):
        assert _safe_float(float("nan")) is None

    def test_safe_float_inf_returns_none(self):
        assert _safe_float(float("inf")) is None

    def test_safe_float_neg_inf_returns_none(self):
        assert _safe_float(float("-inf")) is None

    def test_safe_float_int(self):
        assert _safe_float(42) == pytest.approx(42.0)

    def test_safe_float_string_number(self):
        assert _safe_float("3.14") == pytest.approx(3.14)

    def test_safe_float_string_garbage_returns_none(self):
        assert _safe_float("abc") is None

    def test_safe_float_zero(self):
        assert _safe_float(0) == pytest.approx(0.0)

    # --- _clamp -------------------------------------------------------------

    def test_clamp_below_min(self):
        assert _clamp(-5.0, -1.0, 1.0) == pytest.approx(-1.0)

    def test_clamp_above_max(self):
        assert _clamp(5.0, -1.0, 1.0) == pytest.approx(1.0)

    def test_clamp_within_range(self):
        assert _clamp(0.5, -1.0, 1.0) == pytest.approx(0.5)

    def test_clamp_at_boundaries(self):
        assert _clamp(-1.0, -1.0, 1.0) == pytest.approx(-1.0)
        assert _clamp(1.0, -1.0, 1.0) == pytest.approx(1.0)

    # --- _linear_map --------------------------------------------------------

    def test_linear_map_midpoint(self):
        # mid of [0, 10] maps to mid of [-1, 1] = 0
        assert _linear_map(5.0, 0.0, 10.0, -1.0, 1.0) == pytest.approx(0.0)

    def test_linear_map_low_boundary(self):
        assert _linear_map(0.0, 0.0, 10.0, -1.0, 1.0) == pytest.approx(-1.0)

    def test_linear_map_high_boundary(self):
        assert _linear_map(10.0, 0.0, 10.0, -1.0, 1.0) == pytest.approx(1.0)

    def test_linear_map_degenerate_range(self):
        """Degenerate input range → midpoint of output."""
        result = _linear_map(5.0, 5.0, 5.0, -1.0, 1.0)
        assert result == pytest.approx(0.0)

    def test_linear_map_inverted_output(self):
        """Descending output range (bullish → low input)."""
        # value = low → out_high, value = high → out_low
        assert _linear_map(0.0, 0.0, 10.0, 1.0, -1.0) == pytest.approx(1.0)
        assert _linear_map(10.0, 0.0, 10.0, 1.0, -1.0) == pytest.approx(-1.0)


# ──────────────────────────────────────────────────────────────────────────────
# trend_score
# ──────────────────────────────────────────────────────────────────────────────


class TestTrendScore:
    def test_bullish_inputs(self, cfg, bull_indicators):
        score = trend_score(bull_indicators, cfg)
        assert score > 0.0, f"Bull market should give positive trend score, got {score}"

    def test_bearish_inputs(self, cfg, bear_indicators):
        score = trend_score(bear_indicators, cfg)
        assert score < 0.0, f"Bear market should give negative trend score, got {score}"

    def test_above_200dma_contributes_positive(self, cfg):
        ind = {"spy_above_200dma": 1, "spy_200dma_slope": 0.0}
        score = trend_score(ind, cfg)
        assert score > 0.0

    def test_below_200dma_contributes_negative(self, cfg):
        ind = {"spy_above_200dma": 0, "spy_200dma_slope": 0.0}
        score = trend_score(ind, cfg)
        assert score < 0.0

    def test_positive_slope_increases_score(self, cfg):
        flat = {"spy_above_200dma": 1, "spy_200dma_slope": 0.0}
        steep = {"spy_above_200dma": 1, "spy_200dma_slope": 0.5}
        assert trend_score(steep, cfg) > trend_score(flat, cfg)

    def test_negative_slope_decreases_score(self, cfg):
        flat = {"spy_above_200dma": 1, "spy_200dma_slope": 0.0}
        declining = {"spy_above_200dma": 1, "spy_200dma_slope": -0.5}
        assert trend_score(declining, cfg) < trend_score(flat, cfg)

    def test_missing_above_returns_neutral_leaning_slope(self, cfg):
        """When above_200dma is None, only slope contributes."""
        ind = {"spy_above_200dma": None, "spy_200dma_slope": 0.0}
        score = trend_score(ind, cfg)
        assert score == pytest.approx(0.0, abs=1e-9)

    def test_all_missing_returns_zero(self, cfg):
        assert trend_score({}, cfg) == pytest.approx(0.0)

    def test_score_clamped_to_range(self, cfg):
        # Extreme slope that tanh saturates
        ind = {"spy_above_200dma": 1, "spy_200dma_slope": 1000.0}
        score = trend_score(ind, cfg)
        assert -1.0 <= score <= 1.0

    def test_nan_slope_treated_as_missing(self, cfg):
        ind_nan = {"spy_above_200dma": 1, "spy_200dma_slope": float("nan")}
        ind_none = {"spy_above_200dma": 1, "spy_200dma_slope": None}
        assert trend_score(ind_nan, cfg) == pytest.approx(trend_score(ind_none, cfg))

    def test_known_calculation(self, cfg):
        """
        Hand-calculated expected value (SPY *above* 200 DMA).

        above_score = +0.5 (spy_above_200dma = 1)
        slope_score = tanh(0.05 - 0.0) ≈ 0.04996
        w_above = 0.6, w_slope = 0.4
        combined = 0.6*0.5 + 0.4*0.04996 ≈ 0.3 + 0.01998 ≈ 0.31998
        """
        ind = {"spy_above_200dma": 1, "spy_200dma_slope": 0.05}
        expected = 0.6 * 0.5 + 0.4 * math.tanh(0.05)
        score = trend_score(ind, cfg)
        assert score == pytest.approx(expected, abs=1e-6)

    def test_known_calculation_below_200dma(self, cfg):
        """
        Hand-calculated expected value (SPY *below* 200 DMA, zero slope).

        Asymmetric: below-200DMA scores -0.7 (stronger than above +0.5).
        above_score = -0.7, slope_score = tanh(0.0) = 0.0
        combined = 0.6*(-0.7) + 0.4*0.0 = -0.42
        """
        ind = {"spy_above_200dma": 0, "spy_200dma_slope": 0.0}
        expected = 0.6 * (-0.7) + 0.4 * 0.0  # = -0.42
        score = trend_score(ind, cfg)
        assert score == pytest.approx(expected, abs=1e-6)

    def test_asymmetric_below_stronger_than_above(self, cfg):
        """Downside signal (below 200 DMA) is stronger than upside (above 200 DMA)."""
        above = trend_score({"spy_above_200dma": 1, "spy_200dma_slope": 0.0}, cfg)
        below = trend_score({"spy_above_200dma": 0, "spy_200dma_slope": 0.0}, cfg)
        assert abs(below) > abs(above), (
            f"Expected |below| > |above|, got below={below:.4f}, above={above:.4f}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# risk_score
# ──────────────────────────────────────────────────────────────────────────────


class TestRiskScore:
    def test_low_vix_bullish(self, cfg):
        """VIX = 10 (well below vix_low=20) → near +1.0."""
        ind = {"vix": 10, "vix_term_ratio": 0.85}
        score = risk_score(ind, cfg)
        assert score > 0.5

    def test_extreme_vix_bearish(self, cfg):
        """VIX = 50 (above vix_extreme=35) → near -1.0."""
        ind = {"vix": 50, "vix_term_ratio": 1.3}
        score = risk_score(ind, cfg)
        assert score < -0.5

    def test_contango_bullish(self, cfg):
        """Term ratio < 1 (contango) at neutral VIX → positive score."""
        ind = {"vix": 20, "vix_term_ratio": 0.80}  # vix=20 → score=+1 on VIX level
        score = risk_score(ind, cfg)
        assert score > 0.0

    def test_backwardation_bearish(self, cfg):
        """Term ratio > danger level → term contribution is -1."""
        rt = cfg["risk_thresholds"]
        danger = rt["vix_term_ratio_danger"]
        ind = {"vix": 20, "vix_term_ratio": danger + 0.2}
        score = risk_score(ind, cfg)
        # 0.6 * (+1.0 VIX at vix_low) + 0.4 * (-1.0 term) = 0.2
        assert score < 0.5

    def test_vix_exactly_at_low_threshold(self, cfg):
        """VIX at vix_low threshold → VIX level score = +1.0."""
        rt = cfg["risk_thresholds"]
        ind = {"vix": rt["vix_low"], "vix_term_ratio": 1.0}
        score = risk_score(ind, cfg)
        # term_score = 0 at ratio=1.0 → combined = 0.6*1.0 + 0.4*0 = 0.6
        assert score == pytest.approx(0.6, abs=1e-6)

    def test_vix_exactly_at_extreme_threshold(self, cfg):
        """VIX at vix_extreme → VIX level score = -1.0."""
        rt = cfg["risk_thresholds"]
        ind = {"vix": rt["vix_extreme"], "vix_term_ratio": 1.0}
        score = risk_score(ind, cfg)
        # term_score = 0 at ratio=1.0 → combined = 0.6*(-1.0) + 0.4*0 = -0.6
        assert score == pytest.approx(-0.6, abs=1e-6)

    def test_neutral_term_ratio(self, cfg):
        """term_ratio = 1.0 → term_score = 0.0."""
        rt = cfg["risk_thresholds"]
        ind = {"vix": rt["vix_low"], "vix_term_ratio": 1.0}
        score = risk_score(ind, cfg)
        assert score == pytest.approx(0.6, abs=1e-6)  # 0.6 * 1.0 + 0.4 * 0.0

    def test_missing_vix_returns_term_only(self, cfg):
        """Missing VIX → VIX level score = 0.0; only term structure contributes."""
        rt = cfg["risk_thresholds"]
        danger = rt["vix_term_ratio_danger"]
        ind = {"vix": None, "vix_term_ratio": danger}
        # vix_level = 0, term = -1.0 → 0.6*0 + 0.4*(-1) = -0.4
        score = risk_score(ind, cfg)
        assert score == pytest.approx(-0.4, abs=1e-6)

    def test_all_missing_returns_zero(self, cfg):
        assert risk_score({}, cfg) == pytest.approx(0.0)

    def test_score_clamped_to_range(self, cfg):
        ind = {"vix": 5, "vix_term_ratio": 0.1}
        score = risk_score(ind, cfg)
        assert -1.0 <= score <= 1.0

    def test_bull_inputs_positive(self, cfg, bull_indicators):
        assert risk_score(bull_indicators, cfg) > 0.0

    def test_bear_inputs_negative(self, cfg, bear_indicators):
        assert risk_score(bear_indicators, cfg) < 0.0


# ──────────────────────────────────────────────────────────────────────────────
# credit_score
# ──────────────────────────────────────────────────────────────────────────────


class TestCreditScore:
    def test_tight_spreads_bullish(self, cfg):
        """OAS below oas_normal → clamped to +1.0."""
        ct = cfg["credit_thresholds"]
        ind = {"credit_oas": ct["oas_normal"] - 0.5}
        assert credit_score(ind, cfg) == pytest.approx(1.0)

    def test_crisis_spreads_bearish(self, cfg):
        """OAS at oas_crisis → -1.0."""
        ct = cfg["credit_thresholds"]
        ind = {"credit_oas": ct["oas_crisis"]}
        assert credit_score(ind, cfg) == pytest.approx(-1.0, abs=1e-6)

    def test_above_crisis_clamped(self, cfg):
        """OAS way above crisis → still clamped at -1.0."""
        ct = cfg["credit_thresholds"]
        ind = {"credit_oas": ct["oas_crisis"] + 5.0}
        assert credit_score(ind, cfg) == pytest.approx(-1.0)

    def test_normal_oas_is_maximum_bullish(self, cfg):
        """OAS exactly at oas_normal → +1.0 (start of bearish zone)."""
        ct = cfg["credit_thresholds"]
        ind = {"credit_oas": ct["oas_normal"]}
        assert credit_score(ind, cfg) == pytest.approx(1.0, abs=1e-6)

    def test_midpoint_oas_is_neutral(self, cfg):
        """OAS midway between oas_normal and oas_crisis → 0.0."""
        ct = cfg["credit_thresholds"]
        mid = (ct["oas_normal"] + ct["oas_crisis"]) / 2
        ind = {"credit_oas": mid}
        assert credit_score(ind, cfg) == pytest.approx(0.0, abs=1e-6)

    def test_missing_oas_returns_zero(self, cfg):
        assert credit_score({}, cfg) == pytest.approx(0.0)

    def test_none_oas_returns_zero(self, cfg):
        assert credit_score({"credit_oas": None}, cfg) == pytest.approx(0.0)

    def test_score_monotone_decreasing(self, cfg):
        """Higher OAS → lower score."""
        scores = [credit_score({"credit_oas": v}, cfg) for v in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]]
        for a, b in zip(scores, scores[1:]):
            assert a >= b

    def test_bull_inputs_positive(self, cfg, bull_indicators):
        assert credit_score(bull_indicators, cfg) > 0.0

    def test_bear_inputs_negative(self, cfg, bear_indicators):
        assert credit_score(bear_indicators, cfg) < 0.0


# ──────────────────────────────────────────────────────────────────────────────
# yield_curve_score
# ──────────────────────────────────────────────────────────────────────────────


class TestYieldCurveScore:
    def test_steep_curve_bullish(self, cfg):
        """Both spreads at or above steep_threshold → +1.0."""
        steep = cfg["yield_curve_thresholds"]["steep_threshold"]
        ind = {"yield_curve_10y2y": steep, "yield_curve_10y3m": steep}
        assert yield_curve_score(ind, cfg) == pytest.approx(1.0, abs=1e-6)

    def test_inverted_curve_bearish(self, cfg):
        """Both spreads deeply negative → -1.0."""
        steep = cfg["yield_curve_thresholds"]["steep_threshold"]
        ind = {"yield_curve_10y2y": -steep, "yield_curve_10y3m": -steep}
        assert yield_curve_score(ind, cfg) == pytest.approx(-1.0, abs=1e-6)

    def test_flat_curve_neutral(self, cfg):
        """Both spreads at 0 → 0.0."""
        ind = {"yield_curve_10y2y": 0.0, "yield_curve_10y3m": 0.0}
        assert yield_curve_score(ind, cfg) == pytest.approx(0.0, abs=1e-6)

    def test_average_of_two_spreads(self, cfg):
        """Score is the average of both spread sub-scores."""
        steep = cfg["yield_curve_thresholds"]["steep_threshold"]
        # 10y2y = steep → +1.0; 10y3m = 0 → 0.0; average = 0.5
        ind = {"yield_curve_10y2y": steep, "yield_curve_10y3m": 0.0}
        assert yield_curve_score(ind, cfg) == pytest.approx(0.5, abs=1e-6)

    def test_falls_back_to_single_spread_when_one_missing(self, cfg):
        steep = cfg["yield_curve_thresholds"]["steep_threshold"]
        ind = {"yield_curve_10y2y": steep, "yield_curve_10y3m": None}
        assert yield_curve_score(ind, cfg) == pytest.approx(1.0, abs=1e-6)

    def test_all_missing_returns_zero(self, cfg):
        assert yield_curve_score({}, cfg) == pytest.approx(0.0)

    def test_score_clamped_to_range(self, cfg):
        ind = {"yield_curve_10y2y": 100.0, "yield_curve_10y3m": 100.0}
        assert yield_curve_score(ind, cfg) == pytest.approx(1.0)
        ind2 = {"yield_curve_10y2y": -100.0, "yield_curve_10y3m": -100.0}
        assert yield_curve_score(ind2, cfg) == pytest.approx(-1.0)

    def test_bull_inputs_positive(self, cfg, bull_indicators):
        assert yield_curve_score(bull_indicators, cfg) > 0.0

    def test_bear_inputs_negative(self, cfg, bear_indicators):
        assert yield_curve_score(bear_indicators, cfg) < 0.0


# ──────────────────────────────────────────────────────────────────────────────
# dollar_score
# ──────────────────────────────────────────────────────────────────────────────


class TestDollarScore:
    def test_weak_dollar_bullish(self, cfg):
        """DXY at or below dxy_weak → +1.0."""
        dt = cfg["dollar_thresholds"]
        ind = {"dxy": dt["dxy_weak"]}
        assert dollar_score(ind, cfg) == pytest.approx(1.0, abs=1e-6)

    def test_strong_dollar_bearish(self, cfg):
        """DXY at or above dxy_strong → -1.0."""
        dt = cfg["dollar_thresholds"]
        ind = {"dxy": dt["dxy_strong"]}
        assert dollar_score(ind, cfg) == pytest.approx(-1.0, abs=1e-6)

    def test_midpoint_neutral(self, cfg):
        """DXY at midpoint between weak and strong → 0.0."""
        dt = cfg["dollar_thresholds"]
        mid = (dt["dxy_weak"] + dt["dxy_strong"]) / 2
        ind = {"dxy": mid}
        assert dollar_score(ind, cfg) == pytest.approx(0.0, abs=1e-6)

    def test_very_weak_dollar_clamped(self, cfg):
        assert dollar_score({"dxy": 50.0}, cfg) == pytest.approx(1.0)

    def test_very_strong_dollar_clamped(self, cfg):
        assert dollar_score({"dxy": 150.0}, cfg) == pytest.approx(-1.0)

    def test_missing_dxy_returns_zero(self, cfg):
        assert dollar_score({}, cfg) == pytest.approx(0.0)

    def test_none_dxy_returns_zero(self, cfg):
        assert dollar_score({"dxy": None}, cfg) == pytest.approx(0.0)

    def test_score_monotone_decreasing(self, cfg):
        """Higher DXY → lower score (strong dollar = bearish)."""
        scores = [dollar_score({"dxy": v}, cfg) for v in [80, 90, 95, 100, 105, 110, 120]]
        for a, b in zip(scores, scores[1:]):
            assert a >= b


# ──────────────────────────────────────────────────────────────────────────────
# commodity_score
# ──────────────────────────────────────────────────────────────────────────────


class TestCommodityScore:
    def test_risk_on_ratio_bullish(self, cfg):
        """Ratio at or below risk_on_below → +1.0."""
        ct = cfg["commodity_thresholds"]
        ind = {"gold_copper_ratio": ct["gold_copper_ratio_risk_on_below"]}
        assert commodity_score(ind, cfg) == pytest.approx(1.0, abs=1e-6)

    def test_risk_off_ratio_bearish(self, cfg):
        """Ratio at or above risk_off_above → -1.0."""
        ct = cfg["commodity_thresholds"]
        ind = {"gold_copper_ratio": ct["gold_copper_ratio_risk_off_above"]}
        assert commodity_score(ind, cfg) == pytest.approx(-1.0, abs=1e-6)

    def test_midpoint_neutral(self, cfg):
        ct = cfg["commodity_thresholds"]
        mid = (ct["gold_copper_ratio_risk_on_below"] + ct["gold_copper_ratio_risk_off_above"]) / 2
        ind = {"gold_copper_ratio": mid}
        assert commodity_score(ind, cfg) == pytest.approx(0.0, abs=1e-6)

    def test_very_low_ratio_clamped(self, cfg):
        """Ratio far below threshold → clamped at +1.0."""
        assert commodity_score({"gold_copper_ratio": 1.0}, cfg) == pytest.approx(1.0)

    def test_very_high_ratio_clamped(self, cfg):
        """Ratio far above threshold → clamped at -1.0."""
        assert commodity_score({"gold_copper_ratio": 800.0}, cfg) == pytest.approx(-1.0)

    def test_missing_ratio_returns_zero(self, cfg):
        assert commodity_score({}, cfg) == pytest.approx(0.0)

    def test_none_ratio_returns_zero(self, cfg):
        assert commodity_score({"gold_copper_ratio": None}, cfg) == pytest.approx(0.0)

    def test_score_monotone_decreasing(self, cfg):
        """Higher gold/copper ratio → lower score (more risk-off)."""
        # Values span the corrected scale: risk_on_below=400, risk_off_above=650
        scores = [commodity_score({"gold_copper_ratio": v}, cfg) for v in [300, 400, 500, 600, 700, 800]]
        for a, b in zip(scores, scores[1:]):
            assert a >= b

    def test_bull_inputs_positive(self, cfg, bull_indicators):
        assert commodity_score(bull_indicators, cfg) > 0.0

    def test_bear_inputs_negative(self, cfg, bear_indicators):
        assert commodity_score(bear_indicators, cfg) < 0.0


# ──────────────────────────────────────────────────────────────────────────────
# compute_all_scores
# ──────────────────────────────────────────────────────────────────────────────


class TestComputeAllScores:
    # --- Output structure ---------------------------------------------------

    def test_returns_dict(self, cfg, bull_indicators):
        scores = compute_all_scores(bull_indicators, cfg)
        assert isinstance(scores, dict)

    def test_has_all_keys(self, cfg, bull_indicators):
        scores = compute_all_scores(bull_indicators, cfg)
        expected_keys = {
            "trend", "risk", "credit", "yield_curve",
            "dollar", "commodity", "composite", "available_weight",
        }
        assert set(scores.keys()) == expected_keys

    def test_all_values_are_floats(self, cfg, bull_indicators):
        scores = compute_all_scores(bull_indicators, cfg)
        for key, val in scores.items():
            assert isinstance(val, float), f"Score '{key}' is not float: {val!r}"

    # --- Range checks -------------------------------------------------------

    @pytest.mark.parametrize("key", ["trend", "risk", "credit", "yield_curve", "dollar", "commodity", "composite"])
    def test_all_scores_clamped_to_range_bull(self, cfg, bull_indicators, key):
        score = compute_all_scores(bull_indicators, cfg)[key]
        assert -1.0 <= score <= 1.0, f"{key} out of range: {score}"

    @pytest.mark.parametrize("key", ["trend", "risk", "credit", "yield_curve", "dollar", "commodity", "composite"])
    def test_all_scores_clamped_to_range_bear(self, cfg, bear_indicators, key):
        score = compute_all_scores(bear_indicators, cfg)[key]
        assert -1.0 <= score <= 1.0, f"{key} out of range: {score}"

    # --- Directional sanity -------------------------------------------------

    def test_bull_composite_positive(self, cfg, bull_indicators):
        scores = compute_all_scores(bull_indicators, cfg)
        assert scores["composite"] > 0.3, (
            f"Expected composite > 0.3 for bull market, got {scores['composite']}"
        )

    def test_bear_composite_negative(self, cfg, bear_indicators):
        scores = compute_all_scores(bear_indicators, cfg)
        assert scores["composite"] < -0.3, (
            f"Expected composite < -0.3 for bear market, got {scores['composite']}"
        )

    # --- Composite arithmetic -----------------------------------------------

    def test_composite_matches_manual_calculation(self, cfg, bull_indicators):
        """Composite = weighted sum of individual scores (weights from config)."""
        scores = compute_all_scores(bull_indicators, cfg)
        w = cfg["weights"]
        expected = (
            float(w["trend"])       * scores["trend"]
            + float(w["risk"])        * scores["risk"]
            + float(w["credit"])      * scores["credit"]
            + float(w["yield_curve"]) * scores["yield_curve"]
            + float(w["dollar"])      * scores["dollar"]
            + float(w["commodity"])   * scores["commodity"]
        )
        # The composite is clamped, so compare pre-clamp value
        assert scores["composite"] == pytest.approx(
            max(-1.0, min(1.0, expected)), abs=1e-9
        )

    def test_individual_scores_equal_direct_calls(self, cfg, bull_indicators):
        """compute_all_scores must return the same values as calling each fn directly."""
        scores = compute_all_scores(bull_indicators, cfg)
        assert scores["trend"]       == pytest.approx(trend_score(bull_indicators, cfg))
        assert scores["risk"]        == pytest.approx(risk_score(bull_indicators, cfg))
        assert scores["credit"]      == pytest.approx(credit_score(bull_indicators, cfg))
        assert scores["yield_curve"] == pytest.approx(yield_curve_score(bull_indicators, cfg))
        assert scores["dollar"]      == pytest.approx(dollar_score(bull_indicators, cfg))
        assert scores["commodity"]   == pytest.approx(commodity_score(bull_indicators, cfg))

    # --- available_weight ---------------------------------------------------

    def test_available_weight_is_one_with_full_data(self, cfg, bull_indicators):
        """All indicators present → available_weight = 1.0 (all weights active)."""
        scores = compute_all_scores(bull_indicators, cfg)
        assert scores["available_weight"] == pytest.approx(1.0, abs=1e-9)

    def test_available_weight_partial_missing(self, cfg):
        """
        With credit_oas=None and dxy=None the credit (0.20) and dollar (0.05)
        dimensions are excluded.  available_weight should equal 0.75.
        """
        partial = {
            "spy_above_200dma": 1,
            "spy_200dma_slope": 0.05,
            "vix": 15,
            "yield_curve_10y2y": 1.5,
            "gold_copper_ratio": 350,
            # credit_oas and dxy intentionally absent
        }
        scores = compute_all_scores(partial, cfg)
        expected_weight = (
            float(cfg["weights"]["trend"])
            + float(cfg["weights"]["risk"])
            + float(cfg["weights"]["yield_curve"])
            + float(cfg["weights"]["commodity"])
        )
        assert scores["available_weight"] == pytest.approx(expected_weight, abs=1e-9)

    def test_renormalization_excludes_missing_dimensions(self, cfg):
        """
        When credit and dollar data are missing, their weights (0.20 + 0.05 = 0.25)
        are excluded and the composite is re-normalised over the remaining 0.75.
        The renormalised composite must differ from a naive weighted sum that
        would include the 0.25 dead weight.
        """
        partial = {
            "spy_above_200dma": 1,
            "spy_200dma_slope": 0.05,
            "vix": 15,
            "yield_curve_10y2y": 1.5,
            "gold_copper_ratio": 350,
        }
        scores = compute_all_scores(partial, cfg)
        w = cfg["weights"]

        # Naive sum (old behaviour: missing → 0.0 contributes dead weight)
        naive = (
            float(w["trend"])       * scores["trend"]
            + float(w["risk"])        * scores["risk"]
            + float(w["credit"])      * 0.0   # missing → neutral
            + float(w["yield_curve"]) * scores["yield_curve"]
            + float(w["dollar"])      * 0.0   # missing → neutral
            + float(w["commodity"])   * scores["commodity"]
        )
        # Renormalised composite must be strictly larger (more bullish)
        # because the 0.25 dead weight pulled naive toward 0.
        assert scores["composite"] > naive, (
            f"Expected renormalised composite {scores['composite']:.4f} "
            f"> naive {naive:.4f}"
        )

    # --- Missing data handling ----------------------------------------------

    def test_empty_indicators_returns_all_zeros(self, cfg):
        """All missing data → all scores 0.0 (neutral)."""
        scores = compute_all_scores({}, cfg)
        for key, val in scores.items():
            assert val == pytest.approx(0.0), f"Expected 0.0 for missing {key!r}, got {val}"

    def test_partial_indicators_does_not_raise(self, cfg):
        """Partial indicator dicts must not raise exceptions."""
        partial = {"vix": 18, "credit_oas": 1.2}
        scores = compute_all_scores(partial, cfg)
        assert "composite" in scores

    def test_none_values_do_not_raise(self, cfg):
        """All-None dict must not raise and must return 0.0 everywhere."""
        all_none = {
            "spy_above_200dma": None,
            "spy_200dma_slope": None,
            "vix": None,
            "vix_term_ratio": None,
            "credit_oas": None,
            "yield_curve_10y2y": None,
            "yield_curve_10y3m": None,
            "dxy": None,
            "gold_copper_ratio": None,
        }
        scores = compute_all_scores(all_none, cfg)
        for key, val in scores.items():
            assert val == pytest.approx(0.0), f"{key} != 0.0: {val}"

    def test_nan_values_do_not_raise(self, cfg):
        """NaN values must be treated as missing → 0.0."""
        nan_row = {
            "spy_above_200dma": float("nan"),
            "vix": float("nan"),
            "credit_oas": float("nan"),
        }
        scores = compute_all_scores(nan_row, cfg)
        assert "composite" in scores
        assert -1.0 <= scores["composite"] <= 1.0

    # --- Monotonicity -------------------------------------------------------

    def test_more_bullish_indicators_give_higher_composite(self, cfg):
        """
        With the same set of available indicators, a more-bullish input set must
        produce a higher composite than a neutral one.

        NOTE: The renormalization logic means that *adding new indicator
        dimensions* (changing which fields are present) can change the
        available_weight and therefore the composite in unexpected directions.
        This test keeps available dimensions constant; only the values change.
        """
        # Both dicts have the same four available dimensions so renormalization
        # is identical; only the values differ.
        neutral = {
            "spy_above_200dma": 1,
            "spy_200dma_slope": 0.0,
            "vix": 25,               # moderate — near neutral risk score
            "vix_term_ratio": 1.0,
            "credit_oas": 1.75,      # midpoint between oas_normal/crisis → ~0.0
            "gold_copper_ratio": 525,  # midpoint 400-650 → ~0.0
        }
        bullish = {
            "spy_above_200dma": 1,
            "spy_200dma_slope": 0.05,
            "vix": 15,               # low VIX → strongly bullish
            "vix_term_ratio": 0.88,
            "credit_oas": 0.9,       # tight spreads → bullish
            "gold_copper_ratio": 350,  # below risk-on threshold → bullish
        }
        s_neutral = compute_all_scores(neutral, cfg)
        s_bullish = compute_all_scores(bullish, cfg)
        assert s_bullish["composite"] > s_neutral["composite"], (
            f"bullish composite {s_bullish['composite']:.4f} should exceed "
            f"neutral {s_neutral['composite']:.4f}"
        )

    # --- Extreme-value saturation -------------------------------------------

    def test_extreme_bull_composite_near_one(self, cfg):
        extreme_bull = {
            "spy_above_200dma": 1,
            "spy_200dma_slope": 5.0,
            "vix": 5,
            "vix_term_ratio": 0.5,
            "credit_oas": 0.1,
            "yield_curve_10y2y": 5.0,
            "yield_curve_10y3m": 5.0,
            "dxy": 70,
            "gold_copper_ratio": 5,
        }
        score = compute_all_scores(extreme_bull, cfg)["composite"]
        assert score > 0.7, f"Extreme bull should have composite > 0.7, got {score}"

    def test_extreme_bear_composite_near_minus_one(self, cfg):
        extreme_bear = {
            "spy_above_200dma": 0,
            "spy_200dma_slope": -5.0,
            "vix": 80,
            "vix_term_ratio": 2.0,
            "credit_oas": 10.0,
            "yield_curve_10y2y": -5.0,
            "yield_curve_10y3m": -5.0,
            "dxy": 130,
            "gold_copper_ratio": 50,
        }
        score = compute_all_scores(extreme_bear, cfg)["composite"]
        assert score < -0.7, f"Extreme bear should have composite < -0.7, got {score}"
