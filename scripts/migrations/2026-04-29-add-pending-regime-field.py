"""
Migration: add pending_state column to regime_history

Adds a nullable TEXT column `pending_state` to `regime_history`.
This column stores the raw (unconfirmed) regime classification when
the N-day confirmation gate is active and the regime change has not
yet been confirmed by N consecutive same-state classifications.

Idempotent — safe to run multiple times.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from db.atlas_db import get_db


def run() -> None:
    with get_db() as conn:
        # Check whether column already exists.
        cols = [row[1] for row in conn.execute("PRAGMA table_info(regime_history)").fetchall()]
        if "pending_state" in cols:
            print("pending_state column already exists — nothing to do.")
            return

        conn.execute(
            "ALTER TABLE regime_history ADD COLUMN pending_state TEXT DEFAULT NULL"
        )
        conn.commit()
        print("Migration complete: regime_history.pending_state TEXT DEFAULT NULL added.")


if __name__ == "__main__":
    run()
