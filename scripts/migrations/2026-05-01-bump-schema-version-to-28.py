#!/usr/bin/env python3
"""
Migration: 2026-05-01-bump-schema-version-to-28.py

Brings schema_version up-to-date with the actual number of applied migrations.

Background
----------
schema_version was seeded at version=1 by db/schema.sql (the initial schema).
There is no formal migration runner — migrations are applied manually in date
order.  As of 2026-05-01 there are 28 migration files applied against atlas.db
(see scripts/migrations/ for the full list), so version is bumped to 28.

This is informational only.  See scripts/migrations/README.md for policy.

Usage:
    python3 scripts/migrations/2026-05-01-bump-schema-version-to-28.py          # dry-run
    python3 scripts/migrations/2026-05-01-bump-schema-version-to-28.py --apply  # apply
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

TARGET_VERSION = 28

# Idempotent: only insert if this exact version row is absent
INSERT_SQL = (
    "INSERT OR IGNORE INTO schema_version (version, applied_at) "
    "VALUES (?, datetime('now'));"
)


def _current_max(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] if row else None


def _run(apply: bool) -> None:
    if not DB_PATH.exists():
        logger.error("DB not found at %s", DB_PATH)
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        current = _current_max(conn)
        logger.info("Migration: 2026-05-01-bump-schema-version-to-28")
        logger.info("DB:        %s", DB_PATH)
        logger.info("Mode:      %s", "APPLY" if apply else "DRY-RUN")
        logger.info("Current schema_version (MAX): %s", current)
        logger.info("Target version: %d", TARGET_VERSION)

        if current is not None and current >= TARGET_VERSION:
            logger.info("✅  schema_version already at or above %d — nothing to do.", TARGET_VERSION)
            return

        # Check if applied_at column exists; add it if missing (self-migrating)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(schema_version)").fetchall()]
        if "applied_at" not in cols:
            logger.info("  Adding applied_at column to schema_version...")
            if apply:
                conn.execute("ALTER TABLE schema_version ADD COLUMN applied_at TEXT")
                conn.commit()

        logger.info("  SQL: INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (%d, datetime('now'))", TARGET_VERSION)

        if apply:
            conn.execute(INSERT_SQL, (TARGET_VERSION,))
            conn.commit()

            new_max = _current_max(conn)
            logger.info("")
            logger.info("✅  schema_version bumped to %d.", new_max)

            # Show full table state
            logger.info("")
            logger.info("SELECT * FROM schema_version ORDER BY version DESC LIMIT 5:")
            rows = conn.execute(
                "SELECT * FROM schema_version ORDER BY version DESC LIMIT 5"
            ).fetchall()
            for row in rows:
                logger.info("  %s", tuple(row))
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
