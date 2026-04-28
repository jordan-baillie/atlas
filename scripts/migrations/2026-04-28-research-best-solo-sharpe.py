#!/usr/bin/env python3
"""M2 migration: research_best solo-sharpe schema fix.

Adds three new columns to research_best (additive, non-breaking):
    solo_sharpe      REAL           -- strategy-standalone Sharpe
    portfolio_sharpe REAL           -- whole-portfolio Sharpe (what 'sharpe' was)
    metric_type      TEXT NOT NULL  -- classification tag

Backfill strategy:
    For each legacy row, attempt to join research_experiments to find the
    MAX(sharpe) from solo-screen experiments ([solo screen] description or
    discard_solo status) for the same (strategy, universe).
    • If a solo experiment exists → set solo_sharpe, metric_type='solo'
    • Else → metric_type='legacy_portfolio', solo_sharpe=NULL

Idempotent: safe to run multiple times.

Usage:
    python3 scripts/migrations/2026-04-28-research-best-solo-sharpe.py          # dry-run
    python3 scripts/migrations/2026-04-28-research-best-solo-sharpe.py --apply  # commit changes
"""

from __future__ import annotations

import argparse
import csv
import datetime
import sqlite3
import sys
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = ATLAS_ROOT / "data" / "atlas.db"


# ─── Schema helpers ───────────────────────────────────────────────────────────

_NEW_COLUMNS = [
    ("solo_sharpe",      "REAL"),
    ("portfolio_sharpe", "REAL"),
    ("metric_type",      "TEXT NOT NULL DEFAULT 'unknown'"),
]


def _add_columns_idempotent(conn: sqlite3.Connection) -> list[str]:
    """Add new columns to research_best if they don't already exist.

    Returns list of columns that were actually added (empty if already present).
    """
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(research_best)").fetchall()
    }
    added: list[str] = []
    for col_name, col_def in _NEW_COLUMNS:
        if col_name not in existing_cols:
            conn.execute(
                f"ALTER TABLE research_best ADD COLUMN {col_name} {col_def}"
            )
            added.append(col_name)
    return added


# ─── Backfill ─────────────────────────────────────────────────────────────────

def _build_solo_sharpe_map(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    """Build (strategy, universe) → max_solo_sharpe from research_experiments.

    Solo experiments are identified by:
        description LIKE '[solo screen]%'  OR  status = 'discard_solo'
    """
    rows = conn.execute(
        """
        SELECT strategy, universe, MAX(sharpe) AS max_solo_sharpe
        FROM research_experiments
        WHERE description LIKE '[solo screen]%'
           OR status = 'discard_solo'
        GROUP BY strategy, universe
        """
    ).fetchall()
    return {(r[0], r[1]): float(r[2]) for r in rows if r[2] is not None}


def _backfill(
    conn: sqlite3.Connection,
    dry_run: bool = True,
) -> dict:
    """Populate solo_sharpe / portfolio_sharpe / metric_type for existing rows.

    Returns stats dict.
    """
    solo_map = _build_solo_sharpe_map(conn)
    rows = conn.execute(
        "SELECT strategy, universe, sharpe FROM research_best"
    ).fetchall()

    total = len(rows)
    backfilled = 0          # got a solo_sharpe from experiments
    legacy_portfolio = 0    # no solo data → metric_type='legacy_portfolio'
    already_done = 0        # solo_sharpe already populated

    per_universe_backfilled: dict[str, int] = {}
    per_universe_legacy: dict[str, int] = {}
    changes: list[dict] = []

    for row in rows:
        strategy, universe, combined_sharpe = row[0], row[1], row[2]

        # Check if already backfilled
        existing = conn.execute(
            "SELECT solo_sharpe, metric_type FROM research_best "
            "WHERE strategy=? AND universe=?",
            (strategy, universe),
        ).fetchone()
        if existing and existing[0] is not None:
            already_done += 1
            continue

        solo_sh = solo_map.get((strategy, universe))

        if solo_sh is not None:
            new_solo = solo_sh
            new_portfolio = combined_sharpe  # legacy sharpe was portfolio-level
            new_metric_type = "solo"         # we have solo; portfolio also available
            backfilled += 1
            per_universe_backfilled[universe] = per_universe_backfilled.get(universe, 0) + 1
        else:
            new_solo = None
            new_portfolio = combined_sharpe  # legacy sharpe was portfolio-level
            new_metric_type = "legacy_portfolio"
            legacy_portfolio += 1
            per_universe_legacy[universe] = per_universe_legacy.get(universe, 0) + 1

        changes.append({
            "strategy": strategy,
            "universe": universe,
            "combined_sharpe": combined_sharpe,
            "new_solo_sharpe": new_solo,
            "new_portfolio_sharpe": new_portfolio,
            "new_metric_type": new_metric_type,
        })

        if not dry_run:
            conn.execute(
                """
                UPDATE research_best
                   SET solo_sharpe      = COALESCE(solo_sharpe, ?),
                       portfolio_sharpe = COALESCE(portfolio_sharpe, ?),
                       metric_type      = CASE
                                            WHEN metric_type IS NULL OR metric_type = 'unknown'
                                            THEN ?
                                            ELSE metric_type
                                          END
                 WHERE strategy = ? AND universe = ?
                """,
                (new_solo, new_portfolio, new_metric_type, strategy, universe),
            )

    return {
        "total": total,
        "backfilled_with_solo": backfilled,
        "legacy_portfolio": legacy_portfolio,
        "already_done": already_done,
        "per_universe_backfilled": per_universe_backfilled,
        "per_universe_legacy": per_universe_legacy,
        "changes": changes,
    }


# ─── CSV snapshot ─────────────────────────────────────────────────────────────

def _snapshot_csv(conn: sqlite3.Connection) -> Path:
    """Write a CSV backup of research_best before migration."""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(f"/tmp/research_best_pre_m2_{ts}.csv")
    rows = conn.execute("SELECT * FROM research_best").fetchall()
    if not rows:
        out.write_text("(empty)\n")
        return out
    cols = [d[0] for d in conn.execute("SELECT * FROM research_best LIMIT 0").description]
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow(dict(zip(cols, row)))
    return out


# ─── Report ───────────────────────────────────────────────────────────────────

def _print_report(stats: dict, dry_run: bool) -> None:
    mode = "DRY-RUN" if dry_run else "APPLIED"
    print(f"\n{'='*60}")
    print(f" M2 Migration: research_best solo-sharpe  [{mode}]")
    print(f"{'='*60}")
    print(f"  Total research_best rows   : {stats['total']}")
    print(f"  Already backfilled         : {stats['already_done']}")
    print(f"  Backfilled with solo_sharpe: {stats['backfilled_with_solo']}")
    print(f"  Marked legacy_portfolio    : {stats['legacy_portfolio']}")

    if stats["per_universe_backfilled"]:
        print("\n  Backfilled per universe:")
        for uni, cnt in sorted(stats["per_universe_backfilled"].items()):
            print(f"    {uni:30s}: {cnt}")

    if stats["per_universe_legacy"]:
        print("\n  legacy_portfolio per universe:")
        for uni, cnt in sorted(stats["per_universe_legacy"].items()):
            print(f"    {uni:30s}: {cnt}")

    if stats["changes"]:
        print("\n  Row-level changes:")
        for c in stats["changes"]:
            tag = "solo" if c["new_solo_sharpe"] is not None else "legacy_portfolio"
            print(
                f"    {c['strategy']}/{c['universe']}: "
                f"portfolio_sharpe={c['new_portfolio_sharpe']:.4f}, "
                f"solo_sharpe={c['new_solo_sharpe']!r} [{tag}]"
            )
    print()


# ─── Main ─────────────────────────────────────────────────────────────────────

def run_migration(db_path: Path = DB_PATH, apply: bool = False) -> dict:
    """Run the migration. Returns stats dict.

    Args:
        db_path: Path to atlas.db.
        apply:   If True, commits changes. If False, dry-run only.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row

    # --- Snapshot before any changes ---
    if apply:
        csv_path = _snapshot_csv(conn)
        print(f"Backup saved to: {csv_path}")

    # --- Step 1: Add columns ---
    added = _add_columns_idempotent(conn)
    if added:
        print(f"Added columns: {added}")
        if apply:
            conn.commit()
    else:
        print("Columns already exist — schema is up to date.")

    # --- Step 2: Backfill ---
    stats = _backfill(conn, dry_run=not apply)

    if apply:
        conn.commit()

    conn.close()
    return stats


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Commit changes to DB (default: dry-run only)",
    )
    parser.add_argument(
        "--db", default=str(DB_PATH),
        help=f"Path to atlas.db (default: {DB_PATH})",
    )
    args = parser.parse_args(argv)

    stats = run_migration(db_path=Path(args.db), apply=args.apply)
    _print_report(stats, dry_run=not args.apply)

    if not args.apply:
        print("ℹ️  This was a DRY-RUN. Pass --apply to commit.\n")
    else:
        print("✅ Migration applied successfully.\n")


if __name__ == "__main__":
    main()
