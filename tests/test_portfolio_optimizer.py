"""Tests for research/portfolio_optimizer.py — WP-3.1 additions.

Tests cover:
  - compute_optimal_weights_mv()  — mean-variance (SLSQP Sharpe maximisation)
  - cluster_strategies()          — union-find correlation clustering
  - compute_optimal_weights()     — existing Sharpe-tilted inverse-vol (regression)
  - MV fallback behaviour         — bad covariance → falls back gracefully
"""

import numpy as np
import pandas as pd
import pytest

from research.portfolio_optimizer import (
    PortfolioOptimizer,
    cluster_strategies,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_returns(
    n_strategies: int = 4,
    n_days: int = 500,
    seed: int = 42,
    strategy_names: list | None = None,
) -> pd.DataFrame:
    """Synthetic daily returns for a set of strategies."""
    rng = np.random.default_rng(seed)
    names = strategy_names or [f"strat_{i}" for i in range(n_strategies)]
    data = {}
    for name in names:
        data[name] = rng.normal(0.001, 0.02, n_days)
    dates = pd.date_range("2020-01-01", periods=n_days, freq="B")
    return pd.DataFrame(data, index=dates)


def _make_metrics(strategies: list[str], sharpe: float = 1.2, trades: int = 100) -> dict:
    """Uniform metrics dict for a list of strategies."""
    return {
        s: {
            "sharpe": sharpe,
            "total_trades": trades,
            "cagr": 0.15,
            "max_drawdown": 0.10,
            "sortino": 1.5,
            "win_rate": 0.55,
            "profit_factor": 1.4,
            "total_pnl": 5000.0,
            "final_equity": 15000.0,
            "calmar": 1.5,
        }
        for s in strategies
    }


def _make_optimizer(
    min_weight: float = 0.05,
    max_weight: float = 0.40,
    method: str = "mean_variance",
) -> PortfolioOptimizer:
    """Build a PortfolioOptimizer without touching the filesystem.

    Uses __new__ to bypass __init__ and injects the required attributes
    directly, matching what __init__ would set.
    """
    opt = PortfolioOptimizer.__new__(PortfolioOptimizer)
    opt.market = "sp500"
    opt.strategies = None
    opt.min_sharpe = 0.0
    opt.min_trades = 15
    opt.max_weight = max_weight
    opt.min_weight = min_weight
    opt.max_workers = 4
    opt.config = {
        "market": "sp500",
        "risk": {"starting_equity": 10_000, "max_open_positions": 10},
        "fees": {"commission_pct": 0.0},
        "portfolio_optimizer": {
            "method": method,
            "min_weight": min_weight,
            "max_weight": max_weight,
            "cluster_threshold": 0.7,
        },
    }
    return opt


def _make_cov(returns_df: pd.DataFrame) -> np.ndarray:
    """Simple sample covariance (avoids sklearn dependency in tests)."""
    return returns_df.cov().values


# ---------------------------------------------------------------------------
# Tests: compute_optimal_weights_mv
# ---------------------------------------------------------------------------


class TestComputeOptimalWeightsMV:
    """Mean-variance optimization via SLSQP."""

    def test_weights_sum_to_one(self):
        strategies = ["momentum_breakout", "mean_reversion", "connors_rsi2", "opening_gap"]
        returns_df = _make_returns(strategy_names=strategies)
        metrics = _make_metrics(strategies)
        cov = _make_cov(returns_df)
        opt = _make_optimizer()

        weights = opt.compute_optimal_weights_mv(returns_df, metrics, cov)

        total = sum(weights.values())
        assert abs(total - 1.0) < 1e-4, f"Weights sum={total:.6f}, expected 1.0"

    def test_all_weights_in_bounds(self):
        strategies = ["momentum_breakout", "mean_reversion", "connors_rsi2", "opening_gap"]
        returns_df = _make_returns(strategy_names=strategies)
        metrics = _make_metrics(strategies)
        cov = _make_cov(returns_df)
        opt = _make_optimizer(min_weight=0.05, max_weight=0.40)

        weights = opt.compute_optimal_weights_mv(returns_df, metrics, cov)

        for s, w in weights.items():
            assert w >= 0.05 - 1e-5, f"{s}: weight={w:.4f} < min=0.05"
            assert w <= 0.40 + 1e-5, f"{s}: weight={w:.4f} > max=0.40"

    def test_returns_all_strategy_names(self):
        strategies = ["momentum_breakout", "mean_reversion", "connors_rsi2", "opening_gap"]
        returns_df = _make_returns(strategy_names=strategies)
        metrics = _make_metrics(strategies)
        cov = _make_cov(returns_df)
        opt = _make_optimizer()

        weights = opt.compute_optimal_weights_mv(returns_df, metrics, cov)

        assert set(weights.keys()) == set(strategies)

    def test_two_strategies(self):
        """Works with the minimum viable portfolio (n=2)."""
        strategies = ["strat_a", "strat_b"]
        returns_df = _make_returns(n_strategies=2, strategy_names=strategies)
        metrics = _make_metrics(strategies)
        cov = _make_cov(returns_df)
        opt = _make_optimizer(min_weight=0.05, max_weight=0.95)

        weights = opt.compute_optimal_weights_mv(returns_df, metrics, cov)

        assert abs(sum(weights.values()) - 1.0) < 1e-4
        for w in weights.values():
            assert 0.05 - 1e-5 <= w <= 0.95 + 1e-5

    def test_mv_tilts_toward_higher_return_uncorrelated(self):
        """With equal variance and near-zero correlation, MV tilts toward higher-return strategy.

        Constructs sin/cos returns (orthogonal → ρ ≈ 0, equal variance) so that the
        only difference between the two strategies is their mean return.  The MV
        tangency portfolio for uncorrelated equal-variance assets assigns weight
        proportional to μ, so strat_good (μ=0.010) should dominate strat_poor (μ=0.001).
        """
        n = 600
        dates = pd.date_range("2020-01-01", periods=n, freq="B")
        # sin and cos over an integer number of cycles → exactly orthogonal samples
        period = 20  # samples per cycle; 600 / 20 = 30 complete cycles
        t = 2 * np.pi * np.arange(n) / period
        returns_df = pd.DataFrame(
            {
                "strat_good": np.sin(t) * 0.02 + 0.010,
                "strat_poor": np.cos(t) * 0.02 + 0.001,
            },
            index=dates,
        )
        # Sanity-check near-zero correlation before asserting on weights
        actual_corr = float(returns_df.corr().loc["strat_good", "strat_poor"])
        assert abs(actual_corr) < 0.05, f"Returns too correlated: ρ={actual_corr:.4f}"

        metrics = _make_metrics(["strat_good", "strat_poor"])
        cov = _make_cov(returns_df)
        opt = _make_optimizer(min_weight=0.05, max_weight=0.95)

        weights = opt.compute_optimal_weights_mv(returns_df, metrics, cov)

        assert weights["strat_good"] > weights["strat_poor"], (
            f"Expected strat_good(μ=0.010) to outweigh strat_poor(μ=0.001): "
            f"{weights['strat_good']:.4f} vs {weights['strat_poor']:.4f}"
        )


# ---------------------------------------------------------------------------
# Tests: cluster_strategies
# ---------------------------------------------------------------------------


class TestClusterStrategies:
    """Union-find correlation clustering."""

    def _make_corr(self, strategies: list[str], high_group: list[str], threshold: float = 0.94) -> pd.DataFrame:
        """Correlation matrix where members of high_group have corr=0.94, rest=0.05."""
        n = len(strategies)
        corr = pd.DataFrame(np.eye(n), index=strategies, columns=strategies)
        for i, si in enumerate(strategies):
            for j, sj in enumerate(strategies):
                if i == j:
                    continue
                if si in high_group and sj in high_group:
                    corr.loc[si, sj] = threshold
                else:
                    corr.loc[si, sj] = 0.05
        return corr

    def test_high_corr_strategies_cluster_together(self):
        """mean_reversion, connors_rsi2, opening_gap (corr 0.94) form one cluster."""
        strategies = ["momentum_breakout", "mean_reversion", "connors_rsi2", "opening_gap"]
        high_group = ["mean_reversion", "connors_rsi2", "opening_gap"]
        corr = self._make_corr(strategies, high_group, threshold=0.94)

        clusters = cluster_strategies(corr, threshold=0.7)

        # Should have exactly 2 clusters: {MR, CR2, OG} and {MB}
        assert len(clusters) == 2, f"Expected 2 clusters, got {len(clusters)}: {clusters}"

        # Largest cluster first
        large, small = clusters[0], clusters[1]
        assert len(large) == 3, f"Large cluster should have 3 members: {large}"
        assert len(small) == 1, f"Small cluster should have 1 member: {small}"

        # MR/CR2/OG together
        assert sorted(large) == sorted(high_group), (
            f"Expected {sorted(high_group)}, got {sorted(large)}"
        )
        # MB alone
        assert small == ["momentum_breakout"], f"Expected ['momentum_breakout'], got {small}"

    def test_all_uncorrelated_gives_singletons(self):
        strategies = ["a", "b", "c", "d"]
        corr = pd.DataFrame(np.eye(4), index=strategies, columns=strategies)
        # Off-diagonal all 0 (below any reasonable threshold)

        clusters = cluster_strategies(corr, threshold=0.7)

        assert len(clusters) == 4, f"Expected 4 singleton clusters, got {len(clusters)}"
        for c in clusters:
            assert len(c) == 1

    def test_all_correlated_gives_one_cluster(self):
        strategies = ["a", "b", "c"]
        corr = pd.DataFrame(
            [[1.0, 0.9, 0.85], [0.9, 1.0, 0.88], [0.85, 0.88, 1.0]],
            index=strategies,
            columns=strategies,
        )

        clusters = cluster_strategies(corr, threshold=0.7)

        assert len(clusters) == 1
        assert sorted(clusters[0]) == sorted(strategies)

    def test_transitivity(self):
        """If A-B corr > threshold and B-C corr > threshold, A,B,C cluster together
        even if A-C corr is at threshold boundary."""
        strategies = ["a", "b", "c"]
        corr = pd.DataFrame(
            [[1.0, 0.8, 0.65], [0.8, 1.0, 0.8], [0.65, 0.8, 1.0]],
            index=strategies,
            columns=strategies,
        )

        # With threshold=0.7: A-B (0.8>0.7) ✓, B-C (0.8>0.7) ✓, A-C (0.65<0.7) ✗
        # Union-find: A∪B then B∪C → all three in one cluster (transitivity)
        clusters = cluster_strategies(corr, threshold=0.7)

        assert len(clusters) == 1, f"Expected 1 cluster via transitivity, got {clusters}"
        assert sorted(clusters[0]) == ["a", "b", "c"]

    def test_empty_matrix(self):
        corr = pd.DataFrame()
        clusters = cluster_strategies(corr, threshold=0.7)
        assert clusters == []

    def test_single_strategy(self):
        corr = pd.DataFrame([[1.0]], index=["only"], columns=["only"])
        clusters = cluster_strategies(corr, threshold=0.7)
        assert clusters == [["only"]]

    def test_sorted_largest_first(self):
        """Clusters must be returned largest first."""
        strategies = list("abcdefg")
        # a,b,c,d highly correlated; e,f,g moderately correlated (but above threshold)
        n = len(strategies)
        corr_data = np.eye(n)
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                si, sj = strategies[i], strategies[j]
                if si in list("abcd") and sj in list("abcd"):
                    corr_data[i, j] = 0.85
                elif si in list("efg") and sj in list("efg"):
                    corr_data[i, j] = 0.75
                else:
                    corr_data[i, j] = 0.02
        corr = pd.DataFrame(corr_data, index=strategies, columns=strategies)

        clusters = cluster_strategies(corr, threshold=0.7)

        sizes = [len(c) for c in clusters]
        assert sizes == sorted(sizes, reverse=True), (
            f"Clusters not sorted largest-first: {sizes}"
        )
        assert sizes[0] == 4  # abcd
        assert sizes[1] == 3  # efg


# ---------------------------------------------------------------------------
# Tests: existing compute_optimal_weights (regression guard)
# ---------------------------------------------------------------------------


class TestComputeOptimalWeightsRegression:
    """Ensure the legacy Sharpe-tilted inverse-vol method still works correctly."""

    def test_weights_sum_to_one(self):
        strategies = ["momentum_breakout", "mean_reversion", "connors_rsi2", "opening_gap"]
        returns_df = _make_returns(strategy_names=strategies)
        metrics = _make_metrics(strategies)
        cov = _make_cov(returns_df)
        opt = _make_optimizer(method="sharpe_inverse_vol")

        weights = opt.compute_optimal_weights(returns_df, metrics, cov)

        assert abs(sum(weights.values()) - 1.0) < 1e-4

    def test_zero_sharpe_strategies_excluded(self):
        """Strategies with Sharpe below min_sharpe should have weight 0."""
        opt = _make_optimizer()
        opt.min_sharpe = 0.5  # high threshold

        strategies = ["strat_a", "strat_b", "strat_c"]
        returns_df = _make_returns(n_strategies=3, strategy_names=strategies)
        # Only strat_a passes the Sharpe gate
        metrics = {
            "strat_a": {"sharpe": 1.2, "total_trades": 50, "cagr": 0.1, "max_drawdown": 0.1},
            "strat_b": {"sharpe": 0.2, "total_trades": 50, "cagr": 0.05, "max_drawdown": 0.1},
            "strat_c": {"sharpe": 0.1, "total_trades": 50, "cagr": 0.02, "max_drawdown": 0.1},
        }
        cov = _make_cov(returns_df)

        weights = opt.compute_optimal_weights(returns_df, metrics, cov)

        # strat_b and strat_c have sharpe < 0.5 → weight should be 0
        assert weights["strat_b"] == 0.0
        assert weights["strat_c"] == 0.0

    def test_max_weight_cap_enforced(self):
        opt = _make_optimizer(min_weight=0.03, max_weight=0.25)
        strategies = ["a", "b", "c", "d"]
        returns_df = _make_returns(n_strategies=4, strategy_names=strategies)
        metrics = _make_metrics(strategies)
        cov = _make_cov(returns_df)

        weights = opt.compute_optimal_weights(returns_df, metrics, cov)

        for s, w in weights.items():
            assert w <= 0.25 + 1e-6, f"{s}: weight {w:.4f} exceeds max 0.25"


# ---------------------------------------------------------------------------
# Tests: fallback from MV to existing when optimization fails
# ---------------------------------------------------------------------------


class TestMVFallback:
    """When MV optimization cannot converge, it falls back to the legacy method."""

    def test_fallback_on_degenerate_covariance(self):
        """A near-singular cov matrix can cause SLSQP issues; fallback must activate."""
        strategies = ["a", "b", "c"]
        rng = np.random.default_rng(0)
        dates = pd.date_range("2020-01-01", periods=300, freq="B")
        # Near-identical returns → almost singular covariance
        base = rng.normal(0.001, 0.01, 300)
        returns_df = pd.DataFrame(
            {"a": base, "b": base + 1e-10, "c": base + 2e-10},
            index=dates,
        )
        metrics = _make_metrics(strategies)

        # Deliberately near-singular covariance
        cov = np.ones((3, 3)) * 1e-8 + np.eye(3) * 1e-12
        opt = _make_optimizer()

        # Must not raise — should return valid weights
        weights = opt.compute_optimal_weights_mv(returns_df, metrics, cov)

        assert isinstance(weights, dict)
        assert set(weights.keys()) == set(strategies)
        total = sum(weights.values())
        assert abs(total - 1.0) < 1e-3, f"Fallback weights sum={total}"

    def test_fallback_weights_are_valid(self):
        """Fallback to legacy should still pass basic sanity checks."""
        strategies = ["x", "y"]
        returns_df = _make_returns(n_strategies=2, strategy_names=strategies)
        metrics = _make_metrics(strategies)

        # Provide an all-zero covariance to force a bad gradient
        cov = np.zeros((2, 2))
        opt = _make_optimizer(min_weight=0.05, max_weight=0.95)

        weights = opt.compute_optimal_weights_mv(returns_df, metrics, cov)

        assert isinstance(weights, dict)
        assert abs(sum(weights.values()) - 1.0) < 1e-3
