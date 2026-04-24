#!/usr/bin/env python3
"""Expire pending plans older than 14 days.

Idempotent — already-expired rows are not re-processed; running twice
produces zero changes on the second call.

A timestamped backup of atlas.db is written to data/backups/ before any
mutation so the operation can be rolled back if needed.

Usage:
    python3 scripts/cleanup_stale_plans.py
"""
from __future__ import annotations

import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from db.atlas_db import get_db  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

_DB_PATH = PROJECT / "data" / "atlas.db"
_BACKUP_DIR = PROJECT / "data" / "backups"


def _backup_db() -> Path:
    """Copy atlas.db to data/backups/ with a UTC timestamp suffix."""
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dest = _BACKUP_DIR / f"atlas.db.cleanup-stale-plans-{ts}"
    shutil.copy2(_DB_PATH, dest)
    logger.info("DB backup written: %s", dest)
    return dest


def expire_stale_plans() -> int:
    """Mark pending plans older than 14 days as expired.

    Only touches rows where ``status = 'pending'``.  Plans with any other
    status (approved, executed, rejected, expired) are never modified.

    Returns:
        Number of rows updated (0 when called a second time — idempotent).
    """
    with get_db() as db:
        cur = db.execute(
            """
            UPDATE plans
               SET status = 'expired'
             WHERE status = 'pending'
               AND created_at < datetime('now', '-14 days')
            """,
        )
        count = cur.rowcount
    return count


def main() -> None:
    _backup_db()
    count = expire_stale_plans()
    logger.info("Expired %d stale pending plan(s).", count)
    print(f"Expired {count} stale pending plan(s).")


if __name__ == "__main__":
    main()
