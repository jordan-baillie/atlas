#!/usr/bin/env python3
"""Migration: create market_equity_history table and indexes.

Idempotent — safe to run multiple times (uses IF NOT EXISTS).

Usage:
    python3 scripts/migrations/2026-04-29-add-market-equity-history.py [--apply]

Flags:
    --apply   Actually execute the migration (default: dry-run preview).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS market_equity_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    date             TEXT NOT NULL,
    market_id        TEXT NOT NULL,
    allocated_equity REAL NOT NULL,
    position_mv      REAL NOT NULL,
    cash_attributed  REAL NOT NULL,
    broker_equity    REAL NOT NULL,
    broker_cash      REAL NOT NULL,
    snapshot_time    TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(date, market_id)
)
"""

_CREATE_IDX_DATE = """
CREATE INDEX IF NOT EXISTS idx_market_equity_history_date
    ON market_equity_history(date)
"""

_CREATE_IDX_MARKET = """
CREATE INDEX IF NOT EXISTS idx_market_equity_history_market
    ON market_equity_history(market_id)
"""


def run_migration(dry_run: bool = True) -> None:
    """Create market_equity_history table and indexes (idempotent)."""
    print(f"{'DRY RUN — ' if dry_run else ''}Running migration: market_equity_history …")

    if dry_run:
        print("  [dry-run] Would execute:")
        print("    CREATE TABLE IF NOT EXISTS market_equity_history (...)")
        print("    CREATE INDEX IF NOT EXISTS idx_market_equity_history_date (...)")
        print("    CREATE INDEX IF NOT EXISTS idx_market_equity_history_market (...)")
        print("  Re-run with --apply to execute.")
        return

    from db.atlas_db import get_db

    with get_db() as db:
        db.execute(_CREATE_TABLE_SQL)
        db.execute(_CREATE_IDX_DATE)
        db.execute(_CREATE_IDX_MARKET)

        row = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='market_equity_history'"
        ).fetchone()
        if row is None:
            raise RuntimeError("Migration failed: market_equity_history not found after CREATE")

        idx_rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='market_equity_history'"
        ).fetchall()
        idx_names = {r["name"] for r in idx_rows}

        row_count = db.execute(
            "SELECT COUNT(*) AS n FROM market_equity_history"
        ).fetchone()["n"]

    print(f"  Table  : market_equity_history — OK")
    print(f"  Indexes: {sorted(idx_names)}")
    print(f"  Rows   : {row_count}")
    print("Migration complete.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add market_equity_history table")
    parser.add_argument(
        "--apply", action="store_true", help="Execute migration (default: dry-run)"
    )
    args = parser.parse_args()
    run_migration(dry_run=not args.apply)
