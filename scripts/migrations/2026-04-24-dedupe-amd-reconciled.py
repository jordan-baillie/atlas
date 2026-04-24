"""Remove duplicate open trade rows created by the reconcile_ledger self-heal path.

Context
-------
When reconcile_ledger.py ran for a ticker already in the DB under
strategy='momentum_breakout', it did not check for an existing open row before
calling record_trade_entry().  The DB-level UNIQUE partial index blocks the
second INSERT, but earlier versions of the reconciler may have succeeded in
inserting a second row with strategy='reconciled'.

This migration:
 1. Finds all (ticker, universe) pairs that have >1 open row in `trades`.
 2. For each group, keeps the non-'reconciled' strategy row.  If all rows have
    strategy='reconciled', keeps the oldest (lowest id).
 3. Deletes the extras.

Idempotent: safe to re-run when no duplicates exist (no-op).

Usage
-----
  python3 scripts/migrations/2026-04-24-dedupe-amd-reconciled.py          # dry-run
  python3 scripts/migrations/2026-04-24-dedupe-amd-reconciled.py --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
DB_PATH = PROJECT / "data" / "atlas.db"


def find_duplicates(conn: sqlite3.Connection) -> list[dict]:
    """Return one record per (ticker, universe) that has >1 open row."""
    rows = conn.execute(
        """
        SELECT ticker, universe, COUNT(*) AS cnt
          FROM trades
         WHERE exit_date IS NULL
         GROUP BY ticker, universe
        HAVING cnt > 1
        """
    ).fetchall()
    return [{"ticker": r[0], "universe": r[1], "count": r[2]} for r in rows]


def resolve_duplicates(
    conn: sqlite3.Connection,
    ticker: str,
    universe: str,
    dry_run: bool,
) -> tuple[int, int]:
    """Keep one row for (ticker, universe), delete the rest.

    Priority:
      1. Keep non-'reconciled' strategy (real strategy attribution).
      2. If all 'reconciled', keep the row with the lowest id (oldest insert).

    Returns (kept_id, deleted_count).
    """
    rows = conn.execute(
        """
        SELECT id, strategy, entry_date
          FROM trades
         WHERE ticker = ? AND universe = ? AND exit_date IS NULL
         ORDER BY
               CASE WHEN strategy != 'reconciled' THEN 0 ELSE 1 END,
               id ASC
        """,
        (ticker, universe),
    ).fetchall()

    if not rows:
        return 0, 0

    keep_id = rows[0][0]
    keep_strategy = rows[0][1]
    delete_ids = [r[0] for r in rows[1:]]

    print(
        f"  {ticker}/{universe}: keeping id={keep_id} (strategy={keep_strategy}), "
        f"deleting ids={delete_ids}"
    )

    if dry_run:
        return keep_id, len(delete_ids)

    for del_id in delete_ids:
        conn.execute("DELETE FROM trades WHERE id = ?", (del_id,))
        print(f"    Deleted trade id={del_id}")

    conn.commit()
    return keep_id, len(delete_ids)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute deletes (default: dry-run only)",
    )
    args = parser.parse_args(argv)

    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}")
        return 1

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        dupes = find_duplicates(conn)

        if not dupes:
            print("No duplicate open trades found — database is clean.")
            return 0

        print(f"Found {len(dupes)} (ticker, universe) pair(s) with duplicate open rows:")
        for d in dupes:
            print(f"  {d['ticker']}/{d['universe']}: {d['count']} open rows")

        if not args.apply:
            print("\nDRY-RUN: showing what would be kept/deleted per group:")

        total_deleted = 0
        for d in dupes:
            _, deleted = resolve_duplicates(
                conn, d["ticker"], d["universe"], dry_run=not args.apply
            )
            total_deleted += deleted

        if args.apply:
            print(f"\nDone. Deleted {total_deleted} duplicate row(s).")
            # Verify
            remaining = find_duplicates(conn)
            if remaining:
                print(f"ERROR: {len(remaining)} duplicate(s) still remain after cleanup!")
                return 1
            print("Verification OK: no duplicate open rows remain.")
        else:
            print(f"\nDRY-RUN: would delete {total_deleted} row(s). Pass --apply to execute.")

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
