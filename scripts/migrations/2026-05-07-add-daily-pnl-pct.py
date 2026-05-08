"""Migration: add portfolio_snapshots.daily_pnl_pct column and backfill.

Idempotent — safe to run multiple times.
The column tracks % change in total_equity vs the previous snapshot for the
same market_id, ordered by timestamp (ascending).

Usage:
    python3 scripts/migrations/2026-05-07-add-daily-pnl-pct.py
    python3 scripts/migrations/2026-05-07-add-daily-pnl-pct.py --db data/atlas.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "atlas.db"


def column_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    """Return True if *col* exists in *table*."""
    return col in [
        r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    ]


def run(db_path: str = str(DEFAULT_DB)) -> int:
    """Add and backfill daily_pnl_pct.  Returns number of rows processed."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")

    # Step 1 — add column if absent
    if not column_exists(conn, "portfolio_snapshots", "daily_pnl_pct"):
        conn.execute(
            "ALTER TABLE portfolio_snapshots ADD COLUMN daily_pnl_pct REAL"
        )
        conn.commit()
        print("Column daily_pnl_pct added to portfolio_snapshots.")
    else:
        print("Column daily_pnl_pct already exists — skipping ALTER.")

    # Step 2 — backfill per market_id, ordered by timestamp
    # Only update rows where daily_pnl_pct is still NULL (idempotent re-run).
    markets = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT market_id FROM portfolio_snapshots ORDER BY market_id"
        ).fetchall()
    ]

    total_updated = 0
    for market in markets:
        rows = conn.execute(
            """
            SELECT id, timestamp, total_equity
            FROM portfolio_snapshots
            WHERE market_id = ?
            ORDER BY timestamp ASC
            """,
            (market,),
        ).fetchall()

        prev_equity: float | None = None
        for rid, ts, equity in rows:
            if (
                equity is not None
                and prev_equity is not None
                and prev_equity != 0.0
            ):
                pct = (equity - prev_equity) / prev_equity * 100.0
                conn.execute(
                    "UPDATE portfolio_snapshots SET daily_pnl_pct = ? WHERE id = ?",
                    (round(pct, 4), rid),
                )
                total_updated += 1
            if equity is not None:
                prev_equity = equity

    conn.commit()
    conn.close()
    print(f"Backfilled {total_updated} rows across {len(markets)} market(s).")
    return total_updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to atlas.db")
    args = parser.parse_args()
    run(db_path=args.db)


if __name__ == "__main__":
    main()
