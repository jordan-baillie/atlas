#!/usr/bin/env python3
"""Migration: 2026-04-29-add-reconcile-shadow-runs.py

Creates the reconcile_shadow_runs table used by scripts/reconcile_shadow.py
to persist per-run comparison results during the 7-day shadow period.

Schema:
  ts                     — ISO UTC timestamp of the shadow run
  market                 — market_id ('sp500', 'commodity_etfs', etc.)
  new_drift_count        — drift items found by core.reconcile
  old_drift_count        — drift items found by existing scripts (parsed from logs)
  divergence_count       — abs(new_drift - old_drift); >0 triggers alert
  divergence_detail_json — JSON array of divergence detail strings
  report_json            — JSON of ReconcileReport.summary() for both calls

Usage:
    python3 scripts/migrations/2026-04-29-add-reconcile-shadow-runs.py          # dry-run
    python3 scripts/migrations/2026-04-29-add-reconcile-shadow-runs.py --apply  # apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ATLAS_ROOT / "data" / "atlas.db"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS reconcile_shadow_runs (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                     TEXT NOT NULL,
    market                 TEXT NOT NULL,
    new_drift_count        INTEGER NOT NULL DEFAULT 0,
    old_drift_count        INTEGER NOT NULL DEFAULT 0,
    divergence_count       INTEGER NOT NULL DEFAULT 0,
    divergence_detail_json TEXT,
    report_json            TEXT,
    created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_shadow_runs_market_ts "
    "ON reconcile_shadow_runs(market, ts);"
)


def run_migration(db_path: Path, apply: bool) -> None:
    tag = "[APPLY]" if apply else "[DRY-RUN]"
    print(f"\n{tag} Migration: add reconcile_shadow_runs table")
    print(f"  DB: {db_path}")

    if not db_path.exists():
        print(f"  ERROR: DB not found at {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    try:
        # Check if table already exists
        existing = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='reconcile_shadow_runs'"
        ).fetchone()
        if existing:
            print("  INFO: reconcile_shadow_runs already exists — idempotent, nothing to do")
            return

        print(f"\n{tag} CREATE TABLE reconcile_shadow_runs")
        print(f"{tag} CREATE INDEX idx_shadow_runs_market_ts")

        if apply:
            conn.executescript(CREATE_TABLE_SQL)
            conn.execute(CREATE_INDEX_SQL)
            conn.commit()
            print(f"\n  ✅ Migration applied successfully")
        else:
            print(f"\n  DRY-RUN complete — re-run with --apply to execute")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the migration")
    args = parser.parse_args()
    run_migration(DB_PATH, apply=args.apply)


if __name__ == "__main__":
    main()
