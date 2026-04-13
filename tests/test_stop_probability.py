"""Tests for risk.stop_probability — no DB access; always pass vol_annual explicitly."""
import math
import pytest
from risk.stop_probability import (
    prob_touch_lower,
    prob_touch_upper,
    expected_loss_at_stop,
    analyze_position_stop,
)


# ── prob_touch_lower ──────────────────────────────────────────────────────────

class TestProbTouchLower:
    def test_barrier_at_spot_returns_one(self):
        """barrier == spot → already touched → 1.0"""
        assert prob_touch_lower(100.0, 100.0, 0.3, 10) == pytest.approx(1.0)

    def test_barrier_above_spot_returns_one(self):
        """barrier > spot → already below → 1.0"""
        assert prob_touch_lower(90.0, 100.0, 0.3, 10) == pytest.approx(1.0)

    def test_barrier_far_below_small_prob(self):
        """spot=100, stop=50 (50% away), vol=0.3, 5 days → tiny probability."""
        p = prob_touch_lower(100.0, 50.0, 0.3, 5)
        assert 0.0 < p < 0.01, f"Expected very small prob, got {p:.6f}"

    def test_higher_vol_higher_prob_monotonic(self):
        """Higher vol → higher probability of touching stop."""
        p_low = prob_touch_lower(100.0, 95.0, 0.2, 20)
        p_high = prob_touch_lower(100.0, 95.0, 0.6, 20)
        assert p_high > p_low, f"Expected p(vol=0.6) > p(vol=0.2), got {p_high:.4f} vs {p_low:.4f}"

    def test_longer_horizon_higher_prob_monotonic(self):
        """Longer horizon → more time to touch stop → higher prob."""
        p_1d = prob_touch_lower(100.0, 95.0, 0.3, 1)
        p_20d = prob_touch_lower(100.0, 95.0, 0.3, 20)
        assert p_20d > p_1d, f"Expected p(20d) > p(1d), got {p_20d:.4f} vs {p_1d:.4f}"

    def test_invalid_inputs_zero(self):
        """Any non-positive input → 0.0."""
        assert prob_touch_lower(0.0, 95.0, 0.3, 10) == pytest.approx(0.0)
        assert prob_touch_lower(100.0, 0.0, 0.3, 10) == pytest.approx(0.0)
        assert prob_touch_lower(100.0, 95.0, 0.0, 10) == pytest.approx(0.0)
        assert prob_touch_lower(100.0, 95.0, 0.3, 0) == pytest.approx(0.0)
        assert prob_touch_lower(-1.0, 95.0, 0.3, 10) == pytest.approx(0.0)
        assert prob_touch_lower(100.0, -5.0, 0.3, 10) == pytest.approx(0.0)

    def test_sqrt_T_scaling_monotonic_and_in_range(self):
        """5% stop, vol=0.32: 5d prob > 1d prob, both in [0, 1]."""
        p_1d = prob_touch_lower(100.0, 95.0, 0.32, 1)
        p_5d = prob_touch_lower(100.0, 95.0, 0.32, 5)
        assert 0.0 <= p_1d <= 1.0
        assert 0.0 <= p_5d <= 1.0
        assert p_5d > p_1d

    def test_result_capped_at_one(self):
        """Result never exceeds 1.0 even for extreme inputs."""
        p = prob_touch_lower(100.0, 99.999, 2.0, 252)
        assert p <= 1.0

    def test_close_stop_reasonable_range(self):
        """spot=100, stop=95, vol=0.3, days=20 → should be a meaningful prob (e.g., > 0.01)."""
        p = prob_touch_lower(100.0, 95.0, 0.3, 20)
        assert 0.01 < p < 1.0, f"Expected meaningful prob, got {p:.4f}"


# ── prob_touch_upper ──────────────────────────────────────────────────────────

class TestProbTouchUpper:
    def test_barrier_at_spot_returns_one(self):
        """barrier == spot → already touched → 1.0"""
        assert prob_touch_upper(100.0, 100.0, 0.3, 10) == pytest.approx(1.0)

    def test_barrier_below_spot_returns_one(self):
        """barrier < spot → already exceeded → 1.0"""
        assert prob_touch_upper(105.0, 100.0, 0.3, 10) == pytest.approx(1.0)

    def test_reasonable_inputs_in_range(self):
        """spot=100, barrier=105, vol=0.3, days=10 → (0, 1)."""
        p = prob_touch_upper(100.0, 105.0, 0.3, 10)
        assert 0.0 < p < 1.0, f"Expected (0,1), got {p:.4f}"

    def test_invalid_inputs_zero(self):
        """Any non-positive input → 0.0."""
        assert prob_touch_upper(0.0, 105.0, 0.3, 10) == pytest.approx(0.0)
        assert prob_touch_upper(100.0, 0.0, 0.3, 10) == pytest.approx(0.0)
        assert prob_touch_upper(100.0, 105.0, 0.0, 10) == pytest.approx(0.0)
        assert prob_touch_upper(100.0, 105.0, 0.3, 0) == pytest.approx(0.0)

    def test_higher_barrier_lower_prob(self):
        """Barrier further away → harder to reach → lower prob."""
        p_near = prob_touch_upper(100.0, 103.0, 0.3, 10)
        p_far = prob_touch_upper(100.0, 120.0, 0.3, 10)
        assert p_near > p_far

    def test_symmetry_with_lower(self):
        """
        Upper and lower are symmetric problems:
        prob_touch_upper(100, 105) ≈ prob_touch_lower(100, 100/1.05)
        Both use the same formula internally.
        """
        p_upper = prob_touch_upper(100.0, 105.0, 0.3, 20)
        equivalent_barrier = 100.0 / 1.05
        p_lower = prob_touch_lower(100.0, equivalent_barrier, 0.3, 20)
        # The log-ratio is the same; they should be very close
        assert abs(p_upper - p_lower) < 0.02, (
            f"Expected symmetry: upper={p_upper:.4f} lower_equiv={p_lower:.4f}"
        )


# ── expected_loss_at_stop ─────────────────────────────────────────────────────

class TestExpectedLossAtStop:
    def test_basic_calculation(self):
        """entry=100, stop=95, shares=10, prob=0.5 → expected_loss=25, max_loss=50."""
        result = expected_loss_at_stop(100.0, 95.0, 10, 0.5)
        assert result["max_loss"] == pytest.approx(50.0)
        assert result["expected_loss"] == pytest.approx(25.0)
        assert result["loss_per_share"] == pytest.approx(5.0)

    def test_zero_probability(self):
        """prob=0 → expected_loss=0."""
        result = expected_loss_at_stop(100.0, 95.0, 10, 0.0)
        assert result["expected_loss"] == pytest.approx(0.0)
        assert result["max_loss"] == pytest.approx(50.0)

    def test_full_probability(self):
        """prob=1 → expected_loss == max_loss."""
        result = expected_loss_at_stop(100.0, 95.0, 10, 1.0)
        assert result["expected_loss"] == pytest.approx(result["max_loss"])

    def test_rounding(self):
        """Results rounded to 2 dp (max_loss, expected_loss) and 4 dp (loss_per_share)."""
        result = expected_loss_at_stop(100.123, 95.456, 7, 0.333)
        assert isinstance(result["loss_per_share"], float)
        assert isinstance(result["max_loss"], float)
        assert isinstance(result["expected_loss"], float)
        # Spot-check rounding: loss_per_share = 100.123 - 95.456 = 4.667
        assert result["loss_per_share"] == pytest.approx(4.667, abs=0.0001)


# ── analyze_position_stop ─────────────────────────────────────────────────────

class TestAnalyzePositionStop:
    """No DB access — always pass vol_annual explicitly."""

    BASE = dict(ticker="TEST", spot=100.0, stop=95.0, vol_annual=0.30)

    def test_returns_all_four_horizons(self):
        result = analyze_position_stop(**self.BASE, horizons=(1, 5, 10, 20))
        assert set(result["horizons"].keys()) == {"1d", "5d", "10d", "20d"}

    def test_horizons_monotonically_increasing(self):
        """Longer horizon → higher prob_touch."""
        result = analyze_position_stop(**self.BASE, horizons=(1, 5, 10, 20))
        probs = [result["horizons"][f"{d}d"]["prob_touch"] for d in (1, 5, 10, 20)]
        for i in range(len(probs) - 1):
            assert probs[i+1] >= probs[i], f"Not monotonic at index {i}: {probs}"

    def test_stop_distance_pct_correct(self):
        """stop_distance_pct = |spot - stop| / spot = 5/100 = 0.05."""
        result = analyze_position_stop(**self.BASE)
        assert result["stop_distance_pct"] == pytest.approx(0.05, abs=0.0001)

    def test_stop_distance_pct_different_values(self):
        """spot=200, stop=180 → |200-180|/200 = 0.1."""
        result = analyze_position_stop(
            ticker="X", spot=200.0, stop=180.0, vol_annual=0.25
        )
        assert result["stop_distance_pct"] == pytest.approx(0.10, abs=0.0001)

    def test_result_shape(self):
        """Top-level keys present."""
        result = analyze_position_stop(**self.BASE)
        for key in ("ticker", "spot", "stop", "direction", "vol_annual", "stop_distance_pct", "horizons"):
            assert key in result, f"Missing key: {key}"

    def test_horizon_sub_keys(self):
        """Each horizon entry has days, prob_touch, prob_touch_pct."""
        result = analyze_position_stop(**self.BASE, horizons=(5,))
        h = result["horizons"]["5d"]
        assert h["days"] == 5
        assert 0.0 <= h["prob_touch"] <= 1.0
        assert 0.0 <= h["prob_touch_pct"] <= 100.0

    def test_short_direction_uses_upper(self):
        """For short, stop above spot (covering scenario)."""
        result = analyze_position_stop(
            ticker="SHORT", spot=100.0, stop=108.0,
            direction="short", vol_annual=0.30, horizons=(10,)
        )
        p = result["horizons"]["10d"]["prob_touch"]
        assert 0.0 < p < 1.0

    def test_prob_touch_pct_matches_prob_touch(self):
        """prob_touch_pct == prob_touch * 100 (within rounding)."""
        result = analyze_position_stop(**self.BASE, horizons=(10,))
        h = result["horizons"]["10d"]
        assert h["prob_touch_pct"] == pytest.approx(h["prob_touch"] * 100, abs=0.01)

    def test_vol_annual_reflected_in_result(self):
        """vol_annual in result matches what was passed."""
        result = analyze_position_stop(**self.BASE)
        assert result["vol_annual"] == pytest.approx(0.30, abs=0.0001)
