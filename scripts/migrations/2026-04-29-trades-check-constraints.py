#!/usr/bin/env python3
"""
Migration: 2026-04-29-trades-check-constraints.py

Adds CHECK constraints to the `trades` table that enforce canonical-state
invariants.  SQLite cannot add CHECKs via ALTER TABLE, so this migration
uses the standard table-rebuild pattern (CREATE_new → INSERT → DROP → RENAME).

Existing constraints (already present, carried through):
  • stop_price < entry_price for longs / > entry_price for shorts (Apr-27 audit)
  • exit_date >= entry_date (Phase-0 MU close)

New constraints added here:
  1. Closed-trade completeness: status='closed' → exit_price IS NOT NULL AND exit_date IS NOT NULL
  2. Open-trade completeness:   status='open'   → entry_price > 0 AND shares > 0
  3. Status domain:             status IN ('open','closed','cancelled','pending')

Usage:
    python3 scripts/migrations/2026-04-29-trades-check-constraints.py          # dry-run
    python3 scripts/migrations/2026-04-29-trades-check-constraints.py --apply
    python3 scripts/migrations/2026-04-29-trades-check-constraints.py --apply --allow-fix
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import NamedTuple

ATLAS_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ATLAS_ROOT / "data" / "atlas.db"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Pre-flight queries ────────────────────────────────────────────────────────

PREFLIGHT: list[tuple[str, str]] = [
    (
        "closed_no_exit_fields",
        "SELECT id, ticker, status, exit_price, exit_date "
        "FROM trades WHERE status='closed' AND (exit_price IS NULL OR exit_date IS NULL)",
    ),
    (
        "open_bad_entry_fields",
        "SELECT id, ticker, status, entry_price, shares "
        "FROM trades WHERE status='open' "
        "AND (entry_price IS NULL OR entry_price <= 0 OR shares IS NULL OR shares <= 0)",
    ),
    (
        "stop_price_violates_direction",
        "SELECT id, ticker, direction, entry_price, stop_price "
        "FROM trades WHERE stop_price IS NOT NULL "
        "AND NOT ("
        "  (direction = 'long'  AND stop_price < entry_price) OR "
        "  (direction = 'short' AND stop_price > entry_price)"
        ")",
    ),
    (
        "exit_before_entry",
        "SELECT id, ticker, entry_date, exit_date "
        "FROM trades WHERE exit_date IS NOT NULL AND exit_date < entry_date",
    ),
    (
        "unknown_status",
        "SELECT id, ticker, status "
        "FROM trades WHERE status NOT IN ('open','closed','cancelled','pending')",
    ),
]

# ── Auto-fix definitions ──────────────────────────────────────────────────────
# Applied in order when --allow-fix is set.

_AUTOFIX_SQL: list[tuple[str, str, str]] = [
    # (check_name, description, sql)
    (
        "closed_no_exit_fields",
        "Set exit_price=entry_price and exit_date=entry_date for closed trades missing exits",
        "UPDATE trades SET "
        "  exit_price = entry_price, "
        "  exit_date  = entry_date "
        "WHERE status='closed' AND (exit_price IS NULL OR exit_date IS NULL)",
    ),
]

# ── New DDL ───────────────────────────────────────────────────────────────────

# Full CREATE TABLE statement for the rebuilt table, carrying ALL existing
# columns and constraints + the three new CHECKs.
CREATE_TRADES_NEW_SQL = """\
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
    superseded      INTEGER NOT NULL DEFAULT 0,
    -- ── Pre-existing constraints ─────────────────────────────────────────
    CHECK (superseded IN (0, 1)),
    CHECK (exit_date IS NULL OR exit_date >= entry_date),
    CHECK (
        stop_price IS NULL
        OR (direction = 'long'  AND stop_price < entry_price)
        OR (direction = 'short' AND stop_price > entry_price)
    ),
    -- ── NEW: Phase B.1 constraints ───────────────────────────────────────
    -- C1: closed trades must carry exit fields
    CHECK (
        status != 'closed'
        OR (exit_price IS NOT NULL AND exit_date IS NOT NULL)
    ),
    -- C2: open trades must carry valid entry fields
    CHECK (
        status != 'open'
        OR (entry_price IS NOT NULL AND entry_price > 0
            AND shares IS NOT NULL AND shares > 0)
    ),
    -- C5: status domain
    CHECK (status IN ('open', 'closed', 'cancelled', 'pending'))
)"""

INSERT_FROM_OLD_SQL = """\
INSERT INTO trades_new
    SELECT id, ticker, strategy, universe, direction,
           entry_date, entry_price, shares, stop_price, take_profit,
           exit_date, exit_price, exit_reason, pnl, pnl_pct,
           mae, mfe, hold_days, confidence, regime_at_entry, regime_at_exit,
           status, config_version, created_at, updated_at,
           stop_order_id, tp_order_id, superseded
    FROM trades"""

DROP_OLD_SQL = "DROP TABLE trades"
RENAME_SQL   = "ALTER TABLE trades_new RENAME TO trades"

RECREATE_INDEXES_SQL: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_trades_status   ON trades(status)",
    "CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy)",
    "CREATE INDEX IF NOT EXISTS idx_trades_dates    ON trades(entry_date, exit_date)",
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_unique_open "
        "ON trades(ticker, universe) WHERE status='open'"
    ),
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_active_closed "
        "ON trades(ticker, strategy, DATE(exit_date), ROUND(pnl, 2)) "
        "WHERE status='closed' AND superseded=0"
    ),
]

# Views that reference trades — must be dropped before DROP TABLE, recreated after RENAME
_TRADES_VIEWS: list[tuple[str, str]] = [
    (
        "trades_active",
        "CREATE VIEW IF NOT EXISTS trades_active AS SELECT * FROM trades WHERE superseded = 0",
    ),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _constraints_already_present(conn: sqlite3.Connection) -> bool:
    """True if the new CHECK constraints are already in the trades schema."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if row is None:
        return False
    ddl: str = row[0] or ""
    # If all three new constraint markers are present the migration already ran.
    markers = [
        "status != 'closed'",       # C1
        "status != 'open'",         # C2
        "status IN ('open', 'closed', 'cancelled', 'pending')",  # C5
    ]
    return all(m in ddl for m in markers)


def _run_preflight(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Run all pre-flight queries and return {check_name: [rows]}."""
    results: dict[str, list[dict]] = {}
    for name, sql in PREFLIGHT:
        rows = conn.execute(sql).fetchall()
        results[name] = [dict(r) for r in rows]
    return results


def _print_preflight(results: dict[str, list[dict]]) -> None:
    print("\n=== Pre-flight audit ===")
    total_violations = 0
    for name, rows in results.items():
        if rows:
            print(f"\n  ❌  {name}: {len(rows)} violation(s)")
            for r in rows[:5]:
                print(f"      {dict(r)}")
            if len(rows) > 5:
                print(f"      ... ({len(rows) - 5} more)")
            total_violations += len(rows)
        else:
            print(f"  ✓   {name}: OK")
    print()
    if total_violations == 0:
        print("  All pre-flight checks PASSED — safe to proceed.\n")
    else:
        print(f"  {total_violations} violation(s) found — run with --allow-fix to auto-correct, or fix manually.\n")


def _apply_autofixes(conn: sqlite3.Connection, violations: dict[str, list[dict]]) -> None:
    for check_name, desc, sql in _AUTOFIX_SQL:
        if not violations.get(check_name):
            continue
        count = len(violations[check_name])
        logger.warning("AUTO-FIX [%s]: %s (%d rows)", check_name, desc, count)
        conn.execute(sql)
    conn.commit()
    logger.info("Auto-fixes committed.")


# ── Main ──────────────────────────────────────────────────────────────────────

def _run(apply: bool, allow_fix: bool) -> int:
    if not DB_PATH.exists():
        logger.error("DB not found at %s", DB_PATH)
        return 1

    conn = _connect()

    # ── Idempotency guard ────────────────────────────────────────────────
    if _constraints_already_present(conn):
        print("Migration already applied (constraints detected in schema). Nothing to do.")
        conn.close()
        return 0

    print("Migration: 2026-04-29-trades-check-constraints")
    print(f"Mode:      {'APPLY' + (' + allow-fix' if allow_fix else '') if apply else 'DRY-RUN'}")
    print(f"DB:        {DB_PATH}")

    # ── Pre-flight ───────────────────────────────────────────────────────
    violations = _run_preflight(conn)
    _print_preflight(violations)

    total_viol = sum(len(v) for v in violations.values())
    if not apply:
        print("--- Dry-run complete. Pass --apply (optionally --allow-fix) to execute.")
        conn.close()
        return 0

    if total_viol > 0:
        if not allow_fix:
            logger.error(
                "%d violation(s) found. Pass --allow-fix to auto-correct, or fix manually first.",
                total_viol,
            )
            conn.close()
            return 1
        logger.info("Applying auto-fixes before migration ...")
        _apply_autofixes(conn, violations)
        # Re-check after fixes
        violations = _run_preflight(conn)
        remaining = sum(len(v) for v in violations.values())
        if remaining > 0:
            logger.error(
                "After auto-fix, %d violation(s) remain — manual intervention needed.",
                remaining,
            )
            _print_preflight(violations)
            conn.close()
            return 1
        logger.info("All violations resolved by auto-fix.")

    # ── Count rows before rebuild ────────────────────────────────────────
    row_count_before: int = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    logger.info("Row count before rebuild: %d", row_count_before)

    # ── Table rebuild (transactional) ────────────────────────────────────
    print("\n=== Executing table rebuild ===")
    try:
        conn.execute("BEGIN EXCLUSIVE")

        # Drop views that depend on the trades table
        for view_name, _ in _TRADES_VIEWS:
            print(f"  Drop view {view_name} ...")
            conn.execute(f"DROP VIEW IF EXISTS {view_name}")

        steps = [
            ("Create trades_new with CHECK constraints", CREATE_TRADES_NEW_SQL),
            ("Copy rows from trades → trades_new",        INSERT_FROM_OLD_SQL),
            ("Drop old trades table",                     DROP_OLD_SQL),
            ("Rename trades_new → trades",                RENAME_SQL),
        ]
        for desc, sql in steps:
            print(f"  {desc} ...")
            conn.execute(sql)

        for idx_sql in RECREATE_INDEXES_SQL:
            print(f"  Recreating index: {idx_sql[:60]}...")
            conn.execute(idx_sql)

        # Recreate views
        for view_name, view_sql in _TRADES_VIEWS:
            print(f"  Recreating view {view_name} ...")
            conn.execute(view_sql)

        conn.execute("COMMIT")

    except Exception as exc:
        conn.execute("ROLLBACK")
        logger.error("Migration FAILED — rolled back. Error: %s", exc)
        conn.close()
        return 1

    # ── Post-migration verification ──────────────────────────────────────
    row_count_after: int = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    logger.info("Row count after rebuild: %d", row_count_after)

    if row_count_after != row_count_before:
        logger.error(
            "Row count mismatch! before=%d after=%d",
            row_count_before,
            row_count_after,
        )
        conn.close()
        return 1

    schema_after: str = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()[0]
    if not _constraints_already_present(conn):
        logger.error("Constraints NOT found in schema after migration — something went wrong.")
        conn.close()
        return 1

    print("\n=== Migration COMPLETE ===")
    print(f"  Rows preserved: {row_count_after}")
    print("  CHECK constraints verified in schema.")
    print("\n  New constraints:")
    print("    C1: status='closed' → exit_price IS NOT NULL AND exit_date IS NOT NULL")
    print("    C2: status='open'   → entry_price > 0 AND shares > 0")
    print("    C5: status IN ('open','closed','cancelled','pending')")
    print()

    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Add CHECK constraints to the trades table (table-rebuild migration)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Execute the migration (default is dry-run).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print pre-flight results only (default behaviour — same as omitting --apply).",
    )
    parser.add_argument(
        "--allow-fix",
        action="store_true",
        default=False,
        help="Auto-correct known patterns before migrating (e.g. NULL exit_price on closed trades).",
    )
    args = parser.parse_args(argv)
    return _run(apply=args.apply, allow_fix=args.allow_fix)


if __name__ == "__main__":
    sys.exit(main())

# This line is a placeholder — see the edit below
