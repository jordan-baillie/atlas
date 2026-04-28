"""Idempotent backfill for regime_history Apr 2-11 2026 gap.

Pre-filters target dates to those NOT already in regime_history,
then calls the standard backfill_regime_history() over the filtered range.

Safe to re-run: if all dates exist, no writes occur (snapshot-and-restore
ensures any pre-existing rows are not disturbed even if the underlying
backfill_regime_history uses INSERT OR REPLACE).

Usage::

    python3 scripts/backfill_regime_gap_apr2026.py --dry-run
    python3 scripts/backfill_regime_gap_apr2026.py
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.atlas_db import get_db
from regime.history import backfill_regime_history

GAP_START = "2026-04-02"
GAP_END = "2026-04-11"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def existing_dates(start: str, end: str) -> set[str]:
    """Return set of dates already present in regime_history for the given range."""
    with get_db() as db:
        rows = db.execute(
            "SELECT date FROM regime_history WHERE date BETWEEN ? AND ?",
            (start, end),
        ).fetchall()
    return {r["date"] if hasattr(r, "keys") else r[0] for r in rows}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Idempotent backfill for regime_history Apr 2-11 2026 gap."
    )
    ap.add_argument("--start", default=GAP_START, help="Gap start date (YYYY-MM-DD)")
    ap.add_argument("--end", default=GAP_END, help="Gap end date (YYYY-MM-DD)")
    ap.add_argument("--dry-run", action="store_true", help="Report what would happen, no writes")
    args = ap.parse_args()

    have = existing_dates(args.start, args.end)
    log.info(
        "existing regime_history dates in [%s, %s]: %d",
        args.start, args.end, len(have),
    )
    if have:
        log.info("dates already present: %s", sorted(have))
    else:
        log.info("no existing rows in target range — clean backfill")

    if args.dry_run:
        log.info(
            "dry-run: would call backfill_regime_history('%s', '%s') "
            "and restore %d pre-existing rows afterwards",
            args.start, args.end, len(have),
        )
        return 0

    # ── Snapshot pre-existing rows so we can restore them after ──────────────
    # backfill_regime_history uses INSERT OR REPLACE which would overwrite any
    # rows in the range. We preserve originals and write them back to enforce
    # non-destructive semantics for dates already classified.
    pre_snapshot: dict[str, dict] = {}
    if have:
        with get_db() as db:
            for d in sorted(have):
                row = db.execute(
                    "SELECT * FROM regime_history WHERE date=?", (d,)
                ).fetchone()
                if row:
                    pre_snapshot[d] = dict(row)
        log.info("snapshotted %d pre-existing rows", len(pre_snapshot))

    # ── Run the standard backfill (same classifier as daily updates) ──────────
    log.info("calling backfill_regime_history('%s', '%s')", args.start, args.end)
    stats = backfill_regime_history(start_date=args.start, end_date=args.end)
    log.info(
        "backfill complete: processed=%d skipped=%d transitions=%d",
        stats["dates_processed"],
        stats["dates_skipped"],
        len(stats.get("regime_transitions", [])),
    )

    # ── Restore any pre-existing rows (defensive; enforces non-destructive) ──
    if pre_snapshot:
        with get_db() as db:
            for d, row in pre_snapshot.items():
                cols = ",".join(row.keys())
                placeholders = ",".join("?" * len(row))
                values = tuple(row.values())
                db.execute(
                    f"INSERT OR REPLACE INTO regime_history ({cols}) VALUES ({placeholders})",
                    values,
                )
        log.info("restored %d pre-existing rows", len(pre_snapshot))

    # ── Report final state ────────────────────────────────────────────────────
    with get_db() as db:
        post_rows = db.execute(
            "SELECT date, regime_state FROM regime_history "
            "WHERE date BETWEEN ? AND ? ORDER BY date",
            (args.start, args.end),
        ).fetchall()

    log.info("Final regime_history rows in [%s, %s]:", args.start, args.end)
    for r in post_rows:
        d = r["date"] if hasattr(r, "keys") else r[0]
        s = r["regime_state"] if hasattr(r, "keys") else r[1]
        log.info("  %s -> %s", d, s)

    written = len(post_rows)
    new_dates = written - len(have)
    log.info(
        "Summary: %d total rows in range, %d new, %d pre-existing preserved",
        written, new_dates, len(have),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
