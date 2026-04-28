#!/usr/bin/env python3
"""
Migration: 2026-04-29-add-tp-orders-rca-1a.py

Records the OCO TP+SL order IDs placed on 2026-04-29 for GLD, XLI, XLY.
These positions were TP-naked for 5+ days (no take_profit order).
Trailing stops were replaced with OCO pairs (stop + limit TP).

RCA ticket: Phase 1A
Placed by: Backend Developer, 2026-04-29

Usage:
    python3 scripts/migrations/2026-04-29-add-tp-orders-rca-1a.py          # dry-run
    python3 scripts/migrations/2026-04-29-add-tp-orders-rca-1a.py --apply  # apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ATLAS_ROOT / "data" / "atlas.db"

# OCO orders placed on 2026-04-29:
#   Parent = LIMIT SELL (TP), status=accepted
#   Child leg = STOP SELL (SL), status=held
RECORDS = [
    {
        "trade_id":       135,
        "ticker":         "GLD",
        "universe":       "commodity_etfs",
        "entry_price":    442.80,
        "take_profit":    509.22,          # entry × 1.15 (15% fallback)
        "tp_order_id":    "f8d94dbf-4f90-48a4-8adf-73e087362bba",  # LIMIT SELL parent
        "stop_price":     420.66,          # Fixed OCO stop (original DB level)
        "stop_order_id":  "1ba45d16-4443-46b4-b3bd-bde720c77c46",  # STOP SELL leg
    },
    {
        "trade_id":       185,
        "ticker":         "XLI",
        "universe":       "sector_etfs",
        "entry_price":    173.97,
        "take_profit":    200.07,          # entry × 1.15
        "tp_order_id":    "76a50f20-6028-4539-a943-cfef3c10fa1d",
        "stop_price":     169.23,          # Fixed OCO stop (original DB level rounded)
        "stop_order_id":  "d0813b83-2604-4eac-9c4d-20d43d083f1b",
    },
    {
        "trade_id":       167,
        "ticker":         "XLY",
        "universe":       "sector_etfs",
        "entry_price":    116.44,
        "take_profit":    133.91,          # entry × 1.15
        "tp_order_id":    "c2208ed3-5912-4f3f-9cac-78fe2e24ed22",
        "stop_price":     116.03,          # Updated to ratcheted trailing stop level
        "stop_order_id":  "45a288c3-2a27-44eb-8307-deaded0b9b0c",
    },
]


def _run(apply: bool) -> None:
    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row

    now = datetime.now(timezone.utc).isoformat()
    applied = 0
    skipped = 0

    for rec in RECORDS:
        row = conn.execute(
            "SELECT take_profit, tp_order_id, stop_order_id, stop_price "
            "FROM trades WHERE id=? AND status='open'",
            (rec["trade_id"],),
        ).fetchone()

        if row is None:
            print(f"  SKIP {rec['ticker']} (trade_id={rec['trade_id']}): not found or not open")
            skipped += 1
            continue

        # Idempotency: already set → skip
        if row["tp_order_id"] == rec["tp_order_id"]:
            print(f"  SKIP {rec['ticker']} (trade_id={rec['trade_id']}): tp_order_id already set")
            skipped += 1
            continue

        print(
            f"  {'APPLY' if apply else 'DRY-RUN'} {rec['ticker']} "
            f"(trade_id={rec['trade_id']}): "
            f"take_profit={rec['take_profit']}, "
            f"tp_order_id={rec['tp_order_id'][:8]}..., "
            f"stop_price={rec['stop_price']}, "
            f"stop_order_id={rec['stop_order_id'][:8]}..."
        )

        if apply:
            conn.execute(
                """
                UPDATE trades
                SET take_profit    = ?,
                    tp_order_id    = ?,
                    stop_price     = ?,
                    stop_order_id  = ?,
                    updated_at     = ?
                WHERE id = ? AND status = 'open'
                """,
                (
                    rec["take_profit"],
                    rec["tp_order_id"],
                    rec["stop_price"],
                    rec["stop_order_id"],
                    now,
                    rec["trade_id"],
                ),
            )
            applied += 1

    if apply:
        conn.commit()
        print(f"\nApplied {applied} updates, skipped {skipped}.")
    else:
        print(f"\nDry-run: {applied} would be applied, {skipped} already set/missing.")
        print("Run with --apply to execute.")

    conn.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply the DB updates (default: dry-run only)",
    )
    args = parser.parse_args(argv)
    _run(apply=args.apply)


if __name__ == "__main__":
    main()
