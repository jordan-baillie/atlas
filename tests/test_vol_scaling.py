"""Unit tests for backtest.vol_scaling.VolatilityScaler (Task #124)."""
import math

import numpy as np
import pytest

from backtest.vol_scaling import VolatilityScaler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(enabled=True, lookback=60, half_life=20, target_vol=0.12,
                 conditional=True, percentile_threshold=80) -> dict:
    return {
        "vol_scaling": {
            "enabled": enabled,
            "lookback": lookback,
            "half_life": half_life,
            "target_vol": target_vol,
            "conditional": conditional,
            "percentile_threshold": percentile_threshold,
        }
    }


def _feed_returns(scaler: VolatilityScaler, daily_vol: float, n: int,
                  rng: np.random.Generator = None) -> None:
    """Append ``n`` synthetic returns with the given per-day std."""
    if rng is None:
        rng = np.random.default_rng(42)
    returns = rng.normal(0, daily_vol, n)
    for r in returns:
        scaler.update(float(r))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestVolatilityScalerDisabled:
    """scale_factor() must always return 1.0 when disabled."""

    def test_disabled_returns_one_no_data(self):
        scaler = VolatilityScaler(_make_config(enabled=False))
        assert scaler.scale_factor() == 1.0

    def test_disabled_returns_one_with_data(self):
        scaler = VolatilityScaler(_make_config(enabled=False))
        _feed_returns(scaler, daily_vol=0.03, n=120)
        assert scaler.scale_factor() == 1.0

    def test_disabled_returns_one_regardless_of_high_vol(self):
        scaler = VolatilityScaler(_make_config(enabled=False))
        # Very high volatility — still 1.0 because disabled
        _feed_returns(scaler, daily_vol=0.10, n=120)
        assert scaler.scale_factor() == 1.0


class TestInsufficientData:
    """scale_factor() must return 1.0 until the lookback window is filled."""

    def test_empty_buffer(self):
        scaler = VolatilityScaler(_make_config(enabled=True, lookback=60))
        assert scaler.scale_factor() == 1.0

    def test_partial_buffer_returns_one(self):
        scaler = VolatilityScaler(_make_config(enabled=True, lookback=60))
        _feed_returns(scaler, daily_vol=0.05, n=59)
        assert scaler.scale_factor() == 1.0

    def test_exact_lookback_does_not_return_one_for_high_vol(self):
        """Once lookback is reached with very high vol, scale should be < 1.0."""
        rng = np.random.default_rng(0)
        # unconditional mode so we definitely get scaling
        scaler = VolatilityScaler(
            _make_config(enabled=True, lookback=60, conditional=False,
                         target_vol=0.12)
        )
        # daily vol ~2% → annualized ~32% >> target 12%
        _feed_returns(scaler, daily_vol=0.02, n=60, rng=rng)
        factor = scaler.scale_factor()
        assert factor < 1.0, f"Expected < 1.0 when vol is elevated, got {factor}"


class TestUnconditionalScaling:
    """Without conditional mode, scale = min(1.0, target_vol / realized_vol)."""

    def test_high_vol_produces_scale_below_one(self):
        rng = np.random.default_rng(7)
        scaler = VolatilityScaler(
            _make_config(enabled=True, lookback=60, half_life=20,
                         target_vol=0.12, conditional=False)
        )
        # daily vol ~2% → annualized ~31.7% >> target 12%
        _feed_returns(scaler, daily_vol=0.02, n=120, rng=rng)
        factor = scaler.scale_factor()
        assert factor < 1.0, f"scale should be < 1.0, got {factor}"
        assert factor > 0.0

    def test_scale_bounded_above_by_one(self):
        rng = np.random.default_rng(13)
        scaler = VolatilityScaler(
            _make_config(enabled=True, lookback=60, conditional=False,
                         target_vol=0.50)  # very high target → scale = 1.0
        )
        # low vol data (daily ~0.2%)
        _feed_returns(scaler, daily_vol=0.002, n=120, rng=rng)
        factor = scaler.scale_factor()
        assert factor == pytest.approx(1.0), f"scale should be capped at 1.0, got {factor}"

    def test_scale_is_roughly_target_over_realized(self):
        """Verify the formula: scale ≈ target_vol / realized_vol."""
        rng = np.random.default_rng(99)
        target = 0.12
        scaler = VolatilityScaler(
            _make_config(enabled=True, lookback=60, half_life=20,
                         target_vol=target, conditional=False)
        )
        # Deterministic daily vol ≈ 2%
        returns = [0.02, -0.02] * 60  # alternating → std ≈ 0.02
        for r in returns:
            scaler.update(r)
        factor = scaler.scale_factor()
        ann_vol = 0.02 * math.sqrt(252)  # ≈ 0.317
        expected = min(1.0, target / ann_vol)
        # Allow ±15% relative tolerance (EWMA differs from simple std)
        assert factor == pytest.approx(expected, rel=0.15), (
            f"Expected scale ≈ {expected:.4f}, got {factor:.4f}"
        )


class TestConditionalMode:
    """Conditional mode: only scale when realized vol > Nth percentile."""

    def test_low_vol_returns_one(self):
        """When all returns are small, realized vol is below threshold → 1.0."""
        rng = np.random.default_rng(21)
        scaler = VolatilityScaler(
            _make_config(enabled=True, lookback=60, conditional=True,
                         percentile_threshold=80, target_vol=0.05)
        )
        # Very low vol — daily returns ~0.1%
        _feed_returns(scaler, daily_vol=0.001, n=120, rng=rng)
        factor = scaler.scale_factor()
        assert factor == 1.0, (
            f"Low vol should not trigger scaling in conditional mode, got {factor}"
        )

    def test_high_vol_spike_triggers_scaling(self):
        """After quiet history, a burst of high vol should cross the threshold."""
        rng = np.random.default_rng(55)
        scaler = VolatilityScaler(
            _make_config(enabled=True, lookback=60, conditional=True,
                         percentile_threshold=80, target_vol=0.12)
        )
        # Long quiet history (daily ~0.2%)
        _feed_returns(scaler, daily_vol=0.002, n=200, rng=rng)
        # Now inject 60 days of high-vol returns (daily ~3%)
        _feed_returns(scaler, daily_vol=0.03, n=60, rng=rng)
        factor = scaler.scale_factor()
        assert factor < 1.0, (
            f"High-vol spike after quiet history should trigger scaling, got {factor}"
        )

    def test_conditional_vs_unconditional_agree_at_high_vol(self):
        """Both modes should scale < 1.0 when vol is clearly elevated."""
        rng_c = np.random.default_rng(77)
        rng_u = np.random.default_rng(77)

        cond = VolatilityScaler(
            _make_config(enabled=True, lookback=60, conditional=True,
                         percentile_threshold=50, target_vol=0.12)
        )
        uncond = VolatilityScaler(
            _make_config(enabled=True, lookback=60, conditional=False,
                         target_vol=0.12)
        )
        for scaler, rng in [(cond, rng_c), (uncond, rng_u)]:
            _feed_returns(scaler, daily_vol=0.025, n=120, rng=rng)

        assert cond.scale_factor() < 1.0
        assert uncond.scale_factor() < 1.0

    def test_threshold_boundary(self):
        """Percentile threshold = 0 means always scale (like unconditional)."""
        rng_0 = np.random.default_rng(88)
        rng_u = np.random.default_rng(88)

        cond_zero = VolatilityScaler(
            _make_config(enabled=True, lookback=60, conditional=True,
                         percentile_threshold=0, target_vol=0.12)
        )
        uncond = VolatilityScaler(
            _make_config(enabled=True, lookback=60, conditional=False,
                         target_vol=0.12)
        )
        for scaler, rng in [(cond_zero, rng_0), (uncond, rng_u)]:
            _feed_returns(scaler, daily_vol=0.015, n=120, rng=rng)

        # Both should produce the same (or very similar) scale
        f_cond = cond_zero.scale_factor()
        f_uncond = uncond.scale_factor()
        # With threshold=0, realized vol should always exceed the 0th percentile
        assert f_cond < 1.0, "pct=0 conditional should always trigger scaling"
        assert abs(f_cond - f_uncond) < 0.05, (
            f"Threshold-0 conditional ({f_cond:.4f}) should ≈ unconditional ({f_uncond:.4f})"
        )


class TestUpdateAndBuffer:
    """update() correctly accumulates data; buffer grows unbounded."""

    def test_buffer_grows_with_updates(self):
        scaler = VolatilityScaler(_make_config(enabled=True))
        assert len(scaler._returns) == 0
        scaler.update(0.01)
        scaler.update(-0.005)
        assert len(scaler._returns) == 2

    def test_scale_improves_after_vol_decreases(self):
        """After high vol, feeding quiet returns should raise the scale factor."""
        rng = np.random.default_rng(33)
        scaler = VolatilityScaler(
            _make_config(enabled=True, lookback=60, half_life=20,
                         target_vol=0.12, conditional=False)
        )
        # High vol phase
        _feed_returns(scaler, daily_vol=0.03, n=120, rng=rng)
        high_vol_factor = scaler.scale_factor()

        # Very quiet phase — enough to dominate the lookback window
        _feed_returns(scaler, daily_vol=0.001, n=120, rng=rng)
        low_vol_factor = scaler.scale_factor()

        assert low_vol_factor > high_vol_factor, (
            f"Factor should improve as vol drops. "
            f"high={high_vol_factor:.4f}, low={low_vol_factor:.4f}"
        )
