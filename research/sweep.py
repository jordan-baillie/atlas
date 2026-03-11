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
)

logger = logging.getLogger("autoresearch.sweep")

# ─── Parameter Grids ─────────────────────────────────────────────────────────

# Each strategy has a grid of parameters to sweep.
# Only scalar params — nested dicts handled separately.
# Values are ordered from most likely to least likely improvement.

PARAM_GRIDS: Dict[str, Dict[str, list]] = {
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
]

# ─── Heartbeat / Signals ─────────────────────────────────────────────────────

HEARTBEAT_PATH = Path("/tmp/autoresearch-heartbeat.json")
STOP_PATH = Path("/tmp/autoresearch-stop")


def _write_heartbeat(
    status: str,
    strategy: str = "",
    experiments: int = 0,
    kept: int = 0,
    session_start: float = 0,
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
    """Check for graceful stop signal."""
    return STOP_PATH.exists()


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

                # Update session state
                merged = {**current_params, param_name: best_value}
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

                # Log discards
                for r in results:
                    if r is not best:
                        _append_result(
                            session.strategy, r["metrics"],
                            f"{param_name}={r['value']}",
                            "discard",
                            f"{param_name}: {current_value}→{r['value']}",
                        )
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
                logger.info(
                    "❌ No improvement for %s (%d values tried)",
                    param_name, len(results),
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
    strategy_list = strategies or STRATEGY_ORDER
    session_start = time.time()
    total_experiments = 0
    total_kept = 0
    cycle_num = 0
    deadline = (session_start + max_runtime) if max_runtime > 0 else 0

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

        for strategy_name in strategy_list:
            if _should_stop():
                break
            if deadline and time.time() >= deadline:
                logger.info("Max runtime reached mid-cycle — stopping.")
                break

            grid = PARAM_GRIDS.get(strategy_name, {})
            if not grid:
                logger.info("No param grid for %s — skipping.", strategy_name)
                continue

            logger.info("--- Strategy: %s ---", strategy_name)
            _write_heartbeat(
                "running", strategy_name,
                total_experiments, total_kept, session_start,
            )

            try:
                session = ResearchSession(strategy_name, market, top_n=top_n)
                session.baseline()
            except Exception as e:
                logger.error("Failed to init %s: %s", strategy_name, e)
                continue

            result = sweep_strategy(session, grid, max_consecutive_fails, workers)
            total_experiments += result["experiments_run"]
            total_kept += result["experiments_kept"]

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

        # Between cycles: log leaderboard
        logger.info(leaderboard(market))

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
    args = parser.parse_args()

    # Logging
    handlers = [logging.StreamHandler(sys.stdout)]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)

    # Signal handling
    def _shutdown(signum, frame):
        logger.info("Received signal %s — creating stop file.", signum)
        STOP_PATH.touch()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Clean up any stale stop signal
    STOP_PATH.unlink(missing_ok=True)

    # Default workers: ncpus - 2 (leave room for system + agent)
    workers = args.workers
    if workers is None:
        workers = max(1, os.cpu_count() - 2)

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
