#!/usr/bin/env python3
"""
sweep_universes.py — Run autoresearch parameter sweeps across ETF universes.

Targets viable strategy×universe combinations from task #203 backtest results.
Stores optimized params in research_best table keyed by (strategy, universe).

Usage:
    python3 scripts/sweep_universes.py                    # all viable combos
    python3 scripts/sweep_universes.py --combo 0          # just first combo
    python3 scripts/sweep_universes.py --max-runtime 3600  # 1 hour cap

Run via systemd for long sessions:
    systemctl start atlas-sweep-universes
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sweep_universes")

PROJECT = Path(__file__).resolve().parents[1]

# Viable combos from task #203 universe backtest results.
# Ordered by expected value: baseline Sharpe × trade count.
SWEEP_TARGETS = [
    # (strategy, universe, baseline_sharpe, baseline_trades)
    ("momentum_breakout", "commodity_etfs",  0.605, 137),
    ("connors_rsi2",      "gold_etfs",       0.311, 433),
    ("short_term_mr",     "gold_etfs",       0.108, 439),
    ("short_term_mr",     "sector_etfs",     0.099, 724),
    ("connors_rsi2",      "sector_etfs",     0.041, 743),
]

RESULTS_FILE = PROJECT / "data" / "universe_sweep_results.json"


def run_sweep(strategy: str, universe: str, max_runtime: int = 1800,
              cycles: int = 2, workers: int = 4) -> dict:
    """Run a single parameter sweep for a strategy×universe combo."""
    log.info("Starting sweep: %s × %s (max %ds, %d cycles)", 
             strategy, universe, max_runtime, cycles)
    start = time.time()
    
    cmd = [
        sys.executable, str(PROJECT / "research" / "sweep.py"),
        "--strategy", strategy,
        "--universe", universe,
        "--cycles", str(cycles),
        "--max-runtime", str(max_runtime),
        "--workers", str(workers),
        "--max-fails", "5",
    ]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max_runtime + 120,  # grace period
            cwd=str(PROJECT),
        )
        elapsed = time.time() - start
        
        if result.returncode == 0:
            log.info("Sweep %s × %s completed in %.0fs", strategy, universe, elapsed)
        else:
            log.warning("Sweep %s × %s exited with code %d (%.0fs)", 
                       strategy, universe, result.returncode, elapsed)
            if result.stderr:
                log.warning("stderr: %s", result.stderr[-500:])
        
        return {
            "strategy": strategy,
            "universe": universe,
            "exit_code": result.returncode,
            "elapsed_sec": round(elapsed, 1),
            "stdout_tail": (result.stdout or "")[-300:],
        }
        
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        log.warning("Sweep %s × %s timed out after %.0fs", strategy, universe, elapsed)
        return {
            "strategy": strategy,
            "universe": universe,
            "exit_code": -1,
            "elapsed_sec": round(elapsed, 1),
            "error": "timeout",
        }
    except Exception as e:
        elapsed = time.time() - start
        log.error("Sweep %s × %s failed: %s", strategy, universe, e)
        return {
            "strategy": strategy,
            "universe": universe,
            "exit_code": -2,
            "elapsed_sec": round(elapsed, 1),
            "error": str(e),
        }


def check_current_best(strategy: str, universe: str) -> dict | None:
    """Check if research_best already has an entry for this combo."""
    try:
        from db.atlas_db import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM research_best WHERE strategy = ? AND universe = ?",
                (strategy, universe),
            ).fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="Sweep ETF universe combos")
    parser.add_argument("--combo", type=int, default=None,
                       help="Run only combo N (0-indexed)")
    parser.add_argument("--max-runtime", type=int, default=1800,
                       help="Max seconds per sweep (default: 1800)")
    parser.add_argument("--cycles", type=int, default=2,
                       help="Sweep cycles per combo (default: 2)")
    parser.add_argument("--workers", type=int, default=4,
                       help="Parallel workers (default: 4)")
    parser.add_argument("--skip-existing", action="store_true",
                       help="Skip combos that already have research_best entries")
    args = parser.parse_args()
    
    targets = SWEEP_TARGETS
    if args.combo is not None:
        if 0 <= args.combo < len(targets):
            targets = [targets[args.combo]]
        else:
            log.error("Invalid combo index %d (valid: 0-%d)", args.combo, len(SWEEP_TARGETS) - 1)
            sys.exit(1)
    
    log.info("=" * 60)
    log.info("ETF Universe Parameter Sweep")
    log.info("Targets: %d combos, %d cycles each, %ds max per sweep",
             len(targets), args.cycles, args.max_runtime)
    log.info("=" * 60)
    
    results = []
    total_start = time.time()
    
    for i, (strategy, universe, baseline_sharpe, baseline_trades) in enumerate(targets):
        log.info("")
        log.info("── Combo %d/%d: %s × %s (baseline Sharpe=%.3f, %d trades) ──",
                 i + 1, len(targets), strategy, universe, baseline_sharpe, baseline_trades)
        
        if args.skip_existing:
            existing = check_current_best(strategy, universe)
            if existing:
                log.info("Skipping — already in research_best (Sharpe=%.3f)", 
                        existing.get("sharpe", 0))
                results.append({
                    "strategy": strategy,
                    "universe": universe,
                    "skipped": True,
                    "existing_sharpe": existing.get("sharpe", 0),
                })
                continue
        
        result = run_sweep(strategy, universe, 
                          max_runtime=args.max_runtime,
                          cycles=args.cycles,
                          workers=args.workers)
        
        # Check what the sweep found
        after = check_current_best(strategy, universe)
        if after:
            result["new_sharpe"] = after.get("sharpe", 0)
            result["improvement"] = after.get("sharpe", 0) - baseline_sharpe
            log.info("Result: Sharpe %.3f → %.3f (%+.3f)",
                    baseline_sharpe, after["sharpe"], result["improvement"])
        else:
            result["new_sharpe"] = None
            log.info("No research_best entry written (all trials discarded)")
        
        results.append(result)
    
    total_elapsed = time.time() - total_start
    
    # Save results
    summary = {
        "timestamp": datetime.now().isoformat(),
        "total_elapsed_sec": round(total_elapsed, 1),
        "results": results,
    }
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    
    log.info("")
    log.info("=" * 60)
    log.info("SWEEP COMPLETE — %.0fs total", total_elapsed)
    for r in results:
        status = "SKIPPED" if r.get("skipped") else ("OK" if r.get("exit_code", -1) == 0 else "FAIL")
        sharpe_info = f"Sharpe={r.get('new_sharpe', '?')}" if not r.get("skipped") else f"existing={r.get('existing_sharpe', '?')}"
        log.info("  %s × %s: %s (%s)", r["strategy"], r["universe"], status, sharpe_info)
    log.info("Results saved to %s", RESULTS_FILE)


if __name__ == "__main__":
    main()
