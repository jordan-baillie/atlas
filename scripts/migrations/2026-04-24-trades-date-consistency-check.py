#!/usr/bin/env python3
"""
Migration: 2026-04-24 — Add CHECK (exit_date IS NULL OR exit_date >= entry_date) to trades.

SQLite does not support ALTER TABLE ADD CONSTRAINT, so we must recreate the table.
All existing indexes are preserved.

Pre-flight: aborts if any inverted-date rows remain (fix P1-7 data first).
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "atlas.db"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ── Exact DDL for the rebuilt table ─────────────────────────────────────────

_TRADES_NEW_DDL = """
CREATE TABLE trades_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    strategy        TEXT    NOT NULL,
    universe        TEXT,
    direction       TEXT    DEFAULT 'long',
    entry_date      TEXT    NOT NULL,
    entry_price     REAL    NOT NULL,
    shares          INTEGER NOT NULL,
    stop_price      REAL,
    take_profit     REAL,
    exit_date       TEXT,
    exit_price      REAL,
    exit_reason     TEXT,
    pnl             REAL,
    pnl_pct         REAL,
    mae             REAL,
    mfe             REAL,
    hold_days       INTEGER,
    confidence      REAL,
    regime_at_entry TEXT,
    regime_at_exit  TEXT,
    status          TEXT    DEFAULT 'open',
    config_version  TEXT,
    created_at      TEXT    DEFAULT (datetime('now')),
    updated_at      TEXT    DEFAULT (datetime('now')),
    stop_order_id   TEXT    DEFAULT '',
    tp_order_id     TEXT    DEFAULT '',
    CHECK (exit_date IS NULL OR exit_date >= entry_date)
)
"""

_INDEX_DDLS = [
    "CREATE INDEX idx_trades_status ON trades_new(status)",
    "CREATE INDEX idx_trades_strategy ON trades_new(strategy)",
    "CREATE INDEX idx_trades_dates ON trades_new(entry_date, exit_date)",
    "CREATE UNIQUE INDEX idx_trades_unique_open ON trades_new(ticker, universe) WHERE status='open'",
]


def _preflight(conn: sqlite3.Connection) -> None:
    """Abort if any inverted-date rows remain."""
    rows = conn.execute(
        "SELECT id, ticker, entry_date, exit_date FROM trades "
        "WHERE exit_date IS NOT NULL AND exit_date < entry_date"
    ).fetchall()
    if rows:
        logger.error("Pre-flight FAILED — %d inverted-date trade(s) remain:", len(rows))
        for row in rows:
            logger.error("  id=%s %s: entry=%s exit=%s", *row)
        logger.error("Fix P1-7 data first, then re-run this migration.")
        sys.exit(1)
    logger.info("Pre-flight OK — 0 inverted-date trades.")


def _run_migration(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN")
    try:
        # 1. Create trades_new with CHECK constraint
        conn.execute(_TRADES_NEW_DDL)
        logger.info("Created trades_new with CHECK constraint.")

        # 2. Copy all rows
        conn.execute(
            """
            INSERT INTO trades_new
            SELECT id, ticker, strategy, universe, direction,
                   entry_date, entry_price, shares, stop_price, take_profit,
                   exit_date, exit_price, exit_reason, pnl, pnl_pct,
                   mae, mfe, hold_days, confidence,
                   regime_at_entry, regime_at_exit, status, config_version,
                   created_at, updated_at, stop_order_id, tp_order_id
            FROM trades
            """
        )
        count = conn.execute("SELECT COUNT(*) FROM trades_new").fetchone()[0]
        logger.info("Copied %d rows into trades_new.", count)

        # 3. Drop old table + rename
        conn.execute("DROP TABLE trades")
        conn.execute("ALTER TABLE trades_new RENAME TO trades")
        logger.info("Renamed trades_new → trades.")

        # 4. Recreate indexes (names already reference 'trades' after rename)
        for ddl in _INDEX_DDLS:
            # Replace trades_new with trades for index names — already correct above
            final_ddl = ddl.replace(" ON trades_new(", " ON trades(")
            conn.execute(final_ddl)
        logger.info("Recreated %d indexes.", len(_INDEX_DDLS))

        conn.execute("COMMIT")
        logger.info("Migration committed successfully.")
    except Exception:
        conn.execute("ROLLBACK")
        logger.exception("Migration FAILED — rolled back.")
        raise


def main() -> None:
    logger.info("Migration: trades CHECK constraint — DB=%s", DB_PATH)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")  # Disable during table swap
    try:
        _preflight(conn)
        _run_migration(conn)
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()

    # Verify
    conn2 = sqlite3.connect(str(DB_PATH))
    schema = conn2.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()[0]
    indexes = conn2.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='trades'"
    ).fetchall()
    conn2.close()

    if "CHECK" in schema:
        logger.info("Verified: CHECK constraint present in schema.")
    else:
        logger.error("CHECK constraint NOT found in schema after migration!")
        sys.exit(1)

    logger.info("Indexes present: %s", [r[0] for r in indexes])
    logger.info("Migration complete.")


if __name__ == "__main__":
    main()
