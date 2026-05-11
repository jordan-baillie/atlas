#!/usr/bin/env python3
"""Seed market_equity_history with ASX passive Moomoo balance.

ASX is passive (Moomoo manual holdings, no live broker adapter). This script
inserts a placeholder equity row into market_equity_history so the dashboard
surfaces it correctly.

Idempotent via INSERT OR REPLACE on PK (date, market_id). Run daily until
Moomoo ingestion is implemented.

Usage:
    python3 scripts/seed_asx_equity.py                    # today's row
    python3 scripts/seed_asx_equity.py --equity 2800.00   # override amount

Audit ref: F-05 (ASX equity not surfaced in dashboard)
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
from pathlib import Path

# Bootstrap sys.path for standalone execution
_ATLAS_ROOT = Path(__file__).resolve().parents[1]
if str(_ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATLAS_ROOT))

from db.atlas_db import get_db

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

_ASX_CONFIG = _ATLAS_ROOT / "config" / "active" / "asx.json"
_DEFAULT_EQUITY = 2681.65  # Moomoo AU account balance (manual, as of 2026-05-11)


def _read_config_equity() -> float:
    """Read starting_equity from asx.json, fall back to default."""
    try:
        with open(_ASX_CONFIG) as f:
            cfg = json.load(f)
        val = cfg.get("risk", {}).get("starting_equity") or 0
        if float(val) > 0:
            return float(val)
    except Exception as e:
        logger.debug("Could not read asx.json starting_equity: %s", e)
    return _DEFAULT_EQUITY


def seed_asx_equity(equity: float | None = None, date: str | None = None) -> dict:
    """Insert/replace ASX equity row in market_equity_history.

    Args:
        equity: Amount in AUD. If None, reads from config or uses default.
        date:   ISO date string (YYYY-MM-DD). Defaults to today (UTC).

    Returns:
        dict with 'date', 'equity', 'inserted' (bool).
    """
    amount = equity if equity is not None else _read_config_equity()
    target_date = date or datetime.date.today().isoformat()
    snapshot_time = datetime.datetime.utcnow().isoformat()

    with get_db() as db:
        # Check if row already exists
        existing = db.execute(
            "SELECT allocated_equity FROM market_equity_history "
            "WHERE date=? AND market_id='asx'",
            (target_date,),
        ).fetchone()

        db.execute(
            """INSERT OR REPLACE INTO market_equity_history
                   (date, market_id, allocated_equity, position_mv, cash_attributed,
                    broker_equity, broker_cash, snapshot_time)
               VALUES (?, 'asx', ?, ?, 0.0, ?, 0.0, ?)""",
            (target_date, amount, amount, amount, snapshot_time),
        )

    was_update = existing is not None
    logger.info(
        "%s ASX equity row: date=%s, allocated_equity=%.2f",
        "Updated" if was_update else "Inserted",
        target_date,
        amount,
    )
    return {"date": target_date, "equity": amount, "inserted": not was_update}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--equity", type=float, default=None, help="Override equity amount (AUD)")
    parser.add_argument("--date", default=None, help="Override date (YYYY-MM-DD, default: today)")
    parser.add_argument("--verify", action="store_true", help="Print current ASX row after seeding")
    args = parser.parse_args(argv)

    result = seed_asx_equity(equity=args.equity, date=args.date)
    logger.info("Done: %s", result)

    if args.verify:
        with get_db() as db:
            rows = db.execute(
                "SELECT date, market_id, allocated_equity, broker_equity, snapshot_time "
                "FROM market_equity_history WHERE market_id='asx' ORDER BY date DESC LIMIT 5"
            ).fetchall()
            print("\nASX market_equity_history rows (latest 5):")
            for r in rows:
                print(f"  {dict(r)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
