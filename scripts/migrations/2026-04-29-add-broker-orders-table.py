#!/usr/bin/env python3
"""
Migration: 2026-04-29-add-broker-orders-table.py

Creates the broker_orders table — a local cache of Alpaca order/fill history.
Provides source-of-truth fill prices for reconciliation, eliminating the class
of bugs where inference produces the wrong price (CHTR phantom-price pattern).

Usage:
    python3 scripts/migrations/2026-04-29-add-broker-orders-table.py          # dry-run
    python3 scripts/migrations/2026-04-29-add-broker-orders-table.py --apply  # apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ATLAS_ROOT / "data" / "atlas.db"

# ── DDL ─────────────────────────────────────────────────────────────────────

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS broker_orders (
    order_id           TEXT PRIMARY KEY,           -- Alpaca order UUID
    symbol             TEXT NOT NULL,              -- ticker (Atlas format)
    side               TEXT NOT NULL,              -- buy | sell
    qty                REAL NOT NULL,              -- requested qty
    filled_qty         REAL,                       -- actually filled (NULL if not filled)
    fill_price         REAL,                       -- avg fill price (NULL if not filled)
    status             TEXT NOT NULL,              -- accepted | filled | canceled | rejected | etc
    submitted_at       TEXT NOT NULL,              -- ISO timestamp
    filled_at          TEXT,                       -- ISO timestamp (NULL if not filled)
    order_class        TEXT,                       -- simple | bracket | oco | oto
    parent_id          TEXT,                       -- parent order ID for bracket children
    raw_alpaca_json    TEXT NOT NULL,              -- full Alpaca order JSON for forensic
    last_synced_at     TEXT NOT NULL               -- when this row was last upserted
);
"""

CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_broker_orders_symbol ON broker_orders(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_broker_orders_status ON broker_orders(status);",
    "CREATE INDEX IF NOT EXISTS idx_broker_orders_submitted_at ON broker_orders(submitted_at);",
    "CREATE INDEX IF NOT EXISTS idx_broker_orders_parent_id ON broker_orders(parent_id);",
]

ALL_SQL = [CREATE_TABLE_SQL] + CREATE_INDEXES_SQL


def _run(apply: bool) -> None:
    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        # Check if table already exists (idempotency reporting only — SQL is IF NOT EXISTS)
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='broker_orders'"
        ).fetchone()
        table_exists = existing is not None

        print(f"Migration: 2026-04-29-add-broker-orders-table")
        print(f"DB:        {DB_PATH}")
        print(f"Mode:      {'APPLY' if apply else 'DRY-RUN'}")
        print(f"Table broker_orders exists: {table_exists}")
        print()

        for sql in ALL_SQL:
            trimmed = sql.strip()
            print(f"  SQL: {trimmed[:80]}{'...' if len(trimmed) > 80 else ''}")
            if apply:
                conn.executescript(trimmed)

        if apply:
            conn.commit()

            # Verify
            check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='broker_orders'"
            ).fetchone()
            if check:
                print("\n✅ Table broker_orders created (or already existed).")
                # Check indexes
                idx_count = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='broker_orders'"
                ).fetchone()[0]
                print(f"   Indexes on broker_orders: {idx_count}")
            else:
                print("\n❌ ERROR: Table not found after apply!", file=sys.stderr)
                sys.exit(1)
        else:
            print("\n--- Dry-run complete. Run with --apply to execute.")

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
