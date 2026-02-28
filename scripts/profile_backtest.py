#!/usr/bin/env python3
"""Atlas Backtest Profiler
=============================
Profile the backtest engine to identify performance bottlenecks.

Runs cProfile on a single full backtest pass and produces:
  1. Top 30 functions by cumulative time
  2. Top 30 by total (self) time
  3. Breakdown by module (strategies, engine, data, indicators)
  4. Summary with estimated optimization potential

Usage:
    python3 scripts/profile_backtest.py                      # default: sp500
    python3 scripts/profile_backtest.py --market asx
    python3 scripts/profile_backtest.py --sort tottime        # sort by self time
    python3 scripts/profile_backtest.py --output profile.prof # save raw profile
"""

import argparse
import cProfile
import io
import json
import pstats
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_data(market_id: str) -> dict:
    """Load market data for profiling."""
    from scripts.strategy_evaluator import load_market_data
    return load_market_data(market_id)


def run_backtest(config: dict, data: dict):
    """Run a single backtest pass (the thing we're profiling)."""
    from scripts.strategy_evaluator import run_backtest as _run
    return _run(config, data)


def categorize_function(filename: str) -> str:
    """Categorize a function by its module location."""
    if not filename:
        return "other"
    f = filename.replace("\\", "/")
    if "strategies/" in f:
        return "strategies"
    if "backtest/engine" in f:
        return "engine"
    if "backtest/metrics" in f:
        return "metrics"
    if "data/" in f:
        return "data"
    if "utils/" in f:
        return "utils"
    if "pandas" in f or "numpy" in f:
        return "pandas/numpy"
    if "scipy" in f:
        return "scipy"
    if "/python" in f.lower() or "<" in f:
        return "stdlib"
    return "other"


def analyze_profile(stats: pstats.Stats, total_time: float):
    """Analyze profile results and produce summary."""
    print("\n" + "=" * 70)
    print("ATLAS BACKTEST PROFILER RESULTS")
    print("=" * 70)
    print(f"Total wall time: {total_time:.1f}s")
    print()

    # Top 30 by cumulative time
    print("-" * 70)
    print("TOP 30 FUNCTIONS BY CUMULATIVE TIME")
    print("-" * 70)
    stream = io.StringIO()
    stats.stream = stream
    stats.sort_stats("cumulative")
    stats.print_stats(30)
    print(stream.getvalue())

    # Top 30 by self time
    print("-" * 70)
    print("TOP 30 FUNCTIONS BY SELF TIME (hotspots)")
    print("-" * 70)
    stream2 = io.StringIO()
    stats.stream = stream2
    stats.sort_stats("tottime")
    stats.print_stats(30)
    print(stream2.getvalue())

    # Module breakdown
    print("-" * 70)
    print("TIME BY MODULE")
    print("-" * 70)

    module_times = {}
    module_calls = {}
    for (filename, lineno, funcname), (cc, nc, tt, ct, callers) in stats.stats.items():
        cat = categorize_function(filename)
        module_times[cat] = module_times.get(cat, 0) + tt
        module_calls[cat] = module_calls.get(cat, 0) + nc

    sorted_mods = sorted(module_times.items(), key=lambda x: -x[1])
    for mod, t in sorted_mods:
        pct = (t / total_time * 100) if total_time > 0 else 0
        calls = module_calls.get(mod, 0)
        bar = "█" * int(pct / 2) + "░" * (50 - int(pct / 2))
        print(f"  {mod:15s} {t:8.2f}s  {pct:5.1f}%  {calls:>10,} calls  {bar}")

    # Strategy-level breakdown
    print()
    print("-" * 70)
    print("STRATEGY HOTSPOTS")
    print("-" * 70)

    strat_funcs = []
    for (filename, lineno, funcname), (cc, nc, tt, ct, callers) in stats.stats.items():
        if "strategies/" in (filename or ""):
            strat_funcs.append((funcname, filename.split("/")[-1], tt, nc))

    strat_funcs.sort(key=lambda x: -x[2])
    for funcname, fname, tt, nc in strat_funcs[:15]:
        pct = (tt / total_time * 100) if total_time > 0 else 0
        print(f"  {fname:30s} {funcname:25s} {tt:7.3f}s  {pct:5.1f}%  {nc:>8,} calls")

    # Recommendations
    print()
    print("-" * 70)
    print("OPTIMIZATION RECOMMENDATIONS")
    print("-" * 70)

    top_hotspot = sorted_mods[0] if sorted_mods else ("?", 0)
    strat_total = module_times.get("strategies", 0)
    engine_total = module_times.get("engine", 0)
    data_total = module_times.get("data", 0)
    metrics_total = module_times.get("metrics", 0)

    if strat_total > total_time * 0.4:
        print(f"  🔴 Strategies take {strat_total:.1f}s ({strat_total/total_time*100:.0f}%) — vectorize signal generation")
    if engine_total > total_time * 0.3:
        print(f"  🟡 Engine loop takes {engine_total:.1f}s ({engine_total/total_time*100:.0f}%) — consider caching position lookups")
    if data_total > total_time * 0.2:
        print(f"  🟡 Data I/O takes {data_total:.1f}s ({data_total/total_time*100:.0f}%) — pre-filter columns at load")
    if metrics_total > total_time * 0.1:
        print(f"  🟡 Metrics calc takes {metrics_total:.1f}s ({metrics_total/total_time*100:.0f}%) — defer non-essential metrics")

    # Look for N² patterns
    for (filename, lineno, funcname), (cc, nc, tt, ct, callers) in stats.stats.items():
        if nc > 100000 and tt > 1.0:
            print(f"  ⚠ {funcname} called {nc:,}× (total {tt:.1f}s) — possible N² pattern")

    print()
    print(f"  Target: {total_time:.0f}s → {total_time*0.6:.0f}s (40% reduction)")
    print(f"  Focus on top module: {top_hotspot[0]} ({top_hotspot[1]:.1f}s)")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Profile Atlas backtest engine")
    parser.add_argument("--market", default="sp500", help="Market to profile (default: sp500)")
    parser.add_argument("--sort", default="cumulative", help="Sort order: cumulative, tottime, calls")
    parser.add_argument("--output", help="Save raw profile data to .prof file")
    args = parser.parse_args()

    config_path = PROJECT_ROOT / "config" / "active" / f"{args.market}.json"
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    print(f"Loading {args.market} data...")
    t0 = time.time()
    data = load_data(args.market)
    load_time = time.time() - t0
    print(f"Loaded {len(data)} tickers in {load_time:.1f}s")

    print(f"\nProfiling backtest ({args.market})...")
    profiler = cProfile.Profile()
    t1 = time.time()
    profiler.enable()
    result = run_backtest(config, data)
    profiler.disable()
    total_time = time.time() - t1

    stats = pstats.Stats(profiler)
    stats.strip_dirs()

    # Save raw profile if requested
    if args.output:
        profiler.dump_stats(args.output)
        print(f"Raw profile saved to {args.output}")

    analyze_profile(stats, total_time)

    # Print backtest result summary
    m = result.get("metrics", {})
    print(f"\nBacktest: {m.get('total_trades', 0)} trades, "
          f"Sharpe {m.get('sharpe', 0):.3f}, "
          f"CAGR {m.get('cagr', 0)*100:.1f}%")


if __name__ == "__main__":
    main()
