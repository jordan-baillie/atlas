#!/usr/bin/env python3
"""
Backtest all 7 production strategies against each ETF universe.
Produces an eligibility matrix for regime-based universe activation.

Usage:
    python3 scripts/backtest_universes.py
    python3 scripts/backtest_universes.py --universes sector_etfs,treasury_etfs
    python3 scripts/backtest_universes.py --strategies momentum_breakout,mean_reversion
    python3 scripts/backtest_universes.py --universes sector_etfs --strategies trend_following
"""
import sys
import json
import copy
import time
import logging
import argparse
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import pandas as pd
import numpy as np

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("backtest_universes")

# ── Constants ────────────────────────────────────────────────────────────────

ETF_UNIVERSES = [
    "sector_etfs",
    "treasury_etfs",
    "commodity_etfs",
    "gold_etfs",
    "defensive_etfs",
]

PRODUCTION_STRATEGIES = [
    "momentum_breakout",
    "mean_reversion",
    "trend_following",
    "opening_gap",
    "sector_rotation",
    "short_term_mr",
    "connors_rsi2",
]

START_DATE = "2019-01-01"

# Viability thresholds
MIN_SHARPE = 0.3
MIN_TRADES = 20

# Output file
OUTPUT_JSON = PROJECT / "data" / "universe_backtest_results.json"


# ── Strategy registry ────────────────────────────────────────────────────────

def get_strategy_registry() -> Dict[str, Any]:
    """Return a mapping of strategy_name -> strategy_class."""
    from strategies.momentum_breakout import MomentumBreakout
    from strategies.mean_reversion import MeanReversion
    from strategies.trend_following import TrendFollowing
    from strategies.opening_gap import OpeningGap
    from strategies.sector_rotation import SectorRotation
    from strategies.short_term_mr import ShortTermMR
    from strategies.connors_rsi2 import ConnorsRSI2

    return {
        "momentum_breakout": MomentumBreakout,
        "mean_reversion": MeanReversion,
        "trend_following": TrendFollowing,
        "opening_gap": OpeningGap,
        "sector_rotation": SectorRotation,
        "short_term_mr": ShortTermMR,
        "connors_rsi2": ConnorsRSI2,
    }


# ── Config builder ────────────────────────────────────────────────────────────

def build_etf_config(base_config: Dict[str, Any]) -> Dict[str, Any]:
    """Build a backtest config suitable for ETF universes.

    Starts from the SP500 production config and adjusts:
    - starting_equity = 10000
    - max_open_positions = 3  (ETF universes are smaller, 4–11 tickers)
    - trading.mode = paper   (safety: don't accidentally touch live)
    - drops live_safety constraints irrelevant to backtesting
    """
    cfg = copy.deepcopy(base_config)

    cfg["risk"]["starting_equity"] = 10000
    cfg["risk"]["max_open_positions"] = 3

    # Ensure backtesting mode (not live)
    cfg["trading"]["mode"] = "paper"
    cfg["trading"]["live_enabled"] = False
    cfg["trading"]["approval_required"] = False

    # Remove live safety guardrails (irrelevant in backtest)
    cfg["trading"].pop("live_safety", None)

    return cfg


# ── Single backtest worker ────────────────────────────────────────────────────

def _run_one(
    strategy_name: str,
    universe_name: str,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Run one strategy × universe backtest. Returns result dict.

    This function is designed to run in a subprocess (ProcessPoolExecutor),
    so it must be importable at module level and must not share state.
    """
    result: Dict[str, Any] = {
        "strategy": strategy_name,
        "universe": universe_name,
        "status": "error",
        "sharpe": None,
        "total_trades": None,
        "max_drawdown_pct": None,
        "profit_factor": None,
        "cagr": None,
        "win_rate": None,
        "error": None,
    }

    try:
        from universe.builder import build_from_definition
        from backtest.engine import BacktestEngine

        registry = get_strategy_registry()
        if strategy_name not in registry:
            result["error"] = f"Unknown strategy: {strategy_name}"
            return result

        # Load universe data
        data = build_from_definition(universe_name, start_date=START_DATE)
        if not data:
            result["error"] = f"No data returned for universe {universe_name!r}"
            return result

        # Instantiate strategy with the per-strategy config section
        strategy_cfg = config.get("strategies", {}).get(strategy_name, {})
        strategy_instance = registry[strategy_name](config)

        # Run backtest
        engine = BacktestEngine(config)
        bt_result = engine.run_walkforward(data, [strategy_instance])

        metrics = bt_result.metrics
        result.update(
            {
                "status": "ok",
                "sharpe": round(metrics.get("sharpe", 0.0), 4),
                "total_trades": int(metrics.get("total_trades", 0)),
                "max_drawdown_pct": round(metrics.get("max_drawdown", 0.0) * 100, 2),
                "profit_factor": round(metrics.get("profit_factor", 0.0), 4),
                "cagr": round(metrics.get("cagr", 0.0) * 100, 2),
                "win_rate": round(metrics.get("win_rate", 0.0) * 100, 2),
            }
        )

    except Exception as exc:
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc()

    return result


# ── Parallel execution ────────────────────────────────────────────────────────

def run_all_backtests(
    strategies: List[str],
    universes: List[str],
    config: Dict[str, Any],
    max_workers: int = 4,
) -> List[Dict[str, Any]]:
    """Run all strategy × universe combinations, in parallel where safe.

    BacktestEngine instances share no global state — they're safe to
    parallelise. We use ProcessPoolExecutor to sidestep the GIL for
    compute-heavy metric calculations.
    """
    combos: List[Tuple[str, str]] = [
        (s, u) for s in strategies for u in universes
    ]
    total = len(combos)
    print(f"\nRunning {total} backtests ({len(strategies)} strategies × {len(universes)} universes)…\n")

    results: List[Dict[str, Any]] = []
    completed = 0

    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        future_to_combo = {
            pool.submit(_run_one, s, u, config): (s, u)
            for (s, u) in combos
        }

        for future in as_completed(future_to_combo):
            s, u = future_to_combo[future]
            completed += 1
            try:
                res = future.result()
            except Exception as exc:
                res = {
                    "strategy": s,
                    "universe": u,
                    "status": "error",
                    "error": str(exc),
                    "sharpe": None,
                    "total_trades": None,
                    "max_drawdown_pct": None,
                    "profit_factor": None,
                    "cagr": None,
                    "win_rate": None,
                }

            results.append(res)
            status_icon = "✅" if res["status"] == "ok" else "❌"
            sharpe_str = f"Sharpe={res['sharpe']:.3f}" if res["sharpe"] is not None else "no signals"
            trades_str = f"Trades={res['total_trades']}" if res["total_trades"] is not None else ""
            err_str = f" [{res.get('error', '')}]" if res["status"] != "ok" else ""
            print(
                f"  [{completed:2d}/{total}] {status_icon} {s:<22} × {u:<18} "
                f"{sharpe_str}  {trades_str}{err_str}"
            )

    return results


# ── Display ───────────────────────────────────────────────────────────────────

def print_matrix(results: List[Dict[str, Any]], strategies: List[str], universes: List[str]) -> None:
    """Print the Sharpe matrix to the console."""
    # Build lookup
    lookup: Dict[Tuple[str, str], Optional[float]] = {}
    for r in results:
        lookup[(r["strategy"], r["universe"])] = r.get("sharpe")

    # Column widths
    strat_w = max(len(s) for s in strategies) + 2
    col_w = 16

    header = f"\n{'Strategy':<{strat_w}}" + "".join(f"{u:>{col_w}}" for u in universes)
    print("=" * len(header))
    print("=== Strategy × Universe Sharpe Matrix ===")
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    for s in strategies:
        row = f"{s:<{strat_w}}"
        for u in universes:
            v = lookup.get((s, u))
            if v is None:
                cell = "  n/a"
            else:
                cell = f"{v:+.3f}"
            row += f"{cell:>{col_w}}"
        print(row)

    print("-" * len(header))


def print_viable(viable: List[Dict[str, Any]]) -> None:
    """Print viable combinations."""
    print(f"\n=== Viable Combinations (Sharpe > {MIN_SHARPE}, Trades > {MIN_TRADES}) ===")
    if not viable:
        print("  (none found — try relaxing thresholds)")
        return
    for v in sorted(viable, key=lambda x: x["sharpe"], reverse=True):
        print(
            f"  ✅ {v['strategy']:<22} × {v['universe']:<18}  "
            f"Sharpe={v['sharpe']:.3f}  "
            f"Trades={v['trades']}  "
            f"MaxDD={v['max_drawdown_pct']:.1f}%  "
            f"CAGR={v['cagr']:.1f}%  "
            f"WinRate={v['win_rate']:.1f}%"
        )


# ── Save ──────────────────────────────────────────────────────────────────────

def save_results(
    results: List[Dict[str, Any]],
    viable: List[Dict[str, Any]],
    strategies: List[str],
    universes: List[str],
) -> None:
    """Persist results to JSON."""
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "start_date": START_DATE,
        "strategies": strategies,
        "universes": universes,
        "viability_thresholds": {
            "min_sharpe": MIN_SHARPE,
            "min_trades": MIN_TRADES,
        },
        "results": results,
        "viable": viable,
        "summary": {
            "total_combinations": len(results),
            "successful": sum(1 for r in results if r["status"] == "ok"),
            "viable_count": len(viable),
            "error_count": sum(1 for r in results if r["status"] == "error"),
        },
    }
    with open(OUTPUT_JSON, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    print(f"\nResults saved to {OUTPUT_JSON}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest all 7 production strategies against each ETF universe.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--universes",
        type=str,
        default=None,
        help=f"Comma-separated list of universes (default: all ETF universes). "
             f"Options: {', '.join(ETF_UNIVERSES)}",
    )
    parser.add_argument(
        "--strategies",
        type=str,
        default=None,
        help=f"Comma-separated list of strategies (default: all 7 production strategies). "
             f"Options: {', '.join(PRODUCTION_STRATEGIES)}",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel worker processes (default: 4).",
    )
    parser.add_argument(
        "--sequential",
        action="store_true",
        help="Run backtests sequentially instead of in parallel (safer for debugging).",
    )
    return parser.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    # Resolve strategy and universe lists
    universes = (
        [u.strip() for u in args.universes.split(",")]
        if args.universes
        else ETF_UNIVERSES
    )
    strategies = (
        [s.strip() for s in args.strategies.split(",")]
        if args.strategies
        else PRODUCTION_STRATEGIES
    )

    # Validate
    for u in universes:
        if u not in ETF_UNIVERSES:
            print(f"ERROR: Unknown universe {u!r}. Valid: {ETF_UNIVERSES}")
            sys.exit(1)
    for s in strategies:
        if s not in PRODUCTION_STRATEGIES:
            print(f"ERROR: Unknown strategy {s!r}. Valid: {PRODUCTION_STRATEGIES}")
            sys.exit(1)

    print(f"Universes  : {universes}")
    print(f"Strategies : {strategies}")
    print(f"Start date : {START_DATE}")
    print(f"Workers    : {'sequential' if args.sequential else args.workers}")

    # Load and adapt config
    from utils.config import get_active_config
    base_config = get_active_config("sp500")
    config = build_etf_config(base_config)

    t0 = time.time()

    if args.sequential:
        results: List[Dict[str, Any]] = []
        total = len(strategies) * len(universes)
        for i, (s, u) in enumerate(
            (s, u) for s in strategies for u in universes
        ):
            print(f"  [{i+1:2d}/{total}] {s} × {u} …", end="", flush=True)
            res = _run_one(s, u, config)
            results.append(res)
            if res["status"] == "ok":
                print(f" Sharpe={res['sharpe']:.3f}, Trades={res['total_trades']}")
            else:
                print(f" ❌ {res.get('error', 'unknown error')}")
    else:
        results = run_all_backtests(strategies, universes, config, max_workers=args.workers)

    elapsed = time.time() - t0
    print(f"\nCompleted {len(results)} backtests in {elapsed:.1f}s")

    # Identify viable combinations
    viable = [
        {
            "strategy": r["strategy"],
            "universe": r["universe"],
            "sharpe": r["sharpe"],
            "trades": r["total_trades"],
            "max_drawdown_pct": r["max_drawdown_pct"],
            "cagr": r["cagr"],
            "win_rate": r["win_rate"],
            "profit_factor": r["profit_factor"],
        }
        for r in results
        if r["status"] == "ok"
        and r["sharpe"] is not None
        and r["sharpe"] > MIN_SHARPE
        and r["total_trades"] is not None
        and r["total_trades"] > MIN_TRADES
    ]

    # Display
    print_matrix(results, strategies, universes)
    print_viable(viable)
    save_results(results, viable, strategies, universes)

    # Exit summary
    ok = sum(1 for r in results if r["status"] == "ok")
    errors = sum(1 for r in results if r["status"] == "error")
    print(
        f"\nSummary: {ok} ok, {errors} errors, {len(viable)} viable "
        f"(Sharpe>{MIN_SHARPE} & Trades>{MIN_TRADES})"
    )


if __name__ == "__main__":
    main()
