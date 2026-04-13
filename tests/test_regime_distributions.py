"""Unit tests for regime.distributions."""
import numpy as np
import pytest

from regime.distributions import RegimeDistributions, MIN_OBSERVATIONS
from regime.states import RegimeState


@pytest.fixture(scope="module")
def fitted():
    rd = RegimeDistributions()
    rd.fit(lookback_years=10)
    return rd


def test_fit_populates_all_six_states(fitted):
    for state in RegimeState:
        assert state.value in fitted._cache


def test_all_regime_stats_returns_six(fitted):
    stats = fitted.all_regime_stats()
    assert len(stats) == 6
    for state in RegimeState:
        assert state.value in stats


def test_stats_keys_present(fitted):
    s = fitted.regime_stats(RegimeState.BULL_RISK_ON.value)
    required = {"mean", "vol", "skew", "kurt", "var_5", "var_1",
                "cvar_5", "cvar_1", "n_samples", "min", "max"}
    assert required.issubset(set(s.keys()))


def test_vol_positive(fitted):
    for state in RegimeState:
        s = fitted.regime_stats(state.value)
        assert s["vol"] > 0, f"{state.value} has non-positive vol"


def test_mean_close_to_historical_drift(fitted):
    s = fitted.regime_stats(RegimeState.BULL_RISK_ON.value)
    # Daily SPY drift is roughly 0.03%-0.05% in bull regimes
    assert -0.01 < s["mean"] < 0.01, f"bull_risk_on mean implausible: {s['mean']}"


def test_sample_returns_shape(fitted):
    samples = fitted.sample_returns(RegimeState.BULL_RISK_ON.value, n=500, seed=42)
    assert samples.shape == (500,)
    assert samples.dtype == np.float64


def test_sample_paths_shape(fitted):
    paths = fitted.sample_paths(
        RegimeState.BULL_RISK_ON.value, n_paths=100, n_days=20, seed=42
    )
    assert paths.shape == (100, 20)


def test_seed_reproducibility(fitted):
    a = fitted.sample_returns(RegimeState.BULL_RISK_ON.value, n=200, seed=123)
    b = fitted.sample_returns(RegimeState.BULL_RISK_ON.value, n=200, seed=123)
    np.testing.assert_array_equal(a, b)


def test_sparse_regime_falls_back(fitted):
    # bear_capitulation has only ~12 observations in real data
    s = fitted.regime_stats(RegimeState.BEAR_CAPITULATION.value)
    assert s["n_samples"] < MIN_OBSERVATIONS
    assert s["fallback"] is True


def test_unknown_regime_raises(fitted):
    with pytest.raises(ValueError):
        fitted.sample_returns("not_a_regime", n=10)


def test_var_less_than_mean(fitted):
    s = fitted.regime_stats(RegimeState.BULL_RISK_ON.value)
    assert s["var_5"] < s["mean"]


def test_cvar_5_le_var_5(fitted):
    s = fitted.regime_stats(RegimeState.BULL_RISK_ON.value)
    assert s["cvar_5"] <= s["var_5"]
