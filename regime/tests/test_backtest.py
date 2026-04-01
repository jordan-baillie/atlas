"""
regime/tests/test_backtest.py — Unit tests for regime/backtest.py

Run with:
    cd /root/atlas && python3 -m pytest regime/tests/test_backtest.py -v

Coverage
--------
- Regime states are read from regime_history (DB) for each window
- Active universes change based on the current regime state
- sizing_multiplier is correctly applied to risk config parameters
- compare_with_sp500_only returns both results and a delta dict
- Fallback to BULL_RISK_ON when regime_history is empty/unavailable
- Contiguous same-state rows are merged into a single regime window
- run() aggregates trades and equity curves across all regime windows
- Skips windows that have no data or no strategies (graceful degradation)

All heavy operations (BacktestEngine.run_walkforward, data loading) are
mocked — these tests verify logic only and run in milliseconds.
"""
from __future__ import annotations

import copy
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ── Project root on path ──────────────────────────────────────────────────────
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from backtest.engine import BacktestResult
from regime.backtest import RegimeAwareBacktest, RegimeBacktestResult
from regime.states import REGIME_CONFIGS, RegimeState

# ──────────────────────────────────────────────────────────────────────────────
# Test fixtures & shared helpers
# ──────────────────────────────────────────────────────────────────────────────

MOCK_CONFIG: Dict[str, Any] = {
    "market": "sp500",
    "version": "test",
    "data": {"cache_dir": "data/cache"},
    "backtest": {
        "train_window_days": 252,
        "test_window_days": 63,
        "step_days": 21,
        "risk_free_rate": 0.04,
    },
    "risk": {
        "starting_equity": 10_000,
        "max_risk_per_trade_pct": 0.01,
        "max_open_positions": 10,
        "leverage": 1.0,
        "require_stop_loss": False,
        "require_planned_exit": False,
    },
    "fees": {
        "commission_per_trade": 0,
        "commission_pct": 0,
        "slippage_pct": 0.001,
        "slippage_model": "fixed",
        "min_position_value": 100.0,
        "flat_fee_threshold": 0,
    },
    "trading": {"mode": "paper"},
    "strategies": {
        "momentum_breakout": {"enabled": True},
        "mean_reversion": {"enabled": True},
        "trend_following": {"enabled": True},
        "sector_rotation": {"enabled": False},
        "short_term_mr": {"enabled": True},
        "opening_gap": {"enabled": False},
        "connors_rsi2": {"enabled": True},
    },
}

# Fake regime_history rows in **DESC** order (as get_regime_history returns them)
BULL_HISTORY_DESC: List[Dict[str, Any]] = [
    {
        "date": "2021-06-01",
        "regime_state": "bull_risk_on",
        "active_universes": ["sp500", "sector_etfs", "commodity_etfs"],
        "sizing_multiplier": 1.0,
        "enabled_strategies": ["all"],
    },
    {
        "date": "2021-01-04",
        "regime_state": "bull_risk_on",
        "active_universes": ["sp500", "sector_etfs", "commodity_etfs"],
        "sizing_multiplier": 1.0,
        "enabled_strategies": ["all"],
    },
]

# Mixed bull + bear history — DESC order
MULTI_REGIME_DESC: List[Dict[str, Any]] = [
    # Bear window (later)
    {
        "date": "2022-06-01",
        "regime_state": "bear_risk_off",
        "active_universes": ["treasury_etfs", "gold_etfs", "defensive_etfs"],
        "sizing_multiplier": 0.5,
        "enabled_strategies": ["trend_following"],
    },
    {
        "date": "2022-01-03",
        "regime_state": "bear_risk_off",
        "active_universes": ["treasury_etfs", "gold_etfs", "defensive_etfs"],
        "sizing_multiplier": 0.5,
        "enabled_strategies": ["trend_following"],
    },
    # Bull window (earlier)
    {
        "date": "2021-06-01",
        "regime_state": "bull_risk_on",
        "active_universes": ["sp500", "sector_etfs", "commodity_etfs"],
        "sizing_multiplier": 1.0,
        "enabled_strategies": ["all"],
    },
    {
        "date": "2021-01-04",
        "regime_state": "bull_risk_on",
        "active_universes": ["sp500", "sector_etfs", "commodity_etfs"],
        "sizing_multiplier": 1.0,
        "enabled_strategies": ["all"],
    },
]


def _make_backtest_result(
    n_trades: int = 5,
    sharpe: float = 0.8,
    cagr: float = 0.12,
    max_drawdown: float = -0.08,
) -> BacktestResult:
    """Create a lightweight mock BacktestResult for tests."""
    dates = pd.date_range("2021-01-04", periods=100, freq="B")
    equity = pd.Series(
        [10_000 + i * 10 for i in range(100)], index=dates, dtype=float
    )
    trades = [
        {"ticker": "AAPL", "pnl": 50.0, "strategy": "mean_reversion"}
        for _ in range(n_trades)
    ]
    return BacktestResult(
        trades=trades,
        equity_curve=equity,
        metrics={
            "sharpe": sharpe,
            "cagr": cagr,
            "max_drawdown": max_drawdown,
            "win_rate": 0.6,
            "profit_factor": 1.5,
            "sortino": 1.0,
            "calmar": 1.5,
        },
        benchmark_metrics={},
        walk_forward_windows=[],
    )


# ──────────────────────────────────────────────────────────────────────────────
# 1. Regime states are read from regime_history
# ──────────────────────────────────────────────────────────────────────────────


class TestRegimeWindowExtraction:
    """_get_regime_windows() reads and correctly groups regime_history."""

    def test_reads_regime_state_from_history(self):
        """The window regime should match the state from regime_history."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2021-12-31"
        )
        with patch("db.atlas_db.get_regime_history", return_value=BULL_HISTORY_DESC):
            windows = bt._get_regime_windows()

        assert len(windows) == 1
        assert windows[0]["regime"] == "bull_risk_on"

    def test_contiguous_rows_merged_into_one_window(self):
        """Multiple consecutive rows with the same state → single window."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2021-12-31"
        )
        with patch("db.atlas_db.get_regime_history", return_value=BULL_HISTORY_DESC):
            windows = bt._get_regime_windows()

        # Two rows with bull_risk_on → still one window
        assert len(windows) == 1

    def test_regime_transition_creates_two_windows(self):
        """Bull → bear transition should produce two separate windows."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2022-12-31"
        )
        with patch("db.atlas_db.get_regime_history", return_value=MULTI_REGIME_DESC):
            windows = bt._get_regime_windows()

        assert len(windows) == 2
        assert windows[0]["regime"] == "bull_risk_on"
        assert windows[1]["regime"] == "bear_risk_off"

    def test_fallback_to_bull_risk_on_when_history_empty(self):
        """Empty regime_history → single BULL_RISK_ON window for full range."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2021-12-31"
        )
        with patch("db.atlas_db.get_regime_history", return_value=[]):
            windows = bt._get_regime_windows()

        assert len(windows) == 1
        assert windows[0]["regime"] == RegimeState.BULL_RISK_ON.value
        assert windows[0]["start"] == "2021-01-01"
        assert windows[0]["end"] == "2021-12-31"

    def test_fallback_when_db_raises(self):
        """DB exception should not propagate — fallback to BULL_RISK_ON."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2021-12-31"
        )
        with patch(
            "db.atlas_db.get_regime_history",
            side_effect=RuntimeError("DB unavailable"),
        ):
            windows = bt._get_regime_windows()

        assert len(windows) == 1
        assert windows[0]["regime"] == RegimeState.BULL_RISK_ON.value

    def test_date_range_filtering(self):
        """Rows outside [start_date, end_date] should be excluded."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2022-01-01", end_date="2022-12-31"
        )
        with patch("db.atlas_db.get_regime_history", return_value=MULTI_REGIME_DESC):
            windows = bt._get_regime_windows()

        # Only the bear_risk_off rows fall within 2022
        assert all(w["regime"] == "bear_risk_off" for w in windows)


# ──────────────────────────────────────────────────────────────────────────────
# 2. Active universes change based on regime
# ──────────────────────────────────────────────────────────────────────────────


class TestUniverseSelection:
    """Active universes should reflect the current regime state."""

    def test_bull_risk_on_includes_sp500(self):
        """BULL_RISK_ON must include sp500 in its active universes."""
        cfg = REGIME_CONFIGS[RegimeState.BULL_RISK_ON]
        assert "sp500" in cfg["active_universes"]

    def test_bear_capitulation_excludes_sp500(self):
        """BEAR_CAPITULATION should NOT include sp500 (capital preservation)."""
        cfg = REGIME_CONFIGS[RegimeState.BEAR_CAPITULATION]
        assert "sp500" not in cfg["active_universes"]
        assert "treasury_etfs" in cfg["active_universes"]
        assert "gold_etfs" in cfg["active_universes"]

    def test_bear_risk_off_uses_safe_haven_universes(self):
        """BEAR_RISK_OFF should restrict to defensive/safe-haven universes."""
        cfg = REGIME_CONFIGS[RegimeState.BEAR_RISK_OFF]
        assert "treasury_etfs" in cfg["active_universes"]
        assert "gold_etfs" in cfg["active_universes"]
        # No growth universe
        assert "commodity_etfs" not in cfg["active_universes"]

    def test_window_universes_match_regime_history(self):
        """Universes in the window dict must match those from regime_history."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2022-01-01", end_date="2022-12-31"
        )
        bear_history_desc = [
            {
                "date": "2022-06-01",
                "regime_state": "bear_risk_off",
                "active_universes": ["treasury_etfs", "gold_etfs"],
                "sizing_multiplier": 0.5,
                "enabled_strategies": ["trend_following"],
            },
            {
                "date": "2022-01-03",
                "regime_state": "bear_risk_off",
                "active_universes": ["treasury_etfs", "gold_etfs"],
                "sizing_multiplier": 0.5,
                "enabled_strategies": ["trend_following"],
            },
        ]
        with patch("db.atlas_db.get_regime_history", return_value=bear_history_desc):
            windows = bt._get_regime_windows()

        assert windows[0]["universes"] == ["treasury_etfs", "gold_etfs"]

    def test_all_six_regime_states_have_active_universes(self):
        """Every RegimeState must define at least one active universe."""
        for state in RegimeState:
            cfg = REGIME_CONFIGS[state]
            assert len(cfg["active_universes"]) >= 1, (
                f"{state.value} has no active_universes"
            )


# ──────────────────────────────────────────────────────────────────────────────
# 3. sizing_multiplier is applied to risk config
# ──────────────────────────────────────────────────────────────────────────────


class TestSizingMultiplier:
    """_apply_sizing() correctly scales risk parameters."""

    def test_multiplier_1_leaves_risk_unchanged(self):
        """1.0× should not change max_risk_per_trade_pct."""
        bt = RegimeAwareBacktest(MOCK_CONFIG)
        original = MOCK_CONFIG["risk"]["max_risk_per_trade_pct"]
        cfg = bt._apply_sizing(MOCK_CONFIG, 1.0)
        assert cfg["risk"]["max_risk_per_trade_pct"] == pytest.approx(original)

    def test_multiplier_0_5_halves_risk_pct(self):
        """0.5× should halve max_risk_per_trade_pct."""
        bt = RegimeAwareBacktest(MOCK_CONFIG)
        original = MOCK_CONFIG["risk"]["max_risk_per_trade_pct"]
        cfg = bt._apply_sizing(MOCK_CONFIG, 0.5)
        assert cfg["risk"]["max_risk_per_trade_pct"] == pytest.approx(original * 0.5)

    def test_bear_cap_multiplier_0_3(self):
        """BEAR_CAPITULATION (0.3×) should scale risk to 30%."""
        bt = RegimeAwareBacktest(MOCK_CONFIG)
        original = MOCK_CONFIG["risk"]["max_risk_per_trade_pct"]
        cfg = bt._apply_sizing(MOCK_CONFIG, 0.3)
        assert cfg["risk"]["max_risk_per_trade_pct"] == pytest.approx(original * 0.3)

    def test_apply_sizing_does_not_mutate_original(self):
        """_apply_sizing must return a deep copy and not touch the input."""
        bt = RegimeAwareBacktest(MOCK_CONFIG)
        before = copy.deepcopy(MOCK_CONFIG)
        bt._apply_sizing(MOCK_CONFIG, 0.5)
        assert MOCK_CONFIG == before

    def test_max_positions_scaled_and_floored_at_1(self):
        """max_open_positions is scaled proportionally, minimum 1."""
        bt = RegimeAwareBacktest(MOCK_CONFIG)
        # 10 positions × 0.3 = 3
        cfg = bt._apply_sizing(MOCK_CONFIG, 0.3)
        assert cfg["risk"]["max_open_positions"] == 3

    def test_extreme_small_multiplier_floors_at_1(self):
        """Very small multiplier should never produce 0 positions."""
        bt = RegimeAwareBacktest(MOCK_CONFIG)
        cfg = bt._apply_sizing(MOCK_CONFIG, 0.01)
        assert cfg["risk"]["max_open_positions"] >= 1

    def test_bear_cap_has_lowest_multiplier_of_all_states(self):
        """BEAR_CAPITULATION must have the most conservative sizing."""
        bear_cap = REGIME_CONFIGS[RegimeState.BEAR_CAPITULATION]["sizing_multiplier"]
        for state, regime_cfg in REGIME_CONFIGS.items():
            assert regime_cfg["sizing_multiplier"] >= bear_cap, (
                f"{state.value} has sizing_multiplier "
                f"({regime_cfg['sizing_multiplier']}) < BEAR_CAPITULATION "
                f"({bear_cap})"
            )

    def test_bull_risk_on_has_full_sizing(self):
        """BULL_RISK_ON should have the maximum sizing_multiplier (1.0)."""
        assert REGIME_CONFIGS[RegimeState.BULL_RISK_ON]["sizing_multiplier"] == 1.0


# ──────────────────────────────────────────────────────────────────────────────
# 4. compare_with_sp500_only returns both results
# ──────────────────────────────────────────────────────────────────────────────


class TestCompareWithSp500Only:
    """compare_with_sp500_only() runs two backtests and returns comparison."""

    def _make_comparison_result(self, bt: RegimeAwareBacktest):
        """Helper: run compare_with_sp500_only with all heavy ops mocked."""
        regime_result = RegimeBacktestResult(
            result=_make_backtest_result(n_trades=10, sharpe=0.9, cagr=0.15),
            regime_windows=[],
            regime_distribution={"bull_risk_on": 1},
        )
        sp500_result = _make_backtest_result(n_trades=8, sharpe=0.7, cagr=0.12)

        with patch.object(bt, "run", return_value=regime_result):
            with patch.object(
                bt, "_load_universe_data", return_value={"SPY": pd.DataFrame()}
            ):
                with patch.object(bt, "_build_strategies", return_value=[]):
                    with patch(
                        "regime.backtest.BacktestEngine"
                    ) as MockEngine:
                        MockEngine.return_value.run_walkforward.return_value = (
                            sp500_result
                        )
                        return bt.compare_with_sp500_only(), regime_result, sp500_result

    def test_returns_three_keys(self):
        """Result must contain regime_aware, sp500_only, and delta."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2021-12-31"
        )
        result, _, _ = self._make_comparison_result(bt)
        assert "regime_aware" in result
        assert "sp500_only" in result
        assert "delta" in result

    def test_regime_aware_is_regime_backtest_result(self):
        """regime_aware must be a RegimeBacktestResult instance."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2021-12-31"
        )
        result, _, _ = self._make_comparison_result(bt)
        assert isinstance(result["regime_aware"], RegimeBacktestResult)

    def test_sp500_only_is_backtest_result(self):
        """sp500_only must be a BacktestResult instance."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2021-12-31"
        )
        result, _, _ = self._make_comparison_result(bt)
        assert isinstance(result["sp500_only"], BacktestResult)

    def test_delta_sharpe_is_difference(self):
        """delta['sharpe'] == regime_sharpe - sp500_sharpe."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2021-12-31"
        )
        result, regime_r, sp500_r = self._make_comparison_result(bt)
        expected = regime_r.result.metrics["sharpe"] - sp500_r.metrics["sharpe"]
        assert result["delta"]["sharpe"] == pytest.approx(expected, abs=1e-5)

    def test_delta_cagr_is_difference(self):
        """delta['cagr'] == regime_cagr - sp500_cagr."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2021-12-31"
        )
        result, regime_r, sp500_r = self._make_comparison_result(bt)
        expected = regime_r.result.metrics["cagr"] - sp500_r.metrics["cagr"]
        assert result["delta"]["cagr"] == pytest.approx(expected, abs=1e-5)

    def test_delta_contains_all_standard_metrics(self):
        """delta should include all six standard metric keys."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2021-12-31"
        )
        result, _, _ = self._make_comparison_result(bt)
        for key in ["sharpe", "cagr", "max_drawdown", "win_rate", "profit_factor"]:
            assert key in result["delta"], f"delta missing key: {key}"

    def test_comparison_attached_to_regime_result(self):
        """comparison_vs_sp500 on the RegimeBacktestResult should be populated."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2021-12-31"
        )
        result, _, _ = self._make_comparison_result(bt)
        regime_r = result["regime_aware"]
        assert "delta" in regime_r.comparison_vs_sp500
        assert "sp500_metrics" in regime_r.comparison_vs_sp500


# ──────────────────────────────────────────────────────────────────────────────
# 5. run() method end-to-end (mocked engine)
# ──────────────────────────────────────────────────────────────────────────────


class TestRunMethod:
    """run() aggregates sub-results and returns RegimeBacktestResult."""

    def test_run_returns_regime_backtest_result_type(self):
        """run() must return a RegimeBacktestResult."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2021-12-31"
        )
        mock_sub = _make_backtest_result()

        with patch("db.atlas_db.get_regime_history", return_value=BULL_HISTORY_DESC):
            with patch.object(
                bt, "_load_universe_data", return_value={"SPY": pd.DataFrame()}
            ):
                with patch.object(bt, "_build_strategies", return_value=[MagicMock()]):
                    with patch("regime.backtest.BacktestEngine") as MockEngine:
                        MockEngine.return_value.run_walkforward.return_value = mock_sub
                        result = bt.run()

        assert isinstance(result, RegimeBacktestResult)

    def test_run_populates_regime_distribution(self):
        """regime_distribution should count one entry per unique regime state."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2022-12-31"
        )
        mock_sub = _make_backtest_result()

        with patch("db.atlas_db.get_regime_history", return_value=MULTI_REGIME_DESC):
            with patch.object(
                bt, "_load_universe_data", return_value={"SPY": pd.DataFrame()}
            ):
                with patch.object(bt, "_build_strategies", return_value=[MagicMock()]):
                    with patch("regime.backtest.BacktestEngine") as MockEngine:
                        MockEngine.return_value.run_walkforward.return_value = mock_sub
                        result = bt.run()

        assert "bull_risk_on" in result.regime_distribution
        assert "bear_risk_off" in result.regime_distribution

    def test_run_aggregates_trades_across_windows(self):
        """Total trade count should equal the sum of all sub-backtest trades."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2022-12-31"
        )
        # Each sub-backtest has 5 trades; 2 windows → 10 total
        mock_sub = _make_backtest_result(n_trades=5)

        with patch("db.atlas_db.get_regime_history", return_value=MULTI_REGIME_DESC):
            with patch.object(
                bt, "_load_universe_data", return_value={"SPY": pd.DataFrame()}
            ):
                with patch.object(bt, "_build_strategies", return_value=[MagicMock()]):
                    with patch("regime.backtest.BacktestEngine") as MockEngine:
                        MockEngine.return_value.run_walkforward.return_value = mock_sub
                        result = bt.run()

        assert len(result.result.trades) == 10  # 5 per window × 2 windows

    def test_run_skips_window_when_no_data(self):
        """Windows with no available data should be skipped gracefully."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2021-12-31"
        )
        with patch("db.atlas_db.get_regime_history", return_value=BULL_HISTORY_DESC):
            with patch.object(bt, "_load_universe_data", return_value={}):
                result = bt.run()

        assert isinstance(result, RegimeBacktestResult)
        assert len(result.result.trades) == 0
        # The window should be recorded as skipped
        assert any(w.get("skipped") for w in result.regime_windows)

    def test_run_skips_window_when_no_strategies(self):
        """Windows with no applicable strategies should be skipped gracefully."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2021-12-31"
        )
        with patch("db.atlas_db.get_regime_history", return_value=BULL_HISTORY_DESC):
            with patch.object(
                bt, "_load_universe_data", return_value={"SPY": pd.DataFrame()}
            ):
                with patch.object(bt, "_build_strategies", return_value=[]):
                    result = bt.run()

        assert isinstance(result, RegimeBacktestResult)
        assert len(result.result.trades) == 0
        assert any(w.get("skipped") for w in result.regime_windows)

    def test_run_records_correct_window_count(self):
        """regime_windows list should contain one entry per regime window."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2022-12-31"
        )
        mock_sub = _make_backtest_result()

        with patch("db.atlas_db.get_regime_history", return_value=MULTI_REGIME_DESC):
            with patch.object(
                bt, "_load_universe_data", return_value={"SPY": pd.DataFrame()}
            ):
                with patch.object(bt, "_build_strategies", return_value=[MagicMock()]):
                    with patch("regime.backtest.BacktestEngine") as MockEngine:
                        MockEngine.return_value.run_walkforward.return_value = mock_sub
                        result = bt.run()

        assert len(result.regime_windows) == 2

    def test_run_engine_called_with_sized_config(self):
        """BacktestEngine must be instantiated with the sizing-adjusted config."""
        bt = RegimeAwareBacktest(
            MOCK_CONFIG, start_date="2021-01-01", end_date="2021-12-31"
        )
        mock_sub = _make_backtest_result()
        # Bear window: sizing=0.5 → risk should be halved
        bear_history_desc = [
            {
                "date": "2021-06-01",
                "regime_state": "bear_risk_off",
                "active_universes": ["treasury_etfs"],
                "sizing_multiplier": 0.5,
                "enabled_strategies": ["trend_following"],
            },
            {
                "date": "2021-01-04",
                "regime_state": "bear_risk_off",
                "active_universes": ["treasury_etfs"],
                "sizing_multiplier": 0.5,
                "enabled_strategies": ["trend_following"],
            },
        ]
        original_risk = MOCK_CONFIG["risk"]["max_risk_per_trade_pct"]
        captured_configs = []

        with patch("db.atlas_db.get_regime_history", return_value=bear_history_desc):
            with patch.object(
                bt, "_load_universe_data", return_value={"TLT": pd.DataFrame()}
            ):
                with patch.object(bt, "_build_strategies", return_value=[MagicMock()]):
                    with patch("regime.backtest.BacktestEngine") as MockEngine:
                        MockEngine.return_value.run_walkforward.return_value = mock_sub

                        def capture_config(cfg, **kwargs):
                            captured_configs.append(copy.deepcopy(cfg))
                            return MockEngine.return_value

                        MockEngine.side_effect = capture_config
                        bt.run()

        assert len(captured_configs) == 1
        scaled_risk = captured_configs[0]["risk"]["max_risk_per_trade_pct"]
        assert scaled_risk == pytest.approx(original_risk * 0.5)


# ──────────────────────────────────────────────────────────────────────────────
# 6. Aggregation helpers
# ──────────────────────────────────────────────────────────────────────────────


class TestAggregation:
    """_aggregate_results() combines multiple BacktestResult objects."""

    def test_empty_sub_results_returns_empty(self):
        """No sub-results → empty BacktestResult with no trades."""
        aggregated = RegimeAwareBacktest._aggregate_results([], MOCK_CONFIG)
        assert isinstance(aggregated, BacktestResult)
        assert len(aggregated.trades) == 0
        assert aggregated.equity_curve.empty

    def test_trades_are_concatenated(self):
        """Trades from multiple sub-results should all appear in aggregated."""
        r1 = _make_backtest_result(n_trades=3)
        r2 = _make_backtest_result(n_trades=7)
        aggregated = RegimeAwareBacktest._aggregate_results([r1, r2], MOCK_CONFIG)
        assert len(aggregated.trades) == 10

    def test_equity_curves_are_merged(self):
        """Equity curves from sub-results should be merged into one Series."""
        r1 = _make_backtest_result()
        r2 = _make_backtest_result()
        aggregated = RegimeAwareBacktest._aggregate_results([r1, r2], MOCK_CONFIG)
        # Combined should be non-empty
        assert not aggregated.equity_curve.empty

    def test_duplicate_dates_deduplicated(self):
        """Overlapping dates in equity curves should be deduplicated."""
        r1 = _make_backtest_result()
        r2 = _make_backtest_result()  # same dates as r1
        aggregated = RegimeAwareBacktest._aggregate_results([r1, r2], MOCK_CONFIG)
        assert aggregated.equity_curve.index.is_unique
