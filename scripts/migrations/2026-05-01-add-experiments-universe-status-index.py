#!/usr/bin/env python3
"""
Migration: 2026-05-01-add-experiments-universe-status-index.py

Adds a composite index on research_experiments(universe, status) to accelerate
the common query pattern: filter by universe and status (e.g., universe='sp500'
AND status='completed').

Usage:
    python3 scripts/migrations/2026-05-01-add-experiments-universe-status-index.py          # dry-run
    python3 scripts/migrations/2026-05-01-add-experiments-universe-status-index.py --apply  # apply
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

# ── Bootstrap sys.path so imports resolve from PROJECT_ROOT ──────────────────
ATLAS_ROOT = Path(__file__).resolve().parents[2]
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

DB_PATH = ATLAS_ROOT / "data" / "atlas.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── DDL ─────────────────────────────────────────────────────────────────────

CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_experiments_universe_status "
    "ON research_experiments(universe, status);"
)

VERIFY_EXPLAIN_SQL = (
    "EXPLAIN QUERY PLAN "
    "SELECT * FROM research_experiments "
    "WHERE universe='sp500' AND status='completed' LIMIT 10;"
)


def _index_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_experiments_universe_status'"
    ).fetchone()
    return row is not None


def _run(apply: bool) -> None:
    if not DB_PATH.exists():
        logger.error("DB not found at %s", DB_PATH)
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        already = _index_exists(conn)

        logger.info("Migration: 2026-05-01-add-experiments-universe-status-index")
        logger.info("DB:        %s", DB_PATH)
        logger.info("Mode:      %s", "APPLY" if apply else "DRY-RUN")
        logger.info("Index idx_experiments_universe_status already exists: %s", already)

        logger.info("  SQL: %s", CREATE_INDEX_SQL)

        if apply:
            conn.executescript(CREATE_INDEX_SQL)
            conn.commit()

            # Verify index exists
            if _index_exists(conn):
                logger.info("✅  Index idx_experiments_universe_status created (or already existed).")
            else:
                logger.error("❌  Index NOT found after apply!")
                sys.exit(1)

            # Verification: EXPLAIN QUERY PLAN
            logger.info("")
            logger.info("EXPLAIN QUERY PLAN verification:")
            rows = conn.execute(VERIFY_EXPLAIN_SQL).fetchall()
            for row in rows:
                logger.info("  %s", tuple(row))

            # Confirm the index is referenced in the plan
            plan_text = " ".join(str(col) for row in rows for col in row)
            if "idx_experiments_universe_status" in plan_text:
                logger.info("✅  Query plan uses idx_experiments_universe_status.")
            else:
                logger.warning(
                    "Query plan did not reference idx_experiments_universe_status "
                    "(SQLite may use a different strategy for small tables — index is present)."
                )
        else:
            logger.info("")
            logger.info("--- Dry-run complete. Run with --apply to execute.")

    finally:
        conn.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply the migration (default: dry-run only)",
    )
    args = parser.parse_args(argv)
    _run(apply=args.apply)


if __name__ == "__main__":
    main()
