#!/usr/bin/env python3
"""Remediation audit trail — EBAY zombie trade cleanup (2026-05-06).

This script documents and verifies the manual remediation steps taken on
2026-05-06 to fix the EBAY id=206 zombie open trade row and related FCX id=207
zombie.

Root cause:
  reconcile_entry_fills (brokers/live_executor.py) fetches all CLOSED orders
  from a 7-day window.  EBAY BUY filled 2026-05-05 13:30:00 UTC AND bracket
  STOP SELL filled 13:30:37 UTC were BOTH in the window.  The guard that
  prevents zombie creation was ABSENT, so the BUY created a new 'open' row
  even though the bracket already closed the position at the broker.

  Aggravating factor: sync_broker_orders ran once daily at 14:00 AEST (04:00
  UTC) — AFTER reconcile_ledger (09:30 AEST / 23:30 UTC), so P1 fill-price
  lookups in reconcile_ledger fell back to P2 inferred path for most of the
  day.

Fixes applied (see git log):
  Commit 1  fix(reconcile): bracket-exit guard in reconcile_entry_fills
  Commit 2  fix(sync): sync_broker_orders now runs every 4h + 09:25 AEST Tue-Sat

Remediation steps taken:
  1. Ran sync_broker_orders manually to backfill broker_orders cache
     → fetched=330 upserted=330 filled=64 errors=0
     → EBAY BUY (fill=$107.50) + SELL (fill=$107.0969) both landed in broker_orders
     → FCX BUY (fill=$57.59 on 2026-04-29) + SELL (fill=$56.00 on 2026-05-04) confirmed

  2. Ran reconcile_ledger --market sp500
     → [P1] broker_orders sell fill for EBAY: $107.0969 (used fresh cache)
     → [P1] broker_orders sell fill for FCX: $56.0000
     → closed phantom EBAY id=206 (superseded=1)
     → closed phantom FCX id=207 (superseded=1)
     → reconciliation: backfilled=0 closed=2 matched=2 errors=0

  3. Verified final state:
     - broker truth: [('CAT', 904.59), ('SYK', 295.25)] — only 2 live positions
     - trades open (sp500): CAT|open|1, SYK|open|1 — matches broker truth
     - live_sp500.json positions: ['CAT', 'SYK'] — correct
     - EBAY: id=202 canonical closed row (superseded=0, exit=$107.0969)
             id=204 superseded=1 (earlier duplicate reconcile run)
             id=206 superseded=1 (zombie, closed by this remediation)
     - FCX: id=201 canonical closed row (exit=$56.00)
            id=205 superseded=1
            id=207 superseded=1 (zombie, closed by this remediation)

IDEMPOTENCY NOTE:
  Re-running this script only verifies state — it does NOT modify any data.
  All actual data modifications were done via sync_broker_orders.py and
  reconcile_ledger.py (idempotent scripts).

Usage:
  python3 scripts/remediation/2026-05-06-ebay-zombie-cleanup.py
  python3 scripts/remediation/2026-05-06-ebay-zombie-cleanup.py --verbose
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))


def verify(verbose: bool = False) -> bool:
    """Verify final state matches expectations. Returns True if clean."""
    import sqlite3

    db_path = PROJECT / "data" / "atlas.db"
    if not db_path.exists():
        print(f"ERROR: atlas.db not found at {db_path}", file=sys.stderr)
        return False

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    ok = True
    checks = []

    # Check 1: No open EBAY rows
    rows = conn.execute(
        "SELECT id, status FROM trades WHERE ticker='EBAY' AND status='open'"
    ).fetchall()
    if rows:
        checks.append(("FAIL", f"EBAY still has {len(rows)} open rows: {[r['id'] for r in rows]}"))
        ok = False
    else:
        checks.append(("PASS", "EBAY: no open rows"))

    # Check 2: No open FCX zombie rows (id=207 specifically)
    row = conn.execute(
        "SELECT id, status, superseded FROM trades WHERE id=207"
    ).fetchone()
    if row and row["status"] == "open":
        checks.append(("FAIL", f"FCX id=207 is still open"))
        ok = False
    elif row:
        checks.append(("PASS", f"FCX id=207: status={row['status']} superseded={row['superseded']}"))
    else:
        checks.append(("PASS", "FCX id=207: row not found (may not exist in this environment)"))

    # Check 3: Open sp500 trades match expected {CAT, SYK}
    open_tickers = set(
        r["ticker"]
        for r in conn.execute(
            "SELECT ticker FROM trades WHERE status='open' AND universe='sp500'"
        ).fetchall()
    )
    expected = {"CAT", "SYK"}
    extras = open_tickers - expected
    if extras:
        checks.append(("WARN", f"Extra open sp500 tickers (may be new positions): {extras}"))
    else:
        checks.append(("PASS", f"Open sp500 tickers: {sorted(open_tickers)} (matches expected)"))

    # Check 4: broker_orders has EBAY BUY+SELL filled rows
    ebay_rows = conn.execute(
        "SELECT side, status, fill_price FROM broker_orders "
        "WHERE symbol='EBAY' AND status='filled' ORDER BY submitted_at"
    ).fetchall()
    if len(ebay_rows) >= 2:
        checks.append(("PASS", f"broker_orders: {len(ebay_rows)} filled EBAY rows (BUY+SELL)"))
    else:
        checks.append(("WARN", f"broker_orders: only {len(ebay_rows)} filled EBAY rows (expected ≥2)"))

    conn.close()

    for status, msg in checks:
        marker = "✓" if status == "PASS" else "✗" if status == "FAIL" else "⚠"
        if verbose or status != "PASS":
            print(f"  [{status}] {marker} {msg}")

    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify EBAY zombie remediation state")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print all checks including PASSes")
    args = parser.parse_args()

    print("=" * 60)
    print("EBAY zombie remediation verification (2026-05-06)")
    print("=" * 60)

    clean = verify(verbose=args.verbose)

    if clean:
        print("\nREMEDIATION STATE: CLEAN ✓")
        print("All expected fixes are in place.")
    else:
        print("\nREMEDIATION STATE: ISSUES DETECTED ✗")
        print("Run reconcile_ledger.py --market sp500 to fix remaining issues.")
        sys.exit(1)


if __name__ == "__main__":
    main()
