#!/usr/bin/env python3
"""
Migration: Add UNIQUE partial index on trades(ticker, universe) WHERE status='open'.

Prevents concurrent processes from inserting duplicate open trades for the
same (ticker, universe) pair — the root cause of the CCJ P0-1 incident
(2026-04-24), where sp500 and commodity_etfs syncs both inserted within 11ms.

Idempotent: CREATE UNIQUE INDEX IF NOT EXISTS is a no-op when the index exists.

Pre-flight: checks for existing duplicates first — exits 1 if any remain,
so the migration is safe to re-run without silently masking data issues.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Bootstrap sys.path for running as a standalone script
ATLAS_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ATLAS_ROOT))

from db.atlas_db import get_db  # noqa: E402

_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_unique_open "
    "ON trades(ticker, universe) WHERE status='open'"
)

_PREFLIGHT_SQL = """
    SELECT ticker, universe, COUNT(*) AS cnt
    FROM trades
    WHERE status='open'
    GROUP BY ticker, universe
    HAVING COUNT(*) > 1
"""


def run() -> int:
    with get_db() as db:
        # ── Pre-flight: check for existing duplicates ──────────────────────
        dupes = db.execute(_PREFLIGHT_SQL).fetchall()
        if dupes:
            print("ERROR: pre-flight found duplicate open trades — fix before migrating:")
            for row in dupes:
                print(f"  ticker={row['ticker']}  universe={row['universe']}  count={row['cnt']}")
            return 1

        print("Pre-flight passed: no duplicate open (ticker, universe) pairs.")

        # ── Apply the unique partial index ─────────────────────────────────
        db.execute(_INDEX_DDL)
        db.commit()
        print("Created: idx_trades_unique_open ON trades(ticker, universe) WHERE status='open'")

    return 0


if __name__ == "__main__":
    sys.exit(run())
