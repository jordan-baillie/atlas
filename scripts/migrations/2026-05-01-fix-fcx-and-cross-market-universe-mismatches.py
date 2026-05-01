#!/usr/bin/env python3
"""Migration: fix trades.universe rows where universe disagrees with derive_universe(ticker).

Background
----------
backfill_orphan_trades.py used the broker-state-file's market_id as the
trades.universe value. When a position lived in the wrong state file (e.g.
FCX in live_sp500.json), the universe column was poisoned. Even after the
state file was corrected, the historical trade rows persisted with the
wrong universe.

This migration scans ALL trades, computes canonical_universe via
universe.membership.derive_universe(ticker), and UPDATES rows where they
disagree.  Idempotent — re-running is a no-op.

Usage:
    python3 scripts/migrations/2026-05-01-fix-fcx-and-cross-market-universe-mismatches.py --dry-run
    python3 scripts/migrations/2026-05-01-fix-fcx-and-cross-market-universe-mismatches.py --apply
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT))

from db import atlas_db
from universe.membership import derive_universe

logger = logging.getLogger(__name__)


def run(dry_run: bool) -> int:
    """Audit + fix trades.universe mismatches. Returns 0 on success."""
    with atlas_db.get_db() as db:
        rows = db.execute(
            "SELECT id, ticker, universe, status FROM trades ORDER BY id"
        ).fetchall()

    fixes: list[dict] = []
    unresolvable: list[dict] = []

    for row in rows:
        ticker = row["ticker"]
        current = row["universe"] or ""
        # Use hint=None so static ETF universe membership takes priority over
        # dynamic sp500 membership.  derive_universe(ticker, hint="sp500") would
        # return "sp500" for FCX because FCX is an sp500 constituent — that's the
        # bug we're fixing.  Without a hint, derive_universe returns the
        # alphabetically-first membership (static ETF universes sort before "sp500").
        canonical = derive_universe(ticker)

        if canonical is None:
            unresolvable.append({"id": row["id"], "ticker": ticker, "current": current})
            continue
        if canonical != current:
            fixes.append({
                "id": row["id"],
                "ticker": ticker,
                "old_universe": current,
                "new_universe": canonical,
                "status": row["status"],
            })

    print(f"Scanned {len(rows)} trade rows.")
    print(f"  Mismatches found: {len(fixes)}")
    print(f"  Unresolvable (no membership): {len(unresolvable)}")

    if fixes:
        print("\nMismatches:")
        for f in fixes:
            print(f"  id={f['id']:4d}  {f['ticker']:6s}  '{f['old_universe']}' → '{f['new_universe']}'  ({f['status']})")

    if unresolvable:
        print("\nUnresolvable (left untouched):")
        for u in unresolvable:
            print(f"  id={u['id']:4d}  {u['ticker']:6s}  current='{u['current']}'")

    if dry_run:
        print("\nDRY RUN — no changes applied.")
        return 0

    if not fixes:
        print("No fixes needed — clean state.")
        return 0

    with atlas_db.get_db() as db:
        for f in fixes:
            db.execute(
                "UPDATE trades SET universe=?, updated_at=datetime('now') WHERE id=?",
                (f["new_universe"], f["id"]),
            )
        db.commit()

    print(f"\nAPPLIED: {len(fixes)} rows updated.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview without changing")
    parser.add_argument("--apply", action="store_true", help="Apply changes")
    args = parser.parse_args()
    if not (args.dry_run or args.apply):
        parser.error("must specify --dry-run or --apply")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
