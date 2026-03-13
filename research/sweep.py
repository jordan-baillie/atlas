#!/usr/bin/env python3
"""Atlas Autoresearch Sweeper — headless 24/7 parameter optimization.

The mechanical workhorse that runs without an LLM. Systematically sweeps
parameter grids for every strategy, keeps improvements, discards the rest.
Runs as a systemd service and sends Telegram notifications on discoveries.

This is the "body". The interactive ResearchSession (loop.py) is the "brain".

Usage:
    python3 research/sweep.py                        # all strategies
    python3 research/sweep.py --strategy mean_reversion
    python3 research/sweep.py --strategy mean_reversion --top-n 50

Systemd:
    systemctl start atlas-autoresearch
"""

import os

# Limit threads BEFORE any numerical imports. Each forked worker inherits this.
# Backtests are loop-bound (131s→134s with 1 thread), so no perf impact,
# but prevents 6 workers × 8 threads = 48 threads competing for 8 cores.
os.environ["NUMEXPR_MAX_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"

import argparse
import json
import logging
import signal
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ATLAS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_ROOT))

from research.loop import (
    ResearchSession,
    keep_or_discard,
    load_best,
    save_best,
    _append_result,
    _append_journal,
    _increment_run_count,
    _print_metrics,
    leaderboard,
    combined_test,
)

logger = logging.getLogger("autoresearch.sweep")

# Brain — structured research memory (real-time updates)
from research.brain.writer import (
    update_strategy,
    record_param_result,
    record_experiment,
    SweepSession,
    update_state,
    rebuild_all_indexes,
)

# Result-aware intelligence — reads brain/params/*.md to skip dead zones
# and detect stale strategies.  Gracefully unavailable on first run.
try:
    from research.param_history import (
        get_strategy_staleness,
        build_strategy_param_history,
        reset_staleness,
    )
    _PARAM_HISTORY_AVAILABLE = True
except Exception as _ph_import_err:  # pragma: no cover
    logger.debug("param_history not available: %s", _ph_import_err)
    _PARAM_HISTORY_AVAILABLE = False

# ─── Grid Expansion (jitter around current best) ────────────────────────────

import random

def expand_grid(
    base_grid: Dict[str, list],
    current_best: Dict[str, Any],
    n_jitter: int = 3,
    rng_seed: Optional[int] = None,
    param_history: Optional[Dict[str, Dict]] = None,
) -> Dict[str, list]:
    """Expand a fixed parameter grid with jittered values around the current best.

    For each param in the grid, if the current best is already one of the grid
    values, adds `n_jitter` nearby values (±10-30% of the step size between
    grid values). This prevents the sweep from stalling when the grid is exhausted.

    Rules:
    - Boolean params: no expansion (only True/False)
    - String params: no expansion (categorical, no interpolation)
    - Integer params: jitter by ±1, ±2 of the current value, clamped to valid range
    - Float params: jitter by ±10-30% of the grid step size
    - Never produces duplicates of existing grid values
    - Values are rounded to match the precision of the grid

    Result-aware rules (when param_history is provided):
    - Skip values tested 3+ times for this strategy that always lost (win_rate=0)
    - Bias jitter direction toward the best-performing known value

    Args:
        base_grid:     Original PARAM_GRIDS entry for a strategy.
        current_best:  Current best params dict.
        n_jitter:      Number of jittered values to add per param.
        rng_seed:      Random seed for reproducibility (None = random).
        param_history: Per-value statistics from build_strategy_param_history().
            Format: {param_name: {value: {tests, wins, sharpe_deltas}}}
            When provided, enables result-aware pruning and biased jitter.

    Returns:
        New grid dict with expanded value lists.
    """
    rng = random.Random(rng_seed)
    expanded = {}

    for param, values in base_grid.items():
        # Skip booleans and strings — no meaningful jitter
        if all(isinstance(v, bool) for v in values):
            expanded[param] = list(values)
            continue
        if any(isinstance(v, str) for v in values):
            expanded[param] = list(values)
            continue

        current = current_best.get(param)
        numeric_values = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not numeric_values or current is None:
            expanded[param] = list(values)
            continue

        # Determine if this is an int or float param
        is_int = all(isinstance(v, int) for v in numeric_values)
        sorted_vals = sorted(set(numeric_values))

        # Calculate typical step size
        if len(sorted_vals) >= 2:
            steps = [sorted_vals[i+1] - sorted_vals[i] for i in range(len(sorted_vals)-1)]
            avg_step = sum(steps) / len(steps)
        else:
            avg_step = abs(sorted_vals[0]) * 0.2 if sorted_vals[0] != 0 else 1.0

        # Determine bounds (allow 1 step beyond grid edges)
        lo = sorted_vals[0] - avg_step
        hi = sorted_vals[-1] + avg_step

        # For params that must be positive (stop mult, periods, etc.)
        if all(v > 0 for v in sorted_vals):
            lo = max(lo, avg_step * 0.25)  # don't go below ~quarter step

        # ── Result-aware intelligence (when param_history provided) ──────
        # Extract per-value statistics for this param (empty dict = no history).
        value_stats: Dict = (param_history or {}).get(param, {})

        # Determine jitter bias direction from the best-known value.
        # If historical data shows e.g. higher values perform better, shift the
        # random offset range toward positive so jitter explores that direction.
        bias = 0.0
        if value_stats and isinstance(current, (int, float)) and not isinstance(current, bool):
            best_known_val = None
            best_known_delta = float("-inf")
            for v, stats in value_stats.items():
                if stats["tests"] > 0 and isinstance(v, (int, float)):
                    avg_d = sum(stats["sharpe_deltas"]) / stats["tests"]
                    if avg_d > best_known_delta:
                        best_known_delta = avg_d
                        best_known_val = v
            if best_known_val is not None and best_known_val != current:
                # Shift jitter range by ±0.4 toward best direction.
                # This biases sampling without fully committing to one side.
                bias = 0.4 if best_known_val > current else -0.4

        # ── Generate jittered values around the current best ──────────────
        jittered = set()
        attempts = 0
        while len(jittered) < n_jitter and attempts < n_jitter * 10:
            attempts += 1
            # Random offset shifted by bias toward best-known direction
            offset = rng.uniform(-1.0 + bias, 1.0 + bias) * avg_step
            new_val = current + offset
            new_val = max(lo, min(hi, new_val))

            if is_int:
                new_val = int(round(new_val))
            else:
                # Match precision of grid values
                decimal_places = max(
                    len(str(v).split('.')[-1]) if '.' in str(v) else 0
                    for v in numeric_values
                )
                new_val = round(new_val, max(decimal_places, 2))

            # Skip if it's already in the base grid or already jittered
            if new_val not in values and new_val not in jittered:
                jittered.add(new_val)

        # ── Prune dead values (tested 3+ times, never kept) ──────────────
        # Always preserve the current best value even if it's in the dead set.
        def _is_dead(v: Any) -> bool:
            """Return True if this value has been tested ≥3× with 0 wins."""
            if not value_stats:
                return False
            try:
                stats = value_stats.get(v)
            except TypeError:
                return False
            if stats is None:
                return False
            return stats["tests"] >= 3 and stats["wins"] == 0

        candidate_values = list(values) + sorted(jittered)
        pruned = [v for v in candidate_values if not _is_dead(v) or v == current]
        # Safety: never return an empty list — keep at least the current best
        if not pruned:
            pruned = [current] if current is not None else list(values)

        expanded[param] = pruned

    return expanded


# ─── Parameter Grids ─────────────────────────────────────────────────────────

# Each strategy has a grid of parameters to sweep.
# Only scalar params — nested dicts handled separately.
# Values are ordered from most likely to least likely improvement.

PARAM_GRIDS: Dict[str, Dict[str, list]] = {
    # ── Tier 1 / Core ────────────────────────────────────────────────────
    "mean_reversion": {
        "rsi_period": [7, 10, 14, 21, 5],
        "rsi_oversold": [25, 30, 35, 40, 20],
        "zscore_lookback": [15, 20, 30, 10],
        "zscore_entry": [-1.5, -2.0, -2.5, -1.0],
        "atr_period": [10, 14, 20, 7],
        "atr_stop_mult": [2.0, 2.5, 3.0, 1.5],
        "profit_target_atr_mult": [1.5, 2.0, 2.5, 1.0, 3.0],
        "max_hold_days": [5, 7, 10, 15, 20],
        "sma200_filter": [True, False],
        "ibs_max": [0.3, 0.5, 0.7, 1.0],
    },
    "trend_following": {
        "fast_ma": [10, 15, 20, 30, 50],
        "slow_ma": [20, 50, 100, 200],
        "pullback_pct": [0.02, 0.03, 0.04, 0.05, 0.06],
        "atr_period": [10, 14, 20],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "trailing_stop_atr_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [10, 15, 20, 30],
        "sma200_filter": [True, False],
    },
    "opening_gap": {
        "gap_threshold": [-0.01, -0.015, -0.02, -0.025, -0.03],
        "ibs_confirm": [0.3, 0.4, 0.5, 0.6],
        "rsi14_max": [20, 25, 30, 35],
        "vol_surge_threshold": [1.0, 1.2, 1.5, 2.0],
        "atr_period": [10, 14, 20],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [3, 5, 7, 10],
        "sma200_filter": [True, False],
    },
    # ── Tier 2 / Dormant core ─────────────────────────────────────────────
    "connors_rsi2": {
        "rsi_period": [2, 3, 4, 5],
        "rsi_entry": [5, 10, 15, 20],
        "sma_trend_period": [100, 150, 200],
        "sma200_filter": [True, False],
        "min_consecutive_down": [0, 1, 2, 3],
        "ibs_max": [0.3, 0.5, 0.7, 1.0],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [3, 5, 7, 10],
    },
    "momentum_breakout": {
        "breakout_period": [10, 20, 30, 40, 60],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [5, 10, 15, 20],
        "sma200_filter": [True, False],
        "signal_mode": ["raw", "risk_adjusted", "idiosyncratic"],
        "momentum_lookback": [126, 252],
        "momentum_skip": [0, 21, 42],
    },
    "short_term_mr": {
        "rsi_period": [2, 3, 4, 5],
        "rsi_oversold": [10, 15, 20, 25],
        "max_hold_days": [2, 3, 5, 7],
        "atr_stop_mult": [1.5, 2.0, 2.5],
    },
    "bb_squeeze": {
        "bb_period": [10, 15, 20, 30],
        "bb_std": [1.5, 2.0, 2.5],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [5, 10, 15],
    },
    # ── Tier 3 / Research strategies ─────────────────────────────────────
    "adx_trend_pullback": {
        "adx_period": [7, 10, 14, 21],
        "adx_threshold": [20.0, 25.0, 30.0, 35.0],
        "ema_touch_pct": [0.005, 0.01, 0.015, 0.02],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [5, 7, 10, 15],
        "sma200_filter": [True, False],
    },
    "consecutive_down_days": {
        "min_down_days": [2, 3, 4, 5],
        "ibs_threshold": [0.2, 0.3, 0.5, 1.0],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [3, 5, 7, 10],
        "sma200_filter": [True, False],
    },
    "demark_sequential": {
        "setup_bars": [7, 9, 13],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [5, 7, 10, 15],
        "sma200_filter": [True, False],
    },
    "donchian_breakout": {
        "entry_period": [10, 20, 30, 50],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [10, 15, 20, 30],
        "sma200_filter": [True, False],
    },
    "stochastic_oversold": {
        "stoch_period": [5, 10, 14, 21],
        "stoch_smooth": [3, 5],
        "stoch_entry": [10, 15, 20, 25],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [5, 7, 10, 15],
        "sma200_filter": [True, False],
    },
    "williams_percent_r": {
        "wr_period": [10, 14, 21],
        "wr_entry": [-80, -85, -90, -95],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [5, 7, 10, 15],
        "sma200_filter": [True, False],
    },
    "lower_band_reversion": {
        "band_mult": [1.0, 1.5, 2.0, 2.5],
        "ibs_threshold": [0.2, 0.3, 0.5],
        "range_lookback": [10, 15, 20, 25],
        "max_hold_days": [3, 5, 7, 10],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "sma200_filter": [True, False],
    },
    "triple_rsi": {
        "rsi_period": [3, 5, 7],
        "rsi_entry": [20, 25, 30, 35],
        "decline_days": [2, 3, 4],
        "max_hold_days": [3, 5, 7, 10],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "sma200_filter": [True, False],
    },
    "keltner_reversion": {
        "ema_period": [10, 15, 20],
        "atr_mult": [1.5, 2.0, 2.5],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [5, 7, 10, 15],
        "sma200_filter": [True, False],
    },
    "inside_bar_nr7": {
        "nr_lookback": [5, 7, 10],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [3, 5, 7, 10],
        "sma200_filter": [True, False],
    },
    "volume_climax": {
        "volume_mult": [1.5, 2.0, 2.5, 3.0, 4.0],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [3, 5, 7, 10],
        "sma200_filter": [True, False],
    },
    "gap_and_go": {
        "gap_threshold": [0.02, 0.03, 0.04, 0.05],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [3, 5, 7, 10],
        "sma200_filter": [True, False],
    },
    "heikin_ashi_reversal": {
        "reversal_bars": [1, 2, 3, 4],
        "min_red_bars": [2, 3, 4, 5],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [5, 7, 10, 15],
        "sma200_filter": [True, False],
    },
    "macd_divergence": {
        "macd_fast": [8, 12, 16],
        "macd_slow": [20, 26, 30],
        "macd_signal": [7, 9, 11],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [5, 7, 10, 15],
        "sma200_filter": [True, False],
    },
    "overnight_return": {
        "ibs_min": [0.3, 0.4, 0.5, 0.6],
        "momentum_min": [0.0, 0.005, 0.01, 0.015],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [1, 2, 3, 5],
        "sma200_filter": [True, False],
    },
    "pead_earnings_drift": {
        "min_jump_pct": [0.02, 0.03, 0.04, 0.05],
        "max_days_after_event": [1, 2, 3],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [10, 15, 20, 30],
        "sma200_filter": [True, False],
    },
    # ── Tier 4 / New Builder-1 strategies ────────────────────────────────
    "relative_strength_pullback": {
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [5, 7, 10, 15],
        "sma200_filter": [True, False],
    },
    "rsi_divergence": {
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [5, 7, 10, 15],
        "sma200_filter": [True, False],
    },
    "vwap_reversion": {
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [3, 5, 7, 10],
        "sma200_filter": [True, False],
    },
    "monthly_rotation": {
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [10, 15, 20, 30],
        "sma200_filter": [True, False],
    },
    "put_call_vix_proxy": {
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [3, 5, 7, 10],
        "sma200_filter": [True, False],
    },
    "dividend_capture": {
        "days_before_ex": [3, 5, 7, 10],
        "days_after_ex": [3, 5, 7, 10],
        "atr_stop_mult": [2.0, 2.5, 3.0, 3.5],
        "max_hold_days": [10, 15, 20, 30],
        "require_uptrend": [True, False],
    },
}

# Strategy priority order (highest value first)
STRATEGY_ORDER = [
    # Tier 1: Active — improvements go straight to live
    "mean_reversion",
    "trend_following",
    "opening_gap",
    # Tier 2: Dormant — unlock new profit streams
    "connors_rsi2",
    "momentum_breakout",
    "short_term_mr",
    "bb_squeeze",
    # Tier 3: Research strategies
    "adx_trend_pullback",
    "consecutive_down_days",
    "demark_sequential",
    "donchian_breakout",
    "stochastic_oversold",
    "williams_percent_r",
    "lower_band_reversion",
    "triple_rsi",
    "keltner_reversion",
    "inside_bar_nr7",
    "volume_climax",
    "gap_and_go",
    "heikin_ashi_reversal",
    "macd_divergence",
    "overnight_return",
    "pead_earnings_drift",
    # Tier 4: New strategies (Builder-1)
    "relative_strength_pullback",
    "rsi_divergence",
    "vwap_reversion",
    "monthly_rotation",
    "put_call_vix_proxy",
    "dividend_capture",
]

# ─── Heartbeat / Signals ─────────────────────────────────────────────────────

HEARTBEAT_PATH = Path("/tmp/autoresearch-heartbeat.json")
STOP_PATH = Path("/tmp/autoresearch-stop")
_stop_event = None  # Set to threading.Event in main() for fast SIGTERM response
# PROMOTION_COOLDOWN_PATH removed — cooldown now managed by research/promoter.py


def _write_heartbeat(
    status: str,
    strategy: str = "",
    experiments: int = 0,
    kept: int = 0,
    session_start: float = 0,
    activity: str = "",
    detail: str = "",
    param: str = "",
    param_value: str = "",
    candidates: int = 0,
    last_result: str = "",
    last_delta: float = 0.0,
) -> None:
    try:
        HEARTBEAT_PATH.write_text(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "pid": os.getpid(),
            "strategy": strategy,
            "experiments_total": experiments,
            "experiments_kept": kept,
            "uptime_s": round(time.time() - session_start, 0) if session_start else 0,
            "activity": activity,
            "detail": detail,
            "param": param,
            "param_value": str(param_value),
            "candidates": candidates,
            "last_result": last_result,
            "last_delta": round(last_delta, 4),
        }, indent=2))
    except OSError:
        pass


def _send_telegram(message: str, level=None, category: str = "general") -> None:
    """Best-effort smart Telegram notification.

    Uses SmartNotifier for rate limiting + batching.
    """
    try:
        from utils.telegram import notify, INFO
        if level is None:
            level = INFO
        notify(message, level=level, category=category)
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)


def _should_stop() -> bool:
    """Check for graceful stop signal (file or threading event)."""
    if _stop_event is not None and _stop_event.is_set():
        return True
    return STOP_PATH.exists()


# ─── Combined Test & Promotion ────────────────────────────────────────────────


def _test_combined(
    session: "ResearchSession",
    improved_params: dict,
    strategy_name: str,
) -> bool:
    """Test if improved params hurt the combined portfolio.

    Runs combined portfolio test with the improved params and checks whether
    the portfolio-level Sharpe degrades by more than 0.02.

    Args:
        session:         Active ResearchSession (provides market context).
        improved_params: Merged strategy params after improvement.
        strategy_name:   Strategy being tested.

    Returns:
        True  — safe to keep (Sharpe doesn't degrade by > 0.02).
        False — revert; combined portfolio suffers.
    """
    try:
        result = combined_test(strategy_name, improved_params, session.market)
        delta_sharpe = result.get("delta", {}).get("sharpe", 0.0)
        if delta_sharpe < -0.02:
            logger.info(
                "🚫 Combined test FAILED for %s: portfolio Sharpe delta %.4f (< -0.02)",
                strategy_name, delta_sharpe,
            )
            return False
        logger.info(
            "✅ Combined test PASSED for %s: portfolio Sharpe delta %+.4f",
            strategy_name, delta_sharpe,
        )
        return True
    except Exception as e:
        logger.warning(
            "Combined test errored for %s: %s — assuming safe to keep.",
            strategy_name, e,
        )
        return True  # Don't block keep on infrastructure failures


# _check_promotions() removed — replaced by research/promoter.py::auto_promote()
# Sweep cycle now calls auto_promote() directly for each improved strategy result.


# ─── Parallel Backtest Workers ────────────────────────────────────────────────

# Module-level state for fork-inherited worker processes (copy-on-write).
# Set BEFORE creating ProcessPoolExecutor so forked children inherit it.
_worker_data = None
_worker_config = None


def _init_parallel_state(data: dict, config: dict) -> None:
    """Set module-level state that forked workers will inherit (COW)."""
    global _worker_data, _worker_config
    _worker_data = data
    _worker_config = config


def _backtest_worker(args: tuple) -> dict:
    """Run a single backtest in a worker process.

    Args: (strategy_name, params_override)
    Returns: metrics dict
    """
    strategy_name, params = args
    import time as _time
    from scripts.strategy_evaluator import make_config_with_strategy, run_backtest
    t0 = _time.time()
    cfg = make_config_with_strategy(
        _worker_config, strategy_name,
        params_override=params, solo=True,
    )
    metrics = run_backtest(cfg, _worker_data)
    metrics["runtime_s"] = round(_time.time() - t0, 1)
    return metrics


# ─── Sweep Logic ─────────────────────────────────────────────────────────────


def sweep_strategy(
    session: ResearchSession,
    param_grid: Dict[str, list],
    max_consecutive_fails: int = 5,
    workers: int = 1,
) -> Dict[str, Any]:
    """Sweep all parameters for one strategy, optionally in parallel.

    For each parameter:
    1. Run ALL candidate values in parallel (against the same baseline)
    2. Collect results, pick the BEST improvement
    3. If improved → keep, advance baseline
    4. Move to next parameter (which runs against the updated baseline)

    With workers=1, runs sequentially (original behavior).
    With workers>1, batches all values per parameter into parallel backtests.

    Args:
        session:              Active ResearchSession with baseline already set.
        param_grid:           {param_name: [values to try]}
        max_consecutive_fails: Stop after N params in a row with no improvement.
        workers:              Number of parallel backtest workers (default 1).

    Returns:
        {"experiments_run": int, "experiments_kept": int, "improvements": [...]}
    """
    if workers > 1:
        return _sweep_strategy_parallel(
            session, param_grid, max_consecutive_fails, workers,
        )
    return _sweep_strategy_sequential(
        session, param_grid, max_consecutive_fails,
    )


def _sweep_strategy_sequential(
    session: ResearchSession,
    param_grid: Dict[str, list],
    max_consecutive_fails: int = 5,
) -> Dict[str, Any]:
    """Original sequential sweep — one experiment at a time."""
    total_run = 0
    total_kept = 0
    consecutive_fails = 0
    improvements = []

    current_params = dict(session._best_params)

    for param_name, values in param_grid.items():
        if _should_stop():
            break

        current_value = current_params.get(param_name)

        for value in values:
            if _should_stop():
                break

            if value == current_value:
                continue

            description = f"{param_name}: {current_value}→{value}"
            logger.info("Trying: %s", description)

            try:
                result = session.experiment({param_name: value}, description)
            except Exception as e:
                logger.error("Experiment failed: %s — %s", description, e)
                total_run += 1
                consecutive_fails += 1
                continue

            total_run += 1

            if result["recommendation"] == "keep":
                # Combined portfolio gate: revert if portfolio-level Sharpe degrades
                merged_params = session._last_experiment["merged_params"]
                if not _test_combined(session, merged_params, session.strategy):
                    session.discard()
                    consecutive_fails += 1
                    logger.info(
                        "❌ DISCARD (combined test failed): %s", description,
                    )
                else:
                    session.keep()
                    total_kept += 1
                    consecutive_fails = 0
                    current_value = value
                    current_params = dict(session._best_params)
                    improvements.append({
                        "param": param_name,
                        "value": value,
                        "delta_sharpe": result["delta"]["sharpe"],
                        "new_sharpe": result["metrics"]["sharpe"],
                    })
                    logger.info(
                        "✅ KEPT: %s (Sharpe %+.4f → %.4f)",
                        description,
                        result["delta"]["sharpe"],
                        result["metrics"]["sharpe"],
                    )
            else:
                session.discard()
                consecutive_fails += 1
                logger.info("❌ DISCARD: %s", description)

            if consecutive_fails >= max_consecutive_fails:
                logger.info(
                    "Stopping %s — %d consecutive fails",
                    session.strategy, max_consecutive_fails,
                )
                break

        if consecutive_fails >= max_consecutive_fails:
            break

    return {
        "experiments_run": total_run,
        "experiments_kept": total_kept,
        "improvements": improvements,
    }


def _sweep_strategy_parallel(
    session: ResearchSession,
    param_grid: Dict[str, list],
    max_consecutive_fails: int = 5,
    workers: int = 6,
) -> Dict[str, Any]:
    """Parallel sweep — batch all values per parameter, keep the best.

    For each parameter, ALL candidate values run simultaneously against
    the current baseline. The best improvement (if any) is kept, then
    the next parameter runs against the updated baseline.

    This is strictly better than sequential: it finds the optimal value
    per parameter instead of the first improvement.
    """
    total_run = 0
    total_kept = 0
    consecutive_param_fails = 0
    improvements = []
    current_params = dict(session._best_params)

    # Set up fork-inherited state for workers
    _init_parallel_state(session._data, session._config)

    with ProcessPoolExecutor(max_workers=workers) as pool:
        for param_name, values in param_grid.items():
            if _should_stop():
                break

            current_value = current_params.get(param_name)
            candidates = [v for v in values if v != current_value]
            if not candidates:
                continue

            n_workers = min(workers, len(candidates))
            logger.info(
                "⚡ Parallel sweep: %s — %d values on %d workers",
                param_name, len(candidates), n_workers,
            )

            # Heartbeat: testing parameter
            _write_heartbeat(
                "running", session.strategy,
                total_run, total_kept, 0,
                activity="testing",
                detail=f"Testing {param_name}",
                param=param_name,
                candidates=len(candidates),
            )

            # Build tasks: (strategy_name, full merged params)
            tasks = []
            for v in candidates:
                merged = {**current_params, param_name: v}
                tasks.append((session.strategy, merged))

            # Submit all
            future_to_value = {}
            for task, value in zip(tasks, candidates):
                future = pool.submit(_backtest_worker, task)
                future_to_value[future] = value

            # Collect results
            results = []
            for future in as_completed(future_to_value):
                value = future_to_value[future]
                try:
                    metrics = future.result()
                    verdict = keep_or_discard(session._baseline_metrics, metrics)
                    results.append({
                        "value": value,
                        "metrics": metrics,
                        "verdict": verdict,
                    })
                except Exception as e:
                    logger.error(
                        "Worker failed for %s=%s: %s", param_name, value, e,
                    )
                total_run += 1

            # Find best keeper
            keepers = [r for r in results if r["verdict"]["decision"] == "keep"]

            if keepers:
                best = max(keepers, key=lambda r: r["verdict"]["delta_sharpe"])
                best_value = best["value"]
                merged = {**current_params, param_name: best_value}

                # Combined portfolio gate: revert if portfolio-level Sharpe degrades
                if not _test_combined(session, merged, session.strategy):
                    logger.info(
                        "❌ DISCARD (combined test failed): %s=%s",
                        param_name, best_value,
                    )
                    # Log all candidates as discards
                    for r in results:
                        _append_result(
                            session.strategy, r["metrics"],
                            f"{param_name}={r['value']}",
                            "discard",
                            f"{param_name}: {current_value}→{r['value']} (combined-fail)",
                        )
                    consecutive_param_fails += 1
                    total_run += len(results)  # already added below but not here yet
                    # Skip to next param
                    if consecutive_param_fails >= max_consecutive_fails:
                        logger.info(
                            "Stopping %s — %d params in a row with no improvement",
                            session.strategy, consecutive_param_fails,
                        )
                        break
                    continue

                # Update session state
                session._best_params = merged
                session._baseline_metrics = best["metrics"]
                session._experiments_run += len(results)
                session._experiments_kept += 1
                total_kept += 1
                consecutive_param_fails = 0
                current_params = dict(merged)

                # Save best
                save_best(
                    session.strategy, session.market,
                    merged, best["metrics"],
                    f"{param_name}={best_value}",
                )

                # Log the kept result
                _append_result(
                    session.strategy, best["metrics"],
                    f"{param_name}={best_value}", "keep",
                    f"{param_name}: {current_value}→{best_value} "
                    f"(best of {len(candidates)})",
                )
                _append_journal(
                    session.strategy, session.market, best["metrics"],
                    "keep",
                    f"{param_name}: {current_value}→{best_value}",
                    {param_name: best_value},
                )
                _increment_run_count(session.strategy)

                improvements.append({
                    "param": param_name,
                    "value": best_value,
                    "delta_sharpe": best["verdict"]["delta_sharpe"],
                    "new_sharpe": best["metrics"]["sharpe"],
                })

                logger.info(
                    "✅ KEPT: %s=%s (Sharpe %+.4f → %.4f, best of %d)",
                    param_name, best_value,
                    best["verdict"]["delta_sharpe"],
                    best["metrics"]["sharpe"],
                    len(candidates),
                )

                # Heartbeat: kept result
                _write_heartbeat(
                    "running", session.strategy,
                    total_run, total_kept, 0,
                    activity="kept",
                    detail=f"{param_name}={best_value}",
                    param=param_name,
                    param_value=str(best_value),
                    last_result="kept",
                    last_delta=best["verdict"]["delta_sharpe"],
                )

                # Brain: record kept result
                try:
                    _delta = best["verdict"]["delta_sharpe"]
                    _new_s = best["metrics"]["sharpe"]
                    update_strategy(
                        session.strategy, best["metrics"], merged,
                        description=f"{param_name}={best_value}",
                    )
                    record_param_result(
                        session.strategy, param_name, best_value,
                        current_value, True, _delta, _new_s,
                    )
                    record_experiment(
                        f"ar-{time.strftime('%Y%m%d_%H%M%S')}",
                        session.strategy, param_name, best_value,
                        current_value, True, best["metrics"], _delta,
                    )
                    if _brain_session:
                        _brain_session.add_result(
                            session.strategy, param_name, best_value,
                            True, _delta, _new_s,
                        )
                except Exception as _be:
                    logger.warning("Brain write failed: %s", _be)

                # Log discards
                for r in results:
                    if r is not best:
                        _append_result(
                            session.strategy, r["metrics"],
                            f"{param_name}={r['value']}",
                            "discard",
                            f"{param_name}: {current_value}→{r['value']}",
                        )
                        # Brain: record discarded params
                        try:
                            record_param_result(
                                session.strategy, param_name, r["value"],
                                current_value, False,
                                r["verdict"]["delta_sharpe"],
                                r["metrics"].get("sharpe", 0),
                            )
                        except Exception:
                            pass
            else:
                # All failed — log discards
                consecutive_param_fails += 1
                for r in results:
                    _append_result(
                        session.strategy, r["metrics"],
                        f"{param_name}={r['value']}",
                        "discard",
                        f"{param_name}: {current_value}→{r['value']}",
                    )
                    # Brain: record discarded params
                    try:
                        record_param_result(
                            session.strategy, param_name, r["value"],
                            current_value, False,
                            r["verdict"]["delta_sharpe"],
                            r["metrics"].get("sharpe", 0),
                        )
                    except Exception:
                        pass
                logger.info(
                    "❌ No improvement for %s (%d values tried)",
                    param_name, len(results),
                )

                # Heartbeat: discarded
                _write_heartbeat(
                    "running", session.strategy,
                    total_run, total_kept, 0,
                    activity="discarded",
                    detail=f"{param_name} ({len(results)} values tried)",
                    param=param_name,
                    last_result="discarded",
                )

            if consecutive_param_fails >= max_consecutive_fails:
                logger.info(
                    "Stopping %s — %d params in a row with no improvement",
                    session.strategy, consecutive_param_fails,
                )
                break

    return {
        "experiments_run": total_run,
        "experiments_kept": total_kept,
        "improvements": improvements,
    }


# Module-level brain session — set by run_sweep, read by sweep_strategy
_brain_session: Optional[SweepSession] = None


def run_sweep(
    strategies: Optional[List[str]] = None,
    market: str = "sp500",
    top_n: Optional[int] = None,
    max_consecutive_fails: int = 5,
    cycles: int = 0,
    workers: int = 1,
    max_runtime: int = 0,
) -> None:
    """Run the full autonomous sweep loop.

    Iterates through strategies, sweeping parameters for each.
    On each cycle through all strategies, it starts from the top
    of the priority list again (values that failed before might
    work after other params changed).

    Args:
        strategies:            List of strategy names, or None for all.
        market:                Market ID.
        top_n:                 Ticker subset size (None = full universe).
        max_consecutive_fails: Stop a strategy after this many discards.
        cycles:                Number of full cycles (0 = infinite).
        workers:               Parallel backtest workers (1 = sequential).
        max_runtime:           Max seconds to run (0 = unlimited).
    """
    global _brain_session
    strategy_list = strategies or STRATEGY_ORDER
    session_start = time.time()
    total_experiments = 0
    total_kept = 0
    cycle_num = 0

    # Create brain sweep session
    _brain_session = SweepSession()
    deadline = (session_start + max_runtime) if max_runtime > 0 else 0

    # Clean up stale claims from crashed previous sessions before starting.
    # Without this, experiments orphaned by a killed sweep stay "claimed" forever
    # because sweep.py doesn't use the research_daemon's periodic cleanup.
    try:
        from research.models import cleanup_stale_claims
        n_reset = cleanup_stale_claims(timeout_h=2.0)
        if n_reset:
            logger.info("Cleaned up %d stale claimed experiment(s) on startup", n_reset)
    except Exception as e:
        logger.warning("Stale claim cleanup failed: %s", e)

    # NOTE: No start notification here — parent (autoresearch.py) handles it.
    # sweep.py runs as a subprocess per-strategy, so sending "started" here
    # would spam 7× per cycle.

    while True:
        cycle_num += 1
        if cycles > 0 and cycle_num > cycles:
            break
        if _should_stop():
            logger.info("Stop signal received — exiting cleanly.")
            break
        if deadline and time.time() >= deadline:
            logger.info("Max runtime (%ds) reached — exiting cleanly.", max_runtime)
            break

        logger.info("=== Cycle %d ===", cycle_num)

        cycle_results: List[dict] = []

        for strategy_name in strategy_list:
            if _should_stop():
                break
            if deadline and time.time() >= deadline:
                logger.info("Max runtime reached mid-cycle — stopping.")
                break

            base_grid = PARAM_GRIDS.get(strategy_name, {})
            if not base_grid:
                logger.info("No param grid for %s — skipping.", strategy_name)
                continue

            # ── Staleness check — skip strategies with no wins recently ──
            if _PARAM_HISTORY_AVAILABLE:
                try:
                    staleness = get_strategy_staleness(strategy_name)
                    if staleness["is_stale"]:
                        logger.info(
                            "Skipping stale strategy %s — 0 wins in last %d "
                            "experiments (last win: %s). "
                            "Use --reset-stale to force re-test.",
                            strategy_name,
                            staleness["total_recent"],
                            staleness.get("last_win_date") or "never",
                        )
                        continue
                except Exception as _stale_exc:
                    logger.debug(
                        "Staleness check failed for %s: %s",
                        strategy_name, _stale_exc,
                    )

            # ── Load param history for result-aware grid expansion ────────
            _strategy_ph: Dict = {}
            if _PARAM_HISTORY_AVAILABLE:
                try:
                    _strategy_ph = build_strategy_param_history(strategy_name)
                    if _strategy_ph:
                        total_ph = sum(
                            sum(v["tests"] for v in stats.values())
                            for stats in _strategy_ph.values()
                        )
                        logger.debug(
                            "Loaded param history for %s: %d param×value records",
                            strategy_name, total_ph,
                        )
                except Exception as _ph_exc:
                    logger.debug(
                        "Param history load failed for %s: %s",
                        strategy_name, _ph_exc,
                    )

            # Expand grid with jittered values around current best
            # (prevents stalling when the fixed grid is exhausted)
            try:
                current_best_params = load_best(strategy_name, market).get("params", {})
            except Exception:
                current_best_params = {}
            if current_best_params:
                grid = expand_grid(
                    base_grid,
                    current_best_params,
                    n_jitter=3,
                    param_history=_strategy_ph or None,
                )
                n_base = sum(len(v) for v in base_grid.values())
                n_expanded = sum(len(v) for v in grid.values())
                if n_expanded > n_base:
                    logger.info(
                        "Grid expanded: %d → %d values (+%d jittered around best)",
                        n_base, n_expanded, n_expanded - n_base,
                    )
                elif n_expanded < n_base:
                    logger.info(
                        "Grid pruned: %d → %d values (-%d dead zones removed)",
                        n_base, n_expanded, n_base - n_expanded,
                    )
            else:
                grid = base_grid

            logger.info("--- Strategy: %s ---", strategy_name)
            _write_heartbeat(
                "running", strategy_name,
                total_experiments, total_kept, session_start,
                activity="loading", detail=f"Loading {strategy_name} data",
            )

            try:
                session = ResearchSession(strategy_name, market, top_n=top_n)
                _write_heartbeat(
                    "running", strategy_name,
                    total_experiments, total_kept, session_start,
                    activity="baseline", detail=f"Running baseline backtest",
                )
                session.baseline()
            except Exception as e:
                logger.error("Failed to init %s: %s", strategy_name, e)
                continue

            # Capture baseline Sharpe BEFORE sweeping for promotion tracking
            initial_sharpe = (session._baseline_metrics or {}).get("sharpe", 0.0)

            result = sweep_strategy(session, grid, max_consecutive_fails, workers)
            total_experiments += result["experiments_run"]
            total_kept += result["experiments_kept"]

            # Capture final Sharpe AFTER all improvements
            final_sharpe = (session._baseline_metrics or {}).get("sharpe", 0.0)

            # Collect for end-of-cycle promotion check
            cycle_results.append({
                "strategy": strategy_name,
                "initial_sharpe": initial_sharpe,
                "final_sharpe": final_sharpe,
                "improved_params": dict(session._best_params),
                "improvements": result["improvements"],
                "market": market,
            })

            # Log summary
            summary = session.summary()
            logger.info(summary)

            # Queue improvements for digest (batched, not spammed per-strategy)
            if result["improvements"]:
                imp_lines = []
                for imp in result["improvements"]:
                    imp_lines.append(
                        f"  • {imp['param']}={imp['value']} "
                        f"(Sharpe {imp['delta_sharpe']:+.4f} → {imp['new_sharpe']:.4f})"
                    )
                _send_telegram(
                    f"📈 <b>{strategy_name}</b> improved! "
                    f"{result['experiments_run']} run, {result['experiments_kept']} kept\n"
                    + "\n".join(imp_lines),
                    category="improvement",
                )

        # Cycle complete
        elapsed_h = (time.time() - session_start) / 3600
        logger.info(
            "Cycle %d complete — %d experiments, %d kept, %.1f hours elapsed.",
            cycle_num, total_experiments, total_kept, elapsed_h,
        )

        # Auto-promote strategies that improved beyond threshold
        if cycle_results:
            from research.promoter import auto_promote
            for cr in cycle_results:
                if cr.get("improvements"):
                    try:
                        auto_promote(
                            strategy=cr["strategy"],
                            improved_params=cr.get("improved_params", {}),
                            initial_sharpe=cr.get("initial_sharpe", 0.0),
                            final_sharpe=cr.get("final_sharpe", 0.0),
                            improvements=cr["improvements"],
                            market=cr.get("market", market),
                        )
                    except Exception as _promo_exc:
                        logger.warning(
                            "auto_promote failed for %s: %s",
                            cr.get("strategy", "?"), _promo_exc,
                        )

        # Between cycles: log leaderboard
        logger.info(leaderboard(market))

    # Flush brain session + rebuild indexes
    try:
        _write_heartbeat(
            "running", "",
            total_experiments, total_kept, session_start,
            activity="writing", detail="Updating brain indexes",
        )
        runtime = time.time() - session_start
        if _brain_session:
            _brain_session.flush(runtime_s=runtime)
        update_state(
            last_sweep_session=_brain_session.session_id if _brain_session else "?",
            last_sweep_runtime_s=round(runtime, 1),
            total_experiments=total_experiments,
            total_kept=total_kept,
        )
        rebuild_all_indexes()
        logger.info("Brain indexes rebuilt.")
    except Exception as e:
        logger.warning("Brain flush failed: %s", e)

    # Final heartbeat — no stop notification here (parent handles it)
    _write_heartbeat(
        "stopped", "", total_experiments, total_kept, session_start,
    )


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Atlas Autoresearch Sweeper — 24/7 parameter optimization",
    )
    parser.add_argument(
        "--strategy", type=str, default=None,
        help="Single strategy to sweep (default: all in priority order)",
    )
    parser.add_argument(
        "--market", type=str, default="sp500",
        help="Market ID (default: sp500)",
    )
    parser.add_argument(
        "--top-n", type=int, default=None,
        help="Use top N tickers by volume for faster iterations (default: all)",
    )
    parser.add_argument(
        "--max-fails", type=int, default=5,
        help="Stop a strategy after N consecutive discards (default: 5)",
    )
    parser.add_argument(
        "--cycles", type=int, default=0,
        help="Number of full cycles, 0=infinite (default: 0)",
    )
    parser.add_argument(
        "--workers", type=int, default=None,
        help="Parallel backtest workers (default: ncpus-2, min 1)",
    )
    parser.add_argument(
        "--max-runtime", type=int, default=0,
        help="Max runtime in seconds (0=unlimited, default: 0)",
    )
    parser.add_argument(
        "--log-file", type=str, default=None,
        help="Log file path (default: stdout)",
    )
    parser.add_argument(
        "--reset-stale", action="store_true", default=False,
        help=(
            "Clear staleness tracking so all strategies get re-tested this cycle. "
            "Writes brain/staleness_reset.json; get_strategy_staleness() will "
            "ignore experiments older than the reset timestamp."
        ),
    )
    args = parser.parse_args()

    # Logging — force-configure the root logger.
    # Cannot use basicConfig() because module-level imports (e.g. strategy_evaluator)
    # may have already called setup_logging(), making basicConfig() a silent no-op.
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Clear any handlers set by imported modules (e.g. utils.logging_config)
    root.handlers.clear()
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    root.addHandler(stdout_handler)
    if args.log_file:
        file_handler = logging.FileHandler(args.log_file)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)

    # Signal handling — use both a file AND a threading event for fast response.
    # The file persists across function boundaries; the event wakes up any
    # ProcessPoolExecutor.as_completed() call that's blocking.
    import threading
    global _stop_event
    _stop_event = threading.Event()

    def _shutdown(signum, frame):
        logger.info("Received signal %s — initiating shutdown.", signum)
        STOP_PATH.touch()
        _stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Clean up any stale stop signal
    STOP_PATH.unlink(missing_ok=True)

    # Default workers: ncpus - 2 (leave room for system + agent)
    workers = args.workers
    if workers is None:
        workers = max(1, os.cpu_count() - 2)

    # Handle --reset-stale: clear staleness tracking before sweep starts
    if args.reset_stale:
        if _PARAM_HISTORY_AVAILABLE:
            try:
                reset_staleness()
                logger.info(
                    "Staleness tracking reset — all strategies will be re-tested "
                    "this cycle (brain/staleness_reset.json updated)."
                )
            except Exception as _rst_exc:
                logger.warning("Failed to reset staleness: %s", _rst_exc)
        else:
            logger.warning(
                "--reset-stale specified but param_history module is not available."
            )

    strategies = [args.strategy] if args.strategy else None
    run_sweep(
        strategies=strategies,
        market=args.market,
        top_n=args.top_n,
        max_consecutive_fails=args.max_fails,
        cycles=args.cycles,
        workers=workers,
        max_runtime=args.max_runtime,
    )


if __name__ == "__main__":
    main()
