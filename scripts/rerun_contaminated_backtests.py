#!/usr/bin/env python3
"""Rerun 14 contaminated solo backtests (#327).

The 14 entries in research/best/*.json with contamination_note
"portfolio-contaminated" were measured during combined portfolio
backtests — their headline metrics reflect the whole portfolio, not the
strategy alone.  This script reruns each (strategy, market) pair in
*solo* mode (single strategy, all others disabled) to obtain clean,
uncontaminated performance metrics.

Results are written to:
  - data/atlas.db :: research_best (solo_sharpe, metric_type='solo')
  - research/best/{strategy}[_{market}].json (contamination_note cleared)
  - data/contaminated_backtests/results_{run_id}.json (full run report)

Usage:
    python3 scripts/rerun_contaminated_backtests.py
    python3 scripts/rerun_contaminated_backtests.py --workers 2
    python3 scripts/rerun_contaminated_backtests.py --dry-run
    python3 scripts/rerun_contaminated_backtests.py --detect-only  # list pairs
    python3 scripts/rerun_contaminated_backtests.py --strategy mean_reversion
    python3 scripts/rerun_contaminated_backtests.py --market sp500
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT / "data" / "contaminated_backtests"
BEST_DIR = PROJECT / "research" / "best"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("rerun_contaminated")


# ---------------------------------------------------------------------------
# Contaminated pairs (hardcoded from research/best/*.json audit, 2026-05-14)
# Dynamically re-detected at runtime via detect_contaminated_pairs() —
# these serve as the documented ground truth for issue #327.
# ---------------------------------------------------------------------------

KNOWN_CONTAMINATED: list[tuple[str, str]] = [
    ("connors_rsi2",          "sp500"),
    ("consecutive_down_days", "sp500"),
    ("mean_reversion",        "sp500"),
    ("mean_reversion",        "commodity_etfs"),
    ("mean_reversion",        "crypto"),
    ("mean_reversion",        "defensive_etfs"),
    ("mean_reversion",        "gold_etfs"),
    ("mean_reversion",        "treasury_etfs"),
    ("momentum_breakout",     "commodity_etfs"),
    ("momentum_breakout",     "defensive_etfs"),
    ("momentum_breakout",     "gold_etfs"),
    ("momentum_breakout",     "treasury_etfs"),
    ("opening_gap",           "sp500"),
    ("short_term_mr",         "sp500"),
]


# ---------------------------------------------------------------------------
# Dynamic detection
# ---------------------------------------------------------------------------

def detect_contaminated_pairs(
    best_dir: Path | None = None,
) -> list[tuple[str, str, Path]]:
    """Scan research/best/*.json for contaminated/unvalidated entries.

    Detection criteria (any match → contaminated):

    1. ``is_solo == false``  — explicitly portfolio-contaminated (not a solo backtest).
    2. ``is_solo == true`` AND ``solo_sharpe_clean`` is absent/None  — tagged solo via
       the integrity audit but clean rerun not yet performed (orphan).
    3. Neither ``is_solo`` nor ``solo_sharpe_clean`` present  — legacy entry predating
       the integrity-audit enrichment; no contamination verdict yet.

    Args:
        best_dir: Override directory for tests. Defaults to ``BEST_DIR``.

    Returns:
        List of ``(strategy, market, json_path)`` tuples.
    """
    _dir = best_dir if best_dir is not None else BEST_DIR
    pairs: list[tuple[str, str, Path]] = []
    for f in sorted(_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text())
            strategy = d.get("strategy", "")
            market   = d.get("market", d.get("universe", "sp500"))
            if not strategy:
                continue

            is_solo           = d.get("is_solo")            # may be absent
            solo_sharpe_clean = d.get("solo_sharpe_clean")  # may be absent or None
            has_is_solo       = "is_solo" in d
            has_clean_sharpe  = "solo_sharpe_clean" in d

            contaminated = False
            if has_is_solo and is_solo is False:
                # Criterion 1: explicitly not-solo (portfolio contaminated)
                contaminated = True
            elif has_is_solo and is_solo is True and solo_sharpe_clean is None:
                # Criterion 2: tagged solo but clean rerun metric missing (orphan)
                contaminated = True
            elif not has_is_solo and not has_clean_sharpe:
                # Criterion 3: legacy entry — neither integrity field populated yet
                contaminated = True

            if contaminated:
                pairs.append((strategy, market, f))
        except Exception as exc:
            logger.warning("Could not parse %s: %s", f.name, exc)
    return pairs


def _best_json_path(strategy: str, market: str) -> Path:
    """Return the research/best/ JSON path for (strategy, market)."""
    if market and market != "sp500":
        return BEST_DIR / f"{strategy}_{market}.json"
    return BEST_DIR / f"{strategy}.json"


# ---------------------------------------------------------------------------
# Solo backtest worker (module-level for ProcessPoolExecutor pickling)
# ---------------------------------------------------------------------------

def _run_solo_backtest(
    strategy: str,
    market: str,
    params_override: dict | None,
) -> dict:
    """Run a solo backtest for (strategy, market). Module-level for pickling.

    Args:
        strategy:        Strategy name.
        market:          Market/universe ID.
        params_override: Best-known params from research/best/ JSON.

    Returns:
        Result dict with keys: strategy, market, status, metrics (or error).
    """
    sys.path.insert(0, str(PROJECT))
    t0 = time.time()
    result: dict = {
        "strategy": strategy,
        "market":   market,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        from scripts.strategy_evaluator import (
            load_market_data,
            make_config_with_strategy,
            run_backtest,
        )
        from utils.config import get_active_config

        # Load config — fall back to sp500 config for universes without their
        # own active config file (gold_etfs, treasury_etfs, defensive_etfs, crypto).
        try:
            config = get_active_config(market)
        except Exception as cfg_exc:
            logger.warning(
                "No active config for %s (%s) — using sp500 config as base",
                market, cfg_exc,
            )
            config = get_active_config("sp500")
            config = copy.deepcopy(config)
            config["market"] = market

        # Load market-specific data
        data = load_market_data(market)
        if not data:
            raise RuntimeError(f"No data loaded for market={market!r}")

        # Build solo config: target strategy only, all others disabled
        solo_cfg = make_config_with_strategy(
            config, strategy, params_override, solo=True
        )

        metrics = run_backtest(solo_cfg, data)
        elapsed = round(time.time() - t0, 1)
        result.update({
            "status":   "ok",
            "metrics":  metrics,
            "elapsed_s": elapsed,
            "n_tickers": len(data),
        })
        logger.info(
            "OK  %s/%s  sharpe=%.4f  trades=%d  elapsed=%ss",
            strategy, market,
            metrics.get("sharpe", 0) or 0,
            metrics.get("total_trades", 0) or 0,
            elapsed,
        )

    except Exception as exc:
        elapsed = round(time.time() - t0, 1)
        result.update({
            "status":   "error",
            "error":    str(exc),
            "elapsed_s": elapsed,
        })
        logger.error("FAIL  %s/%s  error=%s", strategy, market, exc)

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result


# ---------------------------------------------------------------------------
# Write-back helpers (run in main process — serialized)
# ---------------------------------------------------------------------------

def _update_research_best(
    strategy: str,
    market: str,
    metrics: dict,
) -> None:
    """Upsert solo metrics to research_best SQLite table."""
    try:
        sys.path.insert(0, str(PROJECT))
        from db.atlas_db import upsert_research_best

        solo_sharpe = float(metrics.get("sharpe", 0) or 0)
        trades      = int(metrics.get("total_trades", 0) or 0)
        max_dd      = float(metrics.get("max_drawdown_pct", 0) or 0)

        # Load current best params from JSON to preserve them
        best_path = _best_json_path(strategy, market)
        params: dict = {}
        if best_path.exists():
            try:
                params = json.loads(best_path.read_text()).get("params", {})
            except Exception:
                pass

        upsert_research_best(
            strategy=strategy,
            universe=market,
            params=params,
            solo_sharpe=solo_sharpe,
            trades=trades,
            max_dd_pct=max_dd,
            metric_type="solo",
        )
        logger.info("research_best updated: %s/%s  solo_sharpe=%.4f", strategy, market, solo_sharpe)
    except Exception as exc:
        logger.error("research_best write failed for %s/%s: %s", strategy, market, exc)


def _update_best_json(
    strategy: str,
    market: str,
    metrics: dict,
    json_path: Path,
) -> None:
    """Update the research/best/ JSON file with fresh solo metrics."""
    if not json_path.exists():
        logger.warning("Best JSON not found: %s", json_path)
        return
    try:
        d = json.loads(json_path.read_text())
        # Replace headline metrics with clean solo metrics
        d["metrics"] = metrics
        d["is_solo"] = True
        d["solo_fraction"] = 1.0
        d["updated_at"] = datetime.now(timezone.utc).isoformat()
        d["solo_sharpe_clean"] = float(metrics.get("sharpe", 0) or 0)
        # Clear contamination note — this is now a clean solo run
        d.pop("contamination_note", None)
        d["_rerun_note"] = (
            f"Clean solo backtest. Re-run by rerun_contaminated_backtests.py "
            f"on {datetime.now(timezone.utc).strftime('%Y-%m-%d')} (#327)."
        )
        json_path.write_text(json.dumps(d, indent=2))
        logger.info("Best JSON updated: %s", json_path.name)
    except Exception as exc:
        logger.error("Best JSON write failed for %s/%s: %s", strategy, market, exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rerun 14 contaminated solo backtests (#327)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 scripts/rerun_contaminated_backtests.py\n"
            "  python3 scripts/rerun_contaminated_backtests.py --workers 2\n"
            "  python3 scripts/rerun_contaminated_backtests.py --dry-run\n"
            "  python3 scripts/rerun_contaminated_backtests.py --strategy mean_reversion\n"
        ),
    )
    parser.add_argument(
        "--workers", type=int, default=2,
        help="Number of parallel backtest workers (default: 2).",
    )
    parser.add_argument(
        "--strategy", default=None,
        help="Only rerun entries matching this strategy name.",
    )
    parser.add_argument(
        "--market", default=None,
        help="Only rerun entries matching this market/universe.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan without running any backtests.",
    )
    parser.add_argument(
        "--detect-only", action="store_true",
        help="Print detected contaminated pairs and exit.",
    )
    parser.add_argument(
        "--no-update-db", action="store_true",
        help="Skip writing results back to SQLite research_best.",
    )
    parser.add_argument(
        "--no-update-json", action="store_true",
        help="Skip updating research/best/*.json files.",
    )
    args = parser.parse_args(argv)

    # Detect contaminated pairs dynamically from best/*.json
    detected = detect_contaminated_pairs()
    logger.info("Detected %d contaminated entries in research/best/", len(detected))

    # Cross-check against hardcoded ground truth
    detected_set = {(s, m) for s, m, _ in detected}
    known_set    = set(KNOWN_CONTAMINATED)
    if detected_set != known_set:
        new_in_detected = detected_set - known_set
        missing_from_detected = known_set - detected_set
        if new_in_detected:
            logger.warning("NEW contaminated entries found (not in KNOWN list): %s", new_in_detected)
        if missing_from_detected:
            logger.info(
                "Entries in KNOWN list no longer contaminated (already fixed?): %s",
                missing_from_detected,
            )

    # --detect-only: print and exit
    if args.detect_only:
        print(f"\n{'Strategy':<30} {'Market':<20} {'JSON file'}")
        print("-" * 70)
        for s, m, p in detected:
            print(f"{s:<30} {m:<20} {p.name}")
        print(f"\nTotal: {len(detected)}")
        return 0

    # Build work list — detected is the primary source; KNOWN_CONTAMINATED is the
    # fallback for the edge case where the detection yields zero entries (e.g. an
    # empty best/ directory during testing or first-run bootstrap).
    if detected:
        work: list[tuple[str, str, Path]] = [(s, m, p) for s, m, p in detected]
    else:
        logger.warning(
            "No dynamic detection — falling back to KNOWN_CONTAMINATED (%d entries)",
            len(KNOWN_CONTAMINATED),
        )
        work = [(s, m, _best_json_path(s, m)) for s, m in KNOWN_CONTAMINATED]

    # Apply filters
    if args.strategy:
        work = [(s, m, p) for s, m, p in work if s == args.strategy]
    if args.market:
        work = [(s, m, p) for s, m, p in work if m == args.market]

    if not work:
        logger.info("No contaminated pairs match the given filters — nothing to do.")
        return 0

    logger.info(
        "=== Contaminated Backtest Rerun (#327) ===  pairs=%d  workers=%d",
        len(work), args.workers,
    )

    # Load best params for each pair (done in main process to avoid I/O races)
    pair_params: dict[tuple[str, str], dict | None] = {}
    for strategy, market, json_path in work:
        params: dict | None = None
        if json_path.exists():
            try:
                params = json.loads(json_path.read_text()).get("params")
            except Exception:
                pass
        pair_params[(strategy, market)] = params

    if args.dry_run:
        print(f"\n{'Strategy':<30} {'Market':<20} {'Params keys'}")
        print("-" * 70)
        for strategy, market, _ in work:
            p = pair_params.get((strategy, market))
            pkeys = list(p.keys()) if p else "none"
            print(f"{strategy:<30} {market:<20} {pkeys}")
        print(f"\nWould run {len(work)} backtests with --workers {args.workers}")
        return 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # Submit parallel solo backtests
    results: list[dict] = []
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        future_to_pair = {
            executor.submit(
                _run_solo_backtest,
                strategy,
                market,
                pair_params.get((strategy, market)),
            ): (strategy, market, json_path)
            for strategy, market, json_path in work
        }

        for future in as_completed(future_to_pair):
            strategy, market, json_path = future_to_pair[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "strategy": strategy,
                    "market":   market,
                    "status":   "error",
                    "error":    str(exc),
                }
            results.append(result)

            # Write-back immediately in main process (serialized)
            if result.get("status") == "ok":
                metrics = result.get("metrics", {})
                if not args.no_update_db:
                    _update_research_best(strategy, market, metrics)
                if not args.no_update_json:
                    _update_best_json(strategy, market, metrics, json_path)

    # Write run report
    report_path = OUTPUT_DIR / f"results_{run_id}.json"
    report_path.write_text(json.dumps(results, indent=2))
    (OUTPUT_DIR / f"contaminated_backtests_{run_id}.done").touch()
    logger.info("Report written -> %s", report_path)

    # Summary
    n_ok    = sum(1 for r in results if r.get("status") == "ok")
    n_fail  = sum(1 for r in results if r.get("status") != "ok")
    logger.info("=== Finished ===  ok=%d  failed=%d", n_ok, n_fail)

    if n_fail:
        failed = [r for r in results if r.get("status") != "ok"]
        for f in failed:
            logger.error("  FAIL: %s/%s — %s", f["strategy"], f["market"], f.get("error", "?"))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
