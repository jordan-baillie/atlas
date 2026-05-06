#!/usr/bin/env python3
"""Add paper_trades and paper_position_protective_orders tables.

Sub-phase 1.2 of the strategy lifecycle paper-trading rollout.

Run:
    python3 scripts/migrations/2026-05-06-paper-trades-schema.py
    python3 scripts/migrations/2026-05-06-paper-trades-schema.py --dry-run
    python3 scripts/migrations/2026-05-06-paper-trades-schema.py --db-path /tmp/test.db

Idempotent — safe to re-run:
  - CREATE TABLE IF NOT EXISTS  (never fails if table exists)
  - CREATE INDEX IF NOT EXISTS  (never fails if index exists)
  - DROP VIEW IF EXISTS + CREATE VIEW IF NOT EXISTS  (idempotent view refresh)
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

# ── Path bootstrap ────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve()
_ATLAS_ROOT = _HERE.parent.parent.parent
sys.path.insert(0, str(_ATLAS_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("paper_trades_migration")

# ── DDL ───────────────────────────────────────────────────────────────────────

_PAPER_TRADES_DDL = """
CREATE TABLE IF NOT EXISTS paper_trades (
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
    superseded      INTEGER NOT NULL DEFAULT 0 CHECK (superseded IN (0,1)),
    paper_account_id TEXT,
    CHECK (exit_date IS NULL OR exit_date >= entry_date),
    CHECK (
        stop_price IS NULL
        OR (direction = 'long'  AND stop_price < entry_price)
        OR (direction = 'short' AND stop_price > entry_price)
    )
)
"""

_PAPER_TRADES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_status   ON paper_trades(status)",
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_strategy ON paper_trades(strategy)",
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_dates    ON paper_trades(entry_date, exit_date)",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_trades_unique_open
       ON paper_trades(ticker, universe) WHERE status='open'""",
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_trades_active_closed
       ON paper_trades(ticker, strategy, DATE(exit_date), ROUND(pnl, 2))
       WHERE status = 'closed' AND superseded = 0""",
]

_PAPER_TRADES_VIEW_DROP = "DROP VIEW IF EXISTS paper_trades_active"
_PAPER_TRADES_VIEW_CREATE = """
CREATE VIEW IF NOT EXISTS paper_trades_active AS
  SELECT * FROM paper_trades WHERE superseded = 0
"""

_PAPER_PROTECTIVE_DDL = """
CREATE TABLE IF NOT EXISTS paper_position_protective_orders (
    market_id       TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    trade_id        INTEGER,
    position_qty    REAL NOT NULL,
    stop_order_id   TEXT,
    stop_price      REAL,
    tp_order_id     TEXT,
    tp_price        REAL,
    oco_class       TEXT,
    last_synced_at  TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (market_id, ticker)
)
"""

_PAPER_PROTECTIVE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_paper_protective_status   ON paper_position_protective_orders(status)",
    "CREATE INDEX IF NOT EXISTS idx_paper_protective_trade_id ON paper_position_protective_orders(trade_id)",
]

_INDEX_NAMES_TRADES = [
    "idx_paper_trades_status",
    "idx_paper_trades_strategy",
    "idx_paper_trades_dates",
    "idx_paper_trades_unique_open",
    "uq_paper_trades_active_closed",
]

_INDEX_NAMES_PROTECTIVE = [
    "idx_paper_protective_status",
    "idx_paper_protective_trade_id",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Return True if *table* exists in the database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _index_exists(conn: sqlite3.Connection, index: str) -> bool:
    """Return True if *index* exists in the database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index,)
    ).fetchone()
    return row is not None


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    """Return row count for *table*.  Returns 0 if table does not exist."""
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    except sqlite3.OperationalError:
        return 0


# ── Core migration logic ──────────────────────────────────────────────────────

def run(
    db_path: str | Path = _ATLAS_ROOT / "data" / "atlas.db",
    *,
    dry_run: bool = False,
) -> None:
    """Apply (or preview) the migration.

    Args:
        db_path: Path to the SQLite database file.
        dry_run: When True, print what would be done but make no changes.
    """
    db_path = Path(db_path)
    logger.info("DB path: %s | dry_run=%s", db_path, dry_run)

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        # ── paper_trades ──────────────────────────────────────────────────
        pt_existed = _table_exists(conn, "paper_trades")

        if dry_run:
            if pt_existed:
                print("⏭  paper_trades already exists (dry-run, no changes)")
            else:
                print("🔍  paper_trades WOULD BE created (dry-run)")
        else:
            conn.execute(_PAPER_TRADES_DDL)
            if pt_existed:
                print("⏭  paper_trades already exists")
            else:
                print("✅  paper_trades created")

        for ddl, idx_name in zip(_PAPER_TRADES_INDEXES, _INDEX_NAMES_TRADES, strict=True):
            idx_existed = _index_exists(conn, idx_name)
            if dry_run:
                status = "EXISTS" if idx_existed else "WOULD CREATE"
                print(f"  index {idx_name}: {status} (dry-run)")
            else:
                conn.execute(ddl)
                status = "already exists" if idx_existed else "created"
                print(f"  index {idx_name}: {status}")

        # View (always refreshed — idempotent)
        if not dry_run:
            conn.execute(_PAPER_TRADES_VIEW_DROP)
            conn.execute(_PAPER_TRADES_VIEW_CREATE)
            print("  view paper_trades_active: refreshed")
        else:
            print("  view paper_trades_active: WOULD BE refreshed (dry-run)")

        # ── paper_position_protective_orders ──────────────────────────────
        pp_existed = _table_exists(conn, "paper_position_protective_orders")

        if dry_run:
            if pp_existed:
                print("⏭  paper_position_protective_orders already exists (dry-run, no changes)")
            else:
                print("🔍  paper_position_protective_orders WOULD BE created (dry-run)")
        else:
            conn.execute(_PAPER_PROTECTIVE_DDL)
            if pp_existed:
                print("⏭  paper_position_protective_orders already exists")
            else:
                print("✅  paper_position_protective_orders created")

        for ddl, idx_name in zip(_PAPER_PROTECTIVE_INDEXES, _INDEX_NAMES_PROTECTIVE, strict=True):
            idx_existed = _index_exists(conn, idx_name)
            if dry_run:
                status = "EXISTS" if idx_existed else "WOULD CREATE"
                print(f"  index {idx_name}: {status} (dry-run)")
            else:
                conn.execute(ddl)
                status = "already exists" if idx_existed else "created"
                print(f"  index {idx_name}: {status}")

        if not dry_run:
            conn.commit()

        # ── Summary ───────────────────────────────────────────────────────
        print()
        if dry_run:
            print("── dry-run summary (no changes committed) ──")
            print(f"  paper_trades:                        {'EXISTS' if pt_existed else 'WOULD CREATE'}")
            print(f"  paper_position_protective_orders:    {'EXISTS' if pp_existed else 'WOULD CREATE'}")
        else:
            pt_rows = _row_count(conn, "paper_trades")
            pp_rows = _row_count(conn, "paper_position_protective_orders")
            print("── migration summary ──")
            print(f"  paper_trades rows:                       {pt_rows}")
            print(f"  paper_position_protective_orders rows:   {pp_rows}")

        # ── Index verification (post-apply only) ─────────────────────────
        if not dry_run:
            print()
            all_index_names = _INDEX_NAMES_TRADES + _INDEX_NAMES_PROTECTIVE
            missing = [n for n in all_index_names if not _index_exists(conn, n)]
            if missing:
                logger.error("MISSING indexes after migration: %s", missing)
                sys.exit(1)
            else:
                print(f"✅  All {len(all_index_names)} indexes verified present")

    finally:
        conn.close()


# ── CLI entry point ───────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Add paper_trades and paper_position_protective_orders tables to atlas.db",
    )
    parser.add_argument(
        "--db-path",
        default=str(_ATLAS_ROOT / "data" / "atlas.db"),
        help="Path to the SQLite database (default: data/atlas.db relative to project root)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview what the migration would do without making changes",
    )
    return parser


if __name__ == "__main__":
    args = _build_parser().parse_args()
    run(db_path=args.db_path, dry_run=args.dry_run)
