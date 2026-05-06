"""
tests/test_dsr_param_grids.py

Guards for DSR-overfitting tightened parameter grids (Item 4, 2026-05-06).

Each test verifies:
1. The tightened dimension has the expected (narrower) count.
2. The full grid cartesian product is smaller than the pre-tightening size.
3. Previously-best-performing research_best params are still in the grid
   (so we don't lose the global optimum by tightening).

Commit: fix(research): tighten DSR-overfitting parameter ranges (11 combos)
"""

from __future__ import annotations

import math
import sqlite3
from functools import reduce
from operator import mul
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------
from research.sweep import PARAM_GRIDS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).parent.parent / "data" / "atlas.db"


def _grid_size(grid: dict[str, list]) -> int:
    sizes = [len(v) for v in grid.values()]
    return reduce(mul, sizes, 1)


def _get_best_research_params(strategy: str, universe: str) -> dict[str, Any] | None:
    """Return the param dict of the highest-sharpe research_best row for the combo.

    Returns None if no row exists or the best row has no specific params ({}).
    """
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT params, sharpe FROM research_best "
            "WHERE strategy=? AND universe=? AND params IS NOT NULL "
            "ORDER BY sharpe DESC",
            (strategy, universe),
        ).fetchall()
        conn.close()
    except Exception:
        return None

    import json
    for row in rows:
        try:
            params = json.loads(row["params"]) if row["params"] else {}
        except (json.JSONDecodeError, TypeError):
            continue
        if params:  # skip empty baseline rows
            return params
    return None


# ---------------------------------------------------------------------------
# 1. bb_squeeze / sp500
#    Tightened: bb_period [10,15,20,30]→[10,15]; bb_std [1.5,2.0,2.5]→[1.5,2.0]
#    Old grid size: 4×3×4×3=144  New: 2×2×4×3=48
# ---------------------------------------------------------------------------
class TestBbSqueezeGrid:
    STRATEGY = "bb_squeeze"

    def test_bb_period_narrowed(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert grid["bb_period"] == [10, 15], (
            f"bb_period should be [10,15], got {grid['bb_period']}"
        )

    def test_bb_std_narrowed(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert grid["bb_std"] == [1.5, 2.0], (
            f"bb_std should be [1.5,2.0], got {grid['bb_std']}"
        )

    def test_grid_size_reduced(self) -> None:
        size = _grid_size(PARAM_GRIDS[self.STRATEGY])
        assert size < 144, f"Grid size {size} should be < 144 (old)"
        assert size == 48, f"Grid size should be 48, got {size}"

    def test_removed_overfit_values_absent(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert 20 not in grid["bb_period"], "bb_period=20 (min=-2.39) should be removed"
        assert 30 not in grid["bb_period"], "bb_period=30 (min=-7.67) should be removed"
        assert 2.5 not in grid["bb_std"], "bb_std=2.5 (min=-1.09) should be removed"

    def test_research_best_params_preserved(self) -> None:
        params = _get_best_research_params(self.STRATEGY, "sp500")
        if params is None:
            pytest.skip("No research_best row with specific params")
        grid = PARAM_GRIDS[self.STRATEGY]
        for key, val in params.items():
            if key in grid:
                assert val in grid[key], (
                    f"research_best param {key}={val} not in tightened grid {grid[key]}"
                )


# ---------------------------------------------------------------------------
# 2. consecutive_down_days / sp500
#    Tightened: min_down_days [2,3,4,5]→[2,3,4]
#    Old: 512  New: 384
# ---------------------------------------------------------------------------
class TestConsecutiveDownDaysGrid:
    STRATEGY = "consecutive_down_days"

    def test_min_down_days_narrowed(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert grid["min_down_days"] == [2, 3, 4], (
            f"min_down_days should be [2,3,4], got {grid['min_down_days']}"
        )

    def test_grid_size_reduced(self) -> None:
        size = _grid_size(PARAM_GRIDS[self.STRATEGY])
        assert size < 512, f"Grid size {size} should be < 512 (old)"
        assert size == 384, f"Grid size should be 384, got {size}"

    def test_removed_overfit_value_absent(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert 5 not in grid["min_down_days"], (
            "min_down_days=5 (avg=-4.48, min=-17.88) should be removed"
        )

    def test_research_best_params_preserved(self) -> None:
        params = _get_best_research_params(self.STRATEGY, "sp500")
        if params is None:
            pytest.skip("No research_best row with specific params")
        grid = PARAM_GRIDS[self.STRATEGY]
        for key, val in params.items():
            if key in grid:
                assert val in grid[key], (
                    f"research_best param {key}={val} not in tightened grid {grid[key]}"
                )


# ---------------------------------------------------------------------------
# 3. demark_sequential / sp500
#    Tightened: setup_bars [7,9,13]→[7,9]
#    Old: 96  New: 64
# ---------------------------------------------------------------------------
class TestDemarkSequentialGrid:
    STRATEGY = "demark_sequential"

    def test_setup_bars_narrowed(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert grid["setup_bars"] == [7, 9], (
            f"setup_bars should be [7,9], got {grid['setup_bars']}"
        )

    def test_grid_size_reduced(self) -> None:
        size = _grid_size(PARAM_GRIDS[self.STRATEGY])
        assert size < 96, f"Grid size {size} should be < 96 (old)"
        assert size == 64, f"Grid size should be 64, got {size}"

    def test_removed_overfit_value_absent(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert 13 not in grid["setup_bars"], (
            "setup_bars=13 (avg=-4.96, min=-6.48) should be removed"
        )

    def test_research_best_params_preserved(self) -> None:
        params = _get_best_research_params(self.STRATEGY, "sp500")
        if params is None:
            pytest.skip("No research_best row with specific params")
        grid = PARAM_GRIDS[self.STRATEGY]
        for key, val in params.items():
            if key in grid:
                assert val in grid[key], (
                    f"research_best param {key}={val} not in tightened grid {grid[key]}"
                )


# ---------------------------------------------------------------------------
# 4. keltner_reversion / sp500
#    Tightened: ema_period [10,15,20]→[15,20]; atr_mult [1.5,2.0,2.5]→[1.5,2.0]
#    Old: 288  New: 128
# ---------------------------------------------------------------------------
class TestKeltnerReversionGrid:
    STRATEGY = "keltner_reversion"

    def test_ema_period_narrowed(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert grid["ema_period"] == [15, 20], (
            f"ema_period should be [15,20], got {grid['ema_period']}"
        )

    def test_atr_mult_narrowed(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert grid["atr_mult"] == [1.5, 2.0], (
            f"atr_mult should be [1.5,2.0], got {grid['atr_mult']}"
        )

    def test_grid_size_reduced(self) -> None:
        size = _grid_size(PARAM_GRIDS[self.STRATEGY])
        assert size < 288, f"Grid size {size} should be < 288 (old)"
        assert size == 128, f"Grid size should be 128, got {size}"

    def test_removed_overfit_values_absent(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert 10 not in grid["ema_period"], (
            "ema_period=10 (avg=-33.49, min=-66.56) should be removed — catastrophic overtrading"
        )
        assert 2.5 not in grid["atr_mult"], (
            "atr_mult=2.5 (avg=-8.90, min=-15.90) should be removed"
        )


# ---------------------------------------------------------------------------
# 5. lower_band_reversion / sp500
#    Tightened: max_hold_days [3,5,7,10]→[5,7,10]; ibs_threshold [0.2,0.3,0.5]→[0.2,0.5]
#    Old: 1536  New: 768
# ---------------------------------------------------------------------------
class TestLowerBandReversionGrid:
    STRATEGY = "lower_band_reversion"

    def test_max_hold_days_narrowed(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert grid["max_hold_days"] == [5, 7, 10], (
            f"max_hold_days should be [5,7,10], got {grid['max_hold_days']}"
        )

    def test_ibs_threshold_narrowed(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert grid["ibs_threshold"] == [0.2, 0.5], (
            f"ibs_threshold should be [0.2,0.5], got {grid['ibs_threshold']}"
        )

    def test_grid_size_reduced(self) -> None:
        size = _grid_size(PARAM_GRIDS[self.STRATEGY])
        assert size < 1536, f"Grid size {size} should be < 1536 (old)"
        assert size == 768, f"Grid size should be 768, got {size}"

    def test_removed_overfit_values_absent(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert 3 not in grid["max_hold_days"], (
            "max_hold_days=3 (avg=-2.93, min=-3.72) should be removed"
        )
        assert 0.3 not in grid["ibs_threshold"], (
            "ibs_threshold=0.3 (avg=-1.60, min=-3.09) should be removed"
        )

    def test_research_best_params_preserved(self) -> None:
        params = _get_best_research_params(self.STRATEGY, "sp500")
        if params is None:
            pytest.skip("No research_best row with specific params")
        grid = PARAM_GRIDS[self.STRATEGY]
        # research_best has ibs_threshold=0.5 which is still in grid
        for key, val in params.items():
            if key in grid:
                assert val in grid[key], (
                    f"research_best param {key}={val} not in tightened grid {grid[key]}"
                )


# ---------------------------------------------------------------------------
# 6. mean_reversion / sp500
#    Tightened: rsi_oversold [25,30,35,40,20]→[30,35,40]
#    Old rsi_oversold dim: 5  New: 3  (overall grid: 1.28M→768K)
# ---------------------------------------------------------------------------
class TestMeanReversionGrid:
    STRATEGY = "mean_reversion"

    def test_rsi_oversold_narrowed(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert sorted(grid["rsi_oversold"]) == [30, 35, 40], (
            f"rsi_oversold should be [30,35,40], got {grid['rsi_oversold']}"
        )

    def test_rsi_oversold_count(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert len(grid["rsi_oversold"]) == 3, (
            f"rsi_oversold should have 3 values, got {len(grid['rsi_oversold'])}"
        )

    def test_removed_overfit_values_absent(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert 20 not in grid["rsi_oversold"], (
            "rsi_oversold=20 (min=-5.63) should be removed — seeds exploration of rsi<25 region"
        )
        assert 25 not in grid["rsi_oversold"], (
            "rsi_oversold=25 (min=-2.64) should be removed"
        )

    def test_grid_size_reduced(self) -> None:
        # Old had 5 rsi_oversold values, new has 3.  Ratio ~= 3/5 of old.
        old_oversold_count = 5
        new_oversold_count = len(PARAM_GRIDS[self.STRATEGY]["rsi_oversold"])
        assert new_oversold_count < old_oversold_count

    def test_best_research_seed_still_reachable(self) -> None:
        """rsi_oversold=32 (best in research_best, sharpe=1.372) is not in the grid
        but is reachable via LLM exploration from the 30 starting point.
        Assert 30 is still in the grid as the nearest seed."""
        grid = PARAM_GRIDS[self.STRATEGY]
        assert 30 in grid["rsi_oversold"], (
            "rsi_oversold=30 must stay in grid as seed toward the research-best value of 32"
        )


# ---------------------------------------------------------------------------
# 7. opening_gap / sp500
#    Tightened: gap_threshold [-0.01,-0.015,-0.02,-0.025,-0.03]→[-0.01,-0.015,-0.02]
#    Old gap_threshold dim: 5  New: 3
# ---------------------------------------------------------------------------
class TestOpeningGapGrid:
    STRATEGY = "opening_gap"

    def test_gap_threshold_narrowed(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert sorted(grid["gap_threshold"]) == sorted([-0.01, -0.015, -0.02]), (
            f"gap_threshold should be [-0.01,-0.015,-0.02], got {grid['gap_threshold']}"
        )

    def test_gap_threshold_count(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert len(grid["gap_threshold"]) == 3, (
            f"gap_threshold should have 3 values, got {len(grid['gap_threshold'])}"
        )

    def test_removed_overfit_values_absent(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert -0.025 not in grid["gap_threshold"], (
            "gap_threshold=-0.025 (min=-11.24) should be removed"
        )
        assert -0.03 not in grid["gap_threshold"], (
            "gap_threshold=-0.03 (min=-11.24) should be removed"
        )

    def test_grid_size_reduced(self) -> None:
        old_gt_count = 5
        new_gt_count = len(PARAM_GRIDS[self.STRATEGY]["gap_threshold"])
        assert new_gt_count < old_gt_count


# ---------------------------------------------------------------------------
# 8. stochastic_oversold / sp500
#    Tightened: stoch_period [5,10,14,21]→[5,10,14]; stoch_smooth [3,5]→[3]
#    Old: 1024  New: 384
# ---------------------------------------------------------------------------
class TestStochasticOversoldGrid:
    STRATEGY = "stochastic_oversold"

    def test_stoch_period_narrowed(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert grid["stoch_period"] == [5, 10, 14], (
            f"stoch_period should be [5,10,14], got {grid['stoch_period']}"
        )

    def test_stoch_smooth_narrowed(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert grid["stoch_smooth"] == [3], (
            f"stoch_smooth should be [3], got {grid['stoch_smooth']}"
        )

    def test_grid_size_reduced(self) -> None:
        size = _grid_size(PARAM_GRIDS[self.STRATEGY])
        assert size < 1024, f"Grid size {size} should be < 1024 (old)"
        assert size == 384, f"Grid size should be 384, got {size}"

    def test_removed_overfit_values_absent(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert 21 not in grid["stoch_period"], (
            "stoch_period=21 (avg=-3.46, min=-3.98) should be removed"
        )
        assert 5 not in grid["stoch_smooth"], (
            "stoch_smooth=5 (avg=-3.73, min=-4.47) — catastrophic overfit, should be removed"
        )

    def test_research_best_params_preserved(self) -> None:
        params = _get_best_research_params(self.STRATEGY, "sp500")
        if params is None:
            pytest.skip("No research_best row with specific params")
        grid = PARAM_GRIDS[self.STRATEGY]
        # research_best has stoch_period=5 (stays) — stoch_smooth not specified
        for key, val in params.items():
            if key in grid:
                assert val in grid[key], (
                    f"research_best param {key}={val} not in tightened grid {grid[key]}"
                )


# ---------------------------------------------------------------------------
# 9. trend_following / sp500
#    Tightened: fast_ma [10,15,20,30,50]→[10,15,30,50]; pullback_pct [...]→[0.02-0.05]
#    Old: 38400  New: 24576
# ---------------------------------------------------------------------------
class TestTrendFollowingGrid:
    STRATEGY = "trend_following"

    def test_fast_ma_narrowed(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert sorted(grid["fast_ma"]) == [10, 15, 30, 50], (
            f"fast_ma should be [10,15,30,50], got {grid['fast_ma']}"
        )

    def test_pullback_pct_narrowed(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert sorted(grid["pullback_pct"]) == sorted([0.02, 0.03, 0.04, 0.05]), (
            f"pullback_pct should be [0.02,0.03,0.04,0.05], got {grid['pullback_pct']}"
        )

    def test_grid_size_reduced(self) -> None:
        size = _grid_size(PARAM_GRIDS[self.STRATEGY])
        assert size < 38400, f"Grid size {size} should be < 38400 (old)"
        assert size == 24576, f"Grid size should be 24576, got {size}"

    def test_removed_overfit_values_absent(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert 20 not in grid["fast_ma"], (
            "fast_ma=20 (equals min slow_ma=20, degenerate zero-spread MA) should be removed"
        )
        assert 0.06 not in grid["pullback_pct"], (
            "pullback_pct=0.06 (avg=-0.60, min=-2.95) should be removed"
        )

    def test_research_best_params_preserved(self) -> None:
        params = _get_best_research_params(self.STRATEGY, "sp500")
        if params is None:
            pytest.skip("No research_best row with specific params")
        grid = PARAM_GRIDS[self.STRATEGY]
        # research_best has fast_ma=15 (stays), pullback_pct=0.04 (stays)
        for key, val in params.items():
            if key in grid:
                assert val in grid[key], (
                    f"research_best param {key}={val} not in tightened grid {grid[key]}"
                )


# ---------------------------------------------------------------------------
# 10. triple_rsi / sp500
#     Tightened: rsi_entry [20,25,30,35]→[30,35]; decline_days [2,3,4]→[2,3]
#     Old: 1152  New: 384
# ---------------------------------------------------------------------------
class TestTripleRsiGrid:
    STRATEGY = "triple_rsi"

    def test_rsi_entry_narrowed(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert sorted(grid["rsi_entry"]) == [30, 35], (
            f"rsi_entry should be [30,35], got {grid['rsi_entry']}"
        )

    def test_decline_days_narrowed(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert grid["decline_days"] == [2, 3], (
            f"decline_days should be [2,3], got {grid['decline_days']}"
        )

    def test_grid_size_reduced(self) -> None:
        size = _grid_size(PARAM_GRIDS[self.STRATEGY])
        assert size < 1152, f"Grid size {size} should be < 1152 (old)"
        assert size == 384, f"Grid size should be 384, got {size}"

    def test_removed_overfit_values_absent(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert 20 not in grid["rsi_entry"], (
            "rsi_entry=20 (avg=-5.28, min=-7.91) — triple-RSI<20 fires only during crashes"
        )
        assert 25 not in grid["rsi_entry"], (
            "rsi_entry=25 (avg=-2.51, min=-5.15) should be removed"
        )
        assert 4 not in grid["decline_days"], (
            "decline_days=4 (avg=-4.46, min=-5.70) should be removed"
        )

    def test_research_best_params_preserved(self) -> None:
        params = _get_best_research_params(self.STRATEGY, "sp500")
        if params is None:
            pytest.skip("No research_best row with specific params")
        grid = PARAM_GRIDS[self.STRATEGY]
        # research_best has rsi_entry=35 (stays), decline_days=2 (stays)
        for key, val in params.items():
            if key in grid:
                assert val in grid[key], (
                    f"research_best param {key}={val} not in tightened grid {grid[key]}"
                )


# ---------------------------------------------------------------------------
# 11. williams_percent_r / sp500
#     Tightened: wr_entry [-80,-85,-90,-95]→[-80,-85,-95]
#     Old: 384  New: 288
# ---------------------------------------------------------------------------
class TestWilliamsPercentRGrid:
    STRATEGY = "williams_percent_r"

    def test_wr_entry_narrowed(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert sorted(grid["wr_entry"]) == sorted([-80, -85, -95]), (
            f"wr_entry should be [-80,-85,-95], got {grid['wr_entry']}"
        )

    def test_wr_entry_count(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert len(grid["wr_entry"]) == 3, (
            f"wr_entry should have 3 values, got {len(grid['wr_entry'])}"
        )

    def test_grid_size_reduced(self) -> None:
        size = _grid_size(PARAM_GRIDS[self.STRATEGY])
        assert size < 384, f"Grid size {size} should be < 384 (old)"
        assert size == 288, f"Grid size should be 288, got {size}"

    def test_removed_overfit_value_absent(self) -> None:
        grid = PARAM_GRIDS[self.STRATEGY]
        assert -90 not in grid["wr_entry"], (
            "wr_entry=-90 (avg=-5.16, min=-13.86) — fires into sustained downtrends"
        )

    def test_research_best_params_preserved(self) -> None:
        params = _get_best_research_params(self.STRATEGY, "sp500")
        if params is None:
            pytest.skip("No research_best row with specific params")
        grid = PARAM_GRIDS[self.STRATEGY]
        # research_best has wr_entry=-85 (stays)
        for key, val in params.items():
            if key in grid:
                assert val in grid[key], (
                    f"research_best param {key}={val} not in tightened grid {grid[key]}"
                )


# ---------------------------------------------------------------------------
# Cross-combo invariants
# ---------------------------------------------------------------------------
class TestCrossComboInvariants:
    """Sanity checks across all tightened combos."""

    CAP_HIT_COMBOS = [
        "bb_squeeze",
        "consecutive_down_days",
        "demark_sequential",
        "keltner_reversion",
        "lower_band_reversion",
        "mean_reversion",
        "opening_gap",
        "stochastic_oversold",
        "trend_following",
        "triple_rsi",
        "williams_percent_r",
    ]

    def test_all_combos_still_in_param_grids(self) -> None:
        for strategy in self.CAP_HIT_COMBOS:
            assert strategy in PARAM_GRIDS, f"{strategy} missing from PARAM_GRIDS"

    def test_all_grids_have_at_least_two_values_per_dim(self) -> None:
        """Each tightened grid should still explore ≥2 values per dimension
        (except stoch_smooth which is intentionally reduced to 1 — the only non-catastrophic value).
        """
        SINGLE_VALUE_EXCEPTIONS = {
            ("stochastic_oversold", "stoch_smooth"),
        }
        for strategy in self.CAP_HIT_COMBOS:
            grid = PARAM_GRIDS[strategy]
            for dim, vals in grid.items():
                if (strategy, dim) in SINGLE_VALUE_EXCEPTIONS:
                    assert len(vals) >= 1, f"{strategy}.{dim} should have ≥1 value"
                else:
                    assert len(vals) >= 2, (
                        f"{strategy}.{dim} should have ≥2 values, got {vals}"
                    )

    def test_grid_sizes_strictly_positive(self) -> None:
        for strategy in self.CAP_HIT_COMBOS:
            size = _grid_size(PARAM_GRIDS[strategy])
            assert size > 0, f"{strategy} grid size is 0"

    def test_no_nan_or_none_values_in_tightened_grids(self) -> None:
        TIGHTENED_DIMS = {
            "mean_reversion": ["rsi_oversold"],
            "trend_following": ["fast_ma", "pullback_pct"],
            "opening_gap": ["gap_threshold"],
            "bb_squeeze": ["bb_period", "bb_std"],
            "consecutive_down_days": ["min_down_days"],
            "demark_sequential": ["setup_bars"],
            "stochastic_oversold": ["stoch_period", "stoch_smooth"],
            "williams_percent_r": ["wr_entry"],
            "lower_band_reversion": ["ibs_threshold", "max_hold_days"],
            "triple_rsi": ["rsi_entry", "decline_days"],
            "keltner_reversion": ["ema_period", "atr_mult"],
        }
        for strategy, dims in TIGHTENED_DIMS.items():
            grid = PARAM_GRIDS[strategy]
            for dim in dims:
                for val in grid[dim]:
                    assert val is not None, f"{strategy}.{dim} contains None"
                    if isinstance(val, float):
                        assert not math.isnan(val), f"{strategy}.{dim} contains NaN"
