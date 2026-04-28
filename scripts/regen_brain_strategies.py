#!/usr/bin/env python3
"""Brain strategies nightly regeneration.

Fallback regeneration of research/brain/strategies/*.md from the
canonical research_best SQLite table.  Runs nightly as a cron; idempotent.

For strategies with multiple universes in research_best, the sp500 row is
preferred; if no sp500 row exists the alphabetically-first universe is used.

Expected cron entry (Worker B adds to pi-cron.sh):
    30 14 * * * /usr/bin/flock -n /tmp/regen_brain.lock bash -c \\
        'cd /root/atlas && timeout 5m python3 scripts/regen_brain_strategies.py --quiet' \\
        >> /root/atlas/logs/regen_brain.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

DB_PATH = ATLAS_ROOT / "data" / "atlas.db"

logger = logging.getLogger(__name__)


# ─── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging(quiet: bool) -> None:
    level = logging.WARNING if quiet else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [regen] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ─── Data loading ─────────────────────────────────────────────────────────────

def _load_research_best(
    db_path: Path,
    strategy_filter: str | None = None,
) -> list[dict]:
    """Query research_best and return one row per strategy.

    For strategies present in multiple universes, prefers the sp500 row;
    if no sp500 row exists, uses the alphabetically-first universe.

    M2 2026-04-28: uses COALESCE(solo_sharpe, sharpe) when M2 columns are
    present; falls back to legacy ``sharpe`` column for pre-migration DBs.
    """
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        # Detect whether M2 columns are present (safe for old test DBs)
        _cols = {r[1] for r in conn.execute("PRAGMA table_info(research_best)").fetchall()}
        _has_m2 = "solo_sharpe" in _cols

        if _has_m2:
            _sharpe_expr = "COALESCE(solo_sharpe, sharpe) AS sharpe, solo_sharpe, metric_type,"
        else:
            _sharpe_expr = "sharpe,"

        _base_sel = (
            f"SELECT strategy, universe, params, {_sharpe_expr} trades, max_dd_pct, updated_at "
            f"FROM research_best"
        )

        if strategy_filter:
            cursor = conn.execute(
                f"{_base_sel} WHERE strategy = ? ORDER BY strategy, universe",
                (strategy_filter,),
            )
        else:
            cursor = conn.execute(f"{_base_sel} ORDER BY strategy, universe")
        rows = [dict(r) for r in cursor.fetchall()]

    # Group by strategy — prefer sp500, else alphabetically first
    grouped: dict[str, dict] = {}
    for row in rows:
        s = row["strategy"]
        if s not in grouped:
            grouped[s] = row
        elif row["universe"] == "sp500":
            grouped[s] = row
        # else keep existing (already alphabetically first due to ORDER BY universe)

    return list(grouped.values())


def _build_metrics(row: dict) -> dict:
    """Build metrics dict from a research_best row."""
    return {
        "sharpe": float(row.get("sharpe") or 0.0),
        "total_trades": int(row.get("trades") or 0),
        "max_drawdown_pct": float(row.get("max_dd_pct") or 0.0),
        # Fields not stored in research_best — default to 0
        "profit_factor": 0.0,
        "cagr_pct": 0.0,
        "win_rate_pct": 0.0,
    }


def _build_params(row: dict) -> dict:
    """Parse params JSON from research_best row."""
    params_raw = row.get("params") or "{}"
    try:
        result = json.loads(params_raw)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "Could not parse params for %s: %r",
            row.get("strategy"),
            str(params_raw)[:80],
        )
        return {}


# ─── Core logic ───────────────────────────────────────────────────────────────

def regen_all(
    db_path: Path = DB_PATH,
    strategy_filter: str | None = None,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """Regenerate brain strategy files from research_best.

    Args:
        db_path:         Path to atlas.db.
        strategy_filter: If given, only regenerate that strategy.
        dry_run:         If True, report what would change without writing.

    Returns:
        (processed, succeeded, failed)
    """
    rows = _load_research_best(db_path, strategy_filter)

    if not rows:
        qualifier = f" for strategy '{strategy_filter}'" if strategy_filter else ""
        logger.warning("No rows found in research_best%s", qualifier)
        return 0, 0, 0

    if dry_run:
        logger.info("DRY RUN — would process %d strategies:", len(rows))
        for row in rows:
            logger.info(
                "  %s (universe=%s, sharpe=%.4f, trades=%d)",
                row["strategy"],
                row["universe"],
                float(row.get("sharpe") or 0.0),
                int(row.get("trades") or 0),
            )
        return len(rows), 0, 0

    from research.brain.writer import update_strategy  # noqa: PLC0415

    processed = len(rows)
    succeeded = 0
    failed = 0

    for row in rows:
        strat = row["strategy"]
        updated_at = row.get("updated_at", "unknown")
        try:
            metrics = _build_metrics(row)
            params = _build_params(row)
            description = (
                f"nightly regen from research_best "
                f"(universe={row['universe']}, updated={updated_at})"
            )
            update_strategy(strat, metrics, params, description=description)
            logger.info("%s: md updated", strat)
            succeeded += 1
        except Exception as exc:
            logger.error("%s: update_strategy failed — %s", strat, exc)
            failed += 1

    return processed, succeeded, failed


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Regenerate research/brain/strategies/*.md from research_best table.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 scripts/regen_brain_strategies.py\n"
            "  python3 scripts/regen_brain_strategies.py --strategy momentum_breakout\n"
            "  python3 scripts/regen_brain_strategies.py --dry-run\n"
        ),
    )
    parser.add_argument("--strategy", default=None,
                        help="Regen a single strategy only")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change, no writes")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress INFO logs (for cron)")
    parser.add_argument(
        "--db",
        default=str(DB_PATH),
        help=f"SQLite DB path (default: {DB_PATH})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _setup_logging(args.quiet)

    db_path = Path(args.db)
    if not db_path.exists():
        logger.error("Database not found: %s", db_path)
        return 1

    label = f"strategy={args.strategy}" if args.strategy else "all strategies"
    if args.dry_run:
        logger.info("Dry-run mode — no files will be written (%s)", label)
    else:
        logger.info("Starting brain regen (%s)", label)

    processed, succeeded, failed = regen_all(
        db_path=db_path,
        strategy_filter=args.strategy,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print(f"[regen] DRY RUN: {processed} strategies would be processed")
        return 0

    print(
        f"[regen] Summary: {processed} strategies processed, "
        f"{succeeded} succeeded, {failed} failed"
    )

    if processed == 0:
        logger.warning("Nothing processed — check research_best table")
        return 1
    if failed == processed:
        logger.error("All %d strategies failed — check logs", processed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
