#!/usr/bin/env python3
"""Migration: add window_coverage_pct column to research_experiments.

Tracks the fraction of walk-forward windows actually simulated vs planned.
Used by keep_or_discard() Gate 5 and surfaced in the dashboard.

Idempotent — safe to run multiple times.
"""
import sqlite3
import sys
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ATLAS_ROOT / "data" / "atlas.db"


def migrate():
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} does not exist")
        return 1
    conn = sqlite3.connect(str(DB_PATH))
    try:
        # Check if column already exists
        cols = [r[1] for r in conn.execute("PRAGMA table_info(research_experiments);").fetchall()]
        if "window_coverage_pct" in cols:
            print("Column window_coverage_pct already exists — no-op")
            return 0

        conn.execute("ALTER TABLE research_experiments ADD COLUMN window_coverage_pct REAL")
        conn.commit()
        print("Added column research_experiments.window_coverage_pct REAL")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(migrate())
