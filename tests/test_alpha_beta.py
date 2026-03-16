"""Tests for calc_alpha_beta in backtest/metrics.py.

Covers:
- Identical returns  → alpha≈0, beta≈1, R²≈1
- Constant-shifted returns → alpha ≈ shift * 252 (annualized)
- Uncorrelated returns → beta≈0, R²≈0
- Short series (<30 days) → graceful zero result
- None inputs → graceful zero result
- All expected keys are present in result
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from backtest.metrics import calc_alpha_beta, calc_all_metrics


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_returns(
    n: int = 252,
    seed: int = 42,
    mean: float = 0.0,
    std: float = 0.01,
) -> pd.Series:
    """Create a synthetic daily-returns Series with a business-day index."""
    rng = np.random.RandomState(seed)
    values = rng.normal(loc=mean, scale=std, size=n)
    return pd.Series(
        values,
        index=pd.date_range("2020-01-01", periods=n, freq="B"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAlphaBetaIdentical:
    """Strategy returns that are identical to the benchmark."""

    def test_alpha_is_zero(self):
        bench = _make_returns(n=252)
        result = calc_alpha_beta(bench, bench)
        assert abs(result["alpha"]) < 1e-6, f"alpha should be ~0, got {result['alpha']}"

    def test_beta_is_one(self):
        bench = _make_returns(n=252)
        result = calc_alpha_beta(bench, bench)
        assert abs(result["beta"] - 1.0) < 1e-6, f"beta should be ~1, got {result['beta']}"

    def test_r_squared_is_one(self):
        bench = _make_returns(n=252)
        result = calc_alpha_beta(bench, bench)
        assert abs(result["r_squared"] - 1.0) < 1e-6, f"R² should be ~1, got {result['r_squared']}"

    def test_correlation_is_one(self):
        bench = _make_returns(n=252)
        result = calc_alpha_beta(bench, bench)
        assert abs(result["correlation"] - 1.0) < 1e-6, f"correlation should be ~1, got {result['correlation']}"

    def test_up_down_capture_are_one(self):
        bench = _make_returns(n=252)
        result = calc_alpha_beta(bench, bench)
        assert abs(result["up_capture"] - 1.0) < 1e-4
        assert abs(result["down_capture"] - 1.0) < 1e-4


class TestAlphaBetaShiftedReturns:
    """Strategy = benchmark + constant daily shift."""

    def test_alpha_annualizes_daily_shift(self):
        """Adding 0.1%/day extra return → annualized alpha ≈ 0.252 (25.2%)."""
        bench = _make_returns(n=500, seed=42)
        daily_shift = 0.001  # 0.1 % per day
        strat = bench + daily_shift

        result = calc_alpha_beta(strat, bench, risk_free_rate=0.0)

        expected_alpha = daily_shift * 252  # ≈ 0.252
        assert abs(result["alpha"] - expected_alpha) < 0.005, (
            f"alpha should be ~{expected_alpha:.4f}, got {result['alpha']:.4f}"
        )

    def test_beta_still_one_with_shift(self):
        """Constant offset doesn't change beta."""
        bench = _make_returns(n=500, seed=42)
        strat = bench + 0.002
        result = calc_alpha_beta(strat, bench)
        assert abs(result["beta"] - 1.0) < 1e-4

    def test_r_squared_still_one_with_shift(self):
        """Constant offset doesn't reduce R²."""
        bench = _make_returns(n=500, seed=42)
        strat = bench + 0.002
        result = calc_alpha_beta(strat, bench)
        assert abs(result["r_squared"] - 1.0) < 1e-4


class TestAlphaBetaUncorrelated:
    """Strategy and benchmark are uncorrelated."""

    def test_beta_near_zero(self):
        rng = np.random.RandomState(0)
        n = 500
        idx = pd.date_range("2020-01-01", periods=n, freq="B")
        bench = pd.Series(rng.normal(0, 0.01, n), index=idx)
        strat = pd.Series(rng.normal(0, 0.01, n), index=idx)

        result = calc_alpha_beta(strat, bench)
        assert abs(result["beta"]) < 0.25, (
            f"beta should be near 0 for uncorrelated series, got {result['beta']}"
        )

    def test_r_squared_near_zero(self):
        rng = np.random.RandomState(0)
        n = 500
        idx = pd.date_range("2020-01-01", periods=n, freq="B")
        bench = pd.Series(rng.normal(0, 0.01, n), index=idx)
        strat = pd.Series(rng.normal(0, 0.01, n), index=idx)

        result = calc_alpha_beta(strat, bench)
        assert result["r_squared"] < 0.15, (
            f"R² should be near 0 for uncorrelated series, got {result['r_squared']}"
        )


class TestAlphaBetaEdgeCases:
    """Edge cases: short series, None inputs, missing overlap."""

    def test_short_series_returns_zeros(self):
        """Series shorter than 30 days → all zeros."""
        bench = _make_returns(n=20)
        strat = _make_returns(n=20, seed=99)
        result = calc_alpha_beta(strat, bench)
        assert result["alpha"] == 0.0
        assert result["beta"] == 0.0
        assert result["r_squared"] == 0.0
        assert result["information_ratio"] == 0.0
        assert result["tracking_error"] == 0.0

    def test_exactly_29_days_returns_zeros(self):
        bench = _make_returns(n=29)
        strat = _make_returns(n=29, seed=7)
        result = calc_alpha_beta(strat, bench)
        assert result["beta"] == 0.0

    def test_exactly_30_days_computes(self):
        bench = _make_returns(n=30)
        strat = _make_returns(n=30, seed=7)
        result = calc_alpha_beta(strat, bench)
        # Should compute something (not all zeros for non-identical series)
        assert "beta" in result

    def test_none_strategy_returns_zeros(self):
        bench = _make_returns(n=100)
        result = calc_alpha_beta(None, bench)
        assert result["beta"] == 0.0
        assert result["alpha"] == 0.0

    def test_none_benchmark_returns_zeros(self):
        strat = _make_returns(n=100)
        result = calc_alpha_beta(strat, None)
        assert result["beta"] == 0.0
        assert result["alpha"] == 0.0

    def test_non_overlapping_indexes_returns_zeros(self):
        """No overlapping dates → zero result."""
        bench = pd.Series(
            [0.01] * 50,
            index=pd.date_range("2020-01-01", periods=50, freq="B"),
        )
        strat = pd.Series(
            [0.01] * 50,
            index=pd.date_range("2022-01-01", periods=50, freq="B"),
        )
        result = calc_alpha_beta(strat, bench)
        assert result["beta"] == 0.0

    def test_all_expected_keys_present(self):
        bench = _make_returns(n=100)
        strat = _make_returns(n=100, seed=77)
        result = calc_alpha_beta(strat, bench)
        expected = {
            "alpha", "beta", "r_squared", "information_ratio",
            "tracking_error", "treynor_ratio", "correlation",
            "up_capture", "down_capture",
        }
        missing = expected - result.keys()
        assert not missing, f"Missing keys: {missing}"


class TestAlphaBetaInCalcAllMetrics:
    """Integration: benchmark_returns wired through calc_all_metrics."""

    def _make_equity(self, n=252, start=10_000.0, seed=1):
        rng = np.random.RandomState(seed)
        daily_r = rng.normal(0.0005, 0.01, n)
        eq = start * np.cumprod(1 + daily_r)
        return pd.Series(eq, index=pd.date_range("2020-01-01", periods=n, freq="B"))

    def test_alpha_beta_keys_present_when_benchmark_provided(self):
        eq = self._make_equity()
        returns = eq.pct_change().dropna()
        bench = _make_returns(n=len(returns), seed=5)
        bench.index = returns.index  # align indexes

        trades = [{"pnl": 100, "strategy": "test"}] * 5
        metrics = calc_all_metrics(eq, trades, benchmark_returns=bench)

        assert "alpha" in metrics
        assert "beta" in metrics
        assert "r_squared" in metrics
        assert "information_ratio" in metrics

    def test_alpha_beta_keys_absent_without_benchmark(self):
        eq = self._make_equity()
        trades = [{"pnl": 100, "strategy": "test"}] * 5
        metrics = calc_all_metrics(eq, trades)

        assert "alpha" not in metrics
        assert "beta" not in metrics
