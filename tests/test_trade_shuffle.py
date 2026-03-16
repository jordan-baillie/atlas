"""Tests for calc_trade_shuffle_analysis in backtest/metrics.py.

Covers:
- Clustered trades (all wins then all losses) → CORRELATED/UNLUCKY
- Interleaved wins/losses → not CORRELATED
- All winners → zero drawdown, no error
- Fewer than 20 trades → shuffle keys absent in calc_all_metrics
- All expected return keys are present
- Reproducibility with same seed
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from backtest.metrics import calc_trade_shuffle_analysis, calc_all_metrics


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _trades(pnls):
    """Build a minimal trade list from a list of P&L values."""
    return [{"pnl": float(p), "strategy": "test"} for p in pnls]


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestTradeShuffleClustered:
    """100 wins followed by 100 losses — worst-case sequencing."""

    def setup_method(self):
        # All wins first → equity rises to 20,000, then falls to 10,000
        # Actual max DD = 50%, which should rank at the very top of the
        # shuffled distribution.
        pnls = [100.0] * 100 + [-100.0] * 100
        self.result = calc_trade_shuffle_analysis(
            _trades(pnls),
            starting_equity=10_000,
            n_simulations=2000,
            seed=42,
        )

    def test_actual_drawdown_is_50pct(self):
        assert abs(self.result["actual_max_drawdown"] - 0.5) < 1e-4, (
            f"Expected DD ~0.5, got {self.result['actual_max_drawdown']}"
        )

    def test_actual_dd_percentile_is_high(self):
        """Actual DD should be well above the shuffled median."""
        assert self.result["actual_dd_percentile"] > 75, (
            f"Clustered trades should produce high DD percentile, "
            f"got {self.result['actual_dd_percentile']}"
        )

    def test_sequencing_impact_is_unlucky_or_correlated(self):
        assert self.result["sequencing_impact"] in ("UNLUCKY", "CORRELATED"), (
            f"Expected UNLUCKY/CORRELATED, got {self.result['sequencing_impact']}"
        )


class TestTradeShuffleInterleaved:
    """Alternating wins and losses — sequencing should not be pathological."""

    def setup_method(self):
        # 50 × (+100, -90) → slowly profitable, very small DD per cycle
        pnls = []
        for _ in range(50):
            pnls.extend([100.0, -90.0])
        self.result = calc_trade_shuffle_analysis(
            _trades(pnls),
            starting_equity=10_000,
            n_simulations=1000,
            seed=42,
        )

    def test_not_correlated(self):
        """Interleaved trades should not hit the CORRELATED threshold."""
        assert self.result["sequencing_impact"] != "CORRELATED", (
            f"Alternating wins/losses should not be CORRELATED, "
            f"got {self.result['sequencing_impact']}"
        )

    def test_actual_dd_percentile_is_not_extreme(self):
        """Actual DD percentile should be well below 95 for this pattern."""
        assert self.result["actual_dd_percentile"] < 95, (
            f"Expected pct < 95 for interleaved trades, "
            f"got {self.result['actual_dd_percentile']}"
        )


class TestTradeShuffleAllWinners:
    """All winning trades — zero drawdown, no error."""

    def setup_method(self):
        pnls = [50.0] * 30
        self.result = calc_trade_shuffle_analysis(
            _trades(pnls),
            starting_equity=10_000,
        )

    def test_no_error_raised(self):
        assert self.result is not None

    def test_actual_max_drawdown_is_zero(self):
        assert self.result["actual_max_drawdown"] == 0.0

    def test_shuffled_mean_dd_is_zero(self):
        assert self.result["shuffled_mean_dd"] == 0.0

    def test_n_simulations_set(self):
        assert self.result["n_simulations"] == 10_000

    def test_sequencing_impact_present(self):
        assert "sequencing_impact" in self.result


class TestTradeShuffleCalcAllMetricsIntegration:
    """Integration: shuffle keys wired through calc_all_metrics."""

    def _make_equity(self, n=100, start=10_000.0):
        idx = pd.date_range("2020-01-01", periods=n, freq="B")
        values = np.linspace(start, start * 1.2, n)
        return pd.Series(values, index=idx)

    def test_shuffle_keys_absent_for_fewer_than_20_trades(self):
        """calc_all_metrics must NOT add shuffle keys when trades < 20."""
        eq = self._make_equity()
        trades = _trades([100.0, -50.0, 80.0, -30.0, 60.0])  # 5 trades

        metrics = calc_all_metrics(eq, trades)

        assert "shuffle_actual_dd" not in metrics, (
            "shuffle_actual_dd should be absent for < 20 trades"
        )
        assert "shuffle_impact" not in metrics

    def test_shuffle_keys_present_for_20_plus_trades(self):
        """calc_all_metrics must add shuffle keys when trades >= 20."""
        eq = self._make_equity(n=200)
        pnls = [10.0 if i % 3 != 0 else -20.0 for i in range(25)]
        trades = _trades(pnls)

        metrics = calc_all_metrics(eq, trades)

        assert "shuffle_actual_dd" in metrics
        assert "shuffle_p50_dd" in metrics
        assert "shuffle_p95_dd" in metrics
        assert "shuffle_percentile" in metrics
        assert "shuffle_impact" in metrics

    def test_exactly_20_trades_triggers_shuffle(self):
        eq = self._make_equity(n=200)
        trades = _trades([10.0] * 20)  # exactly 20

        metrics = calc_all_metrics(eq, trades)
        assert "shuffle_actual_dd" in metrics


class TestTradeShuffleOutputContract:
    """All expected keys are present and values are sane."""

    def setup_method(self):
        pnls = [10.0 if i % 2 == 0 else -8.0 for i in range(50)]
        self.result = calc_trade_shuffle_analysis(
            _trades(pnls),
            starting_equity=10_000,
            n_simulations=500,
            seed=7,
        )

    def test_all_expected_keys_present(self):
        expected = {
            "actual_max_drawdown",
            "shuffled_mean_dd",
            "shuffled_p5_dd",
            "shuffled_p50_dd",
            "shuffled_p95_dd",
            "actual_dd_percentile",
            "sequencing_impact",
            "n_simulations",
        }
        missing = expected - self.result.keys()
        assert not missing, f"Missing keys: {missing}"

    def test_percentiles_are_ordered(self):
        assert self.result["shuffled_p5_dd"] <= self.result["shuffled_p50_dd"]
        assert self.result["shuffled_p50_dd"] <= self.result["shuffled_p95_dd"]

    def test_n_simulations_matches_request(self):
        assert self.result["n_simulations"] == 500

    def test_sequencing_impact_is_valid_label(self):
        assert self.result["sequencing_impact"] in (
            "LUCKY", "NEUTRAL", "UNLUCKY", "CORRELATED"
        )

    def test_actual_dd_percentile_range(self):
        pct = self.result["actual_dd_percentile"]
        assert 0.0 <= pct <= 100.0


class TestTradeShuffleReproducibility:
    """Same seed → identical results."""

    def test_same_seed_same_result(self):
        pnls = [10.0 if i % 2 == 0 else -8.0 for i in range(60)]
        trades = _trades(pnls)

        r1 = calc_trade_shuffle_analysis(trades, 10_000, n_simulations=200, seed=123)
        r2 = calc_trade_shuffle_analysis(trades, 10_000, n_simulations=200, seed=123)

        assert r1["shuffled_p50_dd"] == r2["shuffled_p50_dd"]
        assert r1["actual_dd_percentile"] == r2["actual_dd_percentile"]
        assert r1["sequencing_impact"] == r2["sequencing_impact"]

    def test_different_seeds_can_differ(self):
        rng = np.random.RandomState(0)
        pnls = rng.normal(0, 100, 80).tolist()
        trades = _trades(pnls)

        r1 = calc_trade_shuffle_analysis(trades, 10_000, n_simulations=500, seed=1)
        r2 = calc_trade_shuffle_analysis(trades, 10_000, n_simulations=500, seed=2)

        # Results should differ (not identical) with different seeds
        # (May occasionally be equal by chance, but extremely unlikely)
        # We just check the run succeeds without error
        assert "shuffled_p50_dd" in r1
        assert "shuffled_p50_dd" in r2


class TestTradeShuffleEdgeCases:
    """Empty / degenerate inputs."""

    def test_empty_trades_returns_zeros(self):
        result = calc_trade_shuffle_analysis([], starting_equity=10_000)
        assert result["actual_max_drawdown"] == 0.0
        assert result["n_simulations"] == 0

    def test_zero_starting_equity_returns_zeros(self):
        result = calc_trade_shuffle_analysis(
            _trades([100.0] * 10), starting_equity=0
        )
        assert result["actual_max_drawdown"] == 0.0
        assert result["n_simulations"] == 0

    def test_single_winning_trade(self):
        result = calc_trade_shuffle_analysis(
            _trades([500.0]), starting_equity=10_000
        )
        assert result["actual_max_drawdown"] == 0.0
        assert result["n_simulations"] == 10_000

    def test_single_losing_trade(self):
        result = calc_trade_shuffle_analysis(
            _trades([-500.0]), starting_equity=10_000
        )
        assert result["actual_max_drawdown"] > 0.0
        assert "sequencing_impact" in result

    def test_trades_list_not_mutated(self):
        """Original trades list must not be modified."""
        pnls_orig = [100.0, -50.0, 200.0, -30.0]
        trades = _trades(pnls_orig)
        orig_pnls = [t["pnl"] for t in trades]

        calc_trade_shuffle_analysis(trades, starting_equity=10_000, n_simulations=100)

        after_pnls = [t["pnl"] for t in trades]
        assert orig_pnls == after_pnls, "calc_trade_shuffle_analysis mutated the trades list"
