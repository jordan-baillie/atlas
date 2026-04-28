#!/usr/bin/env python3
"""M3 migration: create overlay_shadow_log table and indexes.

Idempotent — safe to run multiple times (uses IF NOT EXISTS).

Usage:
    python3 -m scripts.migrations.2026-04-28-overlay-shadow-log
"""
from __future__ import annotations

import sys
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS overlay_shadow_log (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id                     TEXT    NOT NULL,
    ticker                      TEXT    NOT NULL,
    market_id                   TEXT    NOT NULL,
    created_at                  TEXT    NOT NULL DEFAULT (datetime('now')),
    original_size               REAL    NOT NULL,
    overlay_size                REAL    NOT NULL,
    sizing_multiplier           REAL    NOT NULL,
    would_be_dollar_diff        REAL,
    overlay_decision_id         INTEGER,
    overlay_action              TEXT,
    overlay_reasoning           TEXT,
    actual_outcome_pnl          REAL,
    actual_outcome_evaluated    INTEGER NOT NULL DEFAULT 0,
    evaluated_at                TEXT,
    FOREIGN KEY (overlay_decision_id) REFERENCES overlay_decisions(id)
)
"""

_CREATE_IDX_UNEVALUATED = """
CREATE INDEX IF NOT EXISTS idx_shadow_unevaluated
    ON overlay_shadow_log(actual_outcome_evaluated, created_at)
"""

_CREATE_IDX_PLAN = """
CREATE INDEX IF NOT EXISTS idx_shadow_plan
    ON overlay_shadow_log(plan_id)
"""


def run_migration() -> None:
    """Create the overlay_shadow_log table and indexes (idempotent)."""
    from db.atlas_db import get_db  # type: ignore

    print("Running M3 migration: overlay_shadow_log …")

    with get_db() as db:
        db.execute(_CREATE_TABLE_SQL)
        db.execute(_CREATE_IDX_UNEVALUATED)
        db.execute(_CREATE_IDX_PLAN)

        # Verify
        row = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='overlay_shadow_log'"
        ).fetchone()
        if row is None:
            raise RuntimeError("Migration failed: overlay_shadow_log table not found after CREATE")

        idx_rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='overlay_shadow_log'"
        ).fetchall()
        idx_names = {r["name"] for r in idx_rows}

    print(f"  Table  : overlay_shadow_log — OK")
    print(f"  Indexes: {sorted(idx_names)}")
    print("Migration complete.\n")


if __name__ == "__main__":
    run_migration()
