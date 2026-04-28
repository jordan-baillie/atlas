#!/usr/bin/env python3
"""
2026-04-29-chtr-forensic-correction-rca-1b.py
──────────────────────────────────────────────
RCA Phase 1B: CHTR ledger forensic correction using Alpaca FILL activities.

FINDINGS (confirmed against Alpaca /v2/account/activities/FILL):
  - Alpaca shows EXACTLY ONE round-trip for CHTR in 2026-04-20 to 2026-04-26:
    BUY  1 @ $243.9300  on 2026-04-21T13:30:01.718Z  order=7ee0a69c
    SELL 1 @ $241.8368  on 2026-04-23T17:28:19.011Z  order=50dc1ec0

  - Trade row 172 (momentum_breakout, entry=2026-04-21, exit=2026-04-23) matches
    Alpaca fill timestamps and prices EXACTLY. It is the LEGITIMATE record.

  - Trade row 184 (reconciled, entry=2026-04-24, exit=2026-04-25) has IDENTICAL
    prices but timestamps that correspond to NO Alpaca fill. It is a PHANTOM
    DUPLICATE injected by reconcile_entry_fills / reconcile_exit_fills re-running
    the same fill scan.

VERDICT: Case A — ONE real round-trip, row 184 is phantom.

ACTION:
  1. Mark row 184 superseded=1 (preserves audit trail, excluded from
     uq_trades_active_closed unique index guard)
  2. Append 'corrected_phase1b' to row 184's exit_reason for traceability
  3. Update row 172 exit_reason → 'reconcile_fill_verified_phase1b' to mark
     it as the ground-truth record
  4. No price/pnl change required — row 172 prices match Alpaca to 4 dp

DOLLAR CORRECTION: $0.00 (prices already correct in the canonical row)

Usage:
    python3 scripts/migrations/2026-04-29-chtr-forensic-correction-rca-1b.py
    python3 scripts/migrations/2026-04-29-chtr-forensic-correction-rca-1b.py --apply
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ATLAS_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ATLAS_ROOT))

from db.atlas_db import get_db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migration.chtr_rca_1b")

# ── Correction constants ──────────────────────────────────────────────────────

# The PHANTOM duplicate to suppress
PHANTOM_ROW_ID = 184

# The LEGITIMATE canonical row (timestamps match Alpaca fills)
CANONICAL_ROW_ID = 172

# Alpaca-confirmed actual fill prices (for documentation)
ALPACA_ENTRY_PRICE = 243.9300
ALPACA_EXIT_PRICE  = 241.8368
ALPACA_PNL         = round((ALPACA_EXIT_PRICE - ALPACA_ENTRY_PRICE) * 1, 4)  # -2.0932

# Alpaca fill order IDs
ALPACA_BUY_ORDER_ID  = "7ee0a69c-7989-4472-8048-8992c0c92203"
ALPACA_SELL_ORDER_ID = "50dc1ec0-550c-4e8e-83b7-84cf66a4ae3a"

# Idempotency sentinels
_PHANTOM_EXIT_REASON_SUFFIX = "corrected_phase1b"
_CANONICAL_EXIT_REASON = "reconcile_fill_verified_phase1b"


def _already_applied(conn) -> bool:
    """Return True if this migration was already applied (idempotent guard)."""
    row_phantom = conn.execute(
        "SELECT superseded, exit_reason FROM trades WHERE id = ?",
        (PHANTOM_ROW_ID,),
    ).fetchone()
    if row_phantom is None:
        logger.info("Row %d does not exist — migration is a no-op", PHANTOM_ROW_ID)
        return True
    if row_phantom[0] == 1 and _PHANTOM_EXIT_REASON_SUFFIX in (row_phantom[1] or ""):
        logger.info("Migration already applied (row %d superseded=1)", PHANTOM_ROW_ID)
        return True
    return False


def _capture_originals(conn) -> dict:
    """Read current values of both rows for reverse-migration documentation."""
    originals: dict = {}
    for row_id in (CANONICAL_ROW_ID, PHANTOM_ROW_ID):
        row = conn.execute(
            "SELECT id, ticker, strategy, entry_date, exit_date, entry_price, "
            "exit_price, shares, pnl, pnl_pct, exit_reason, superseded, status "
            "FROM trades WHERE id = ?",
            (row_id,),
        ).fetchone()
        if row:
            originals[row_id] = dict(zip(
                ["id", "ticker", "strategy", "entry_date", "exit_date",
                 "entry_price", "exit_price", "shares", "pnl", "pnl_pct",
                 "exit_reason", "superseded", "status"],
                row,
            ))
    return originals


def apply_migration(dry_run: bool = True) -> None:
    """Apply (or preview) the CHTR RCA-1B ledger correction."""
    with get_db() as conn:
        if _already_applied(conn):
            return

        originals = _capture_originals(conn)
        if not originals:
            logger.warning("Neither row %d nor %d found — skipping", CANONICAL_ROW_ID, PHANTOM_ROW_ID)
            return

        # ── Log originals for audit / reverse migration ────────────────────
        logger.info("ORIGINAL STATE:")
        for row_id, vals in originals.items():
            logger.info(
                "  id=%-4d %-20s entry=%-10s exit=%-10s pnl=%-8s superseded=%s exit_reason=%s",
                vals["id"], vals["strategy"], vals["entry_date"],
                vals["exit_date"], vals["pnl"], vals["superseded"], vals["exit_reason"],
            )

        # ── Correction 1: Mark phantom row superseded ──────────────────────
        phantom_orig = originals.get(PHANTOM_ROW_ID)
        if phantom_orig:
            new_exit_reason = (phantom_orig["exit_reason"] or "") + f"|{_PHANTOM_EXIT_REASON_SUFFIX}"
            logger.info(
                "%s: row %d (reconciled) → superseded=1, exit_reason=%s",
                "[DRY-RUN]" if dry_run else "[APPLY]",
                PHANTOM_ROW_ID,
                new_exit_reason,
            )
            if not dry_run:
                conn.execute(
                    "UPDATE trades SET superseded = 1, exit_reason = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (new_exit_reason, PHANTOM_ROW_ID),
                )

        # ── Correction 2: Mark canonical row verified ──────────────────────
        canonical_orig = originals.get(CANONICAL_ROW_ID)
        if canonical_orig:
            logger.info(
                "%s: row %d (momentum_breakout) → exit_reason=%s (prices already correct)",
                "[DRY-RUN]" if dry_run else "[APPLY]",
                CANONICAL_ROW_ID,
                _CANONICAL_EXIT_REASON,
            )
            if not dry_run:
                conn.execute(
                    "UPDATE trades SET exit_reason = ?, updated_at = datetime('now') "
                    "WHERE id = ?",
                    (_CANONICAL_EXIT_REASON, CANONICAL_ROW_ID),
                )

        if not dry_run:
            conn.commit()
            logger.info("✅ Migration applied successfully")
            logger.info(
                "   Alpaca-confirmed prices: entry=%s exit=%s pnl=%s",
                ALPACA_ENTRY_PRICE, ALPACA_EXIT_PRICE, ALPACA_PNL,
            )
            logger.info("   Dollar correction to ledger: $0.00 (prices were already correct)")
            logger.info("   Row 172 = canonical (1 real round-trip), row 184 = superseded phantom")
        else:
            logger.info("DRY-RUN complete — pass --apply to commit changes")

        # ── Verify state after apply ───────────────────────────────────────
        if not dry_run:
            final = conn.execute(
                "SELECT id, superseded, exit_reason FROM trades WHERE id IN (?,?)",
                (CANONICAL_ROW_ID, PHANTOM_ROW_ID),
            ).fetchall()
            logger.info("POST-MIGRATION STATE:")
            for r in final:
                logger.info("  id=%d  superseded=%d  exit_reason=%s", r[0], r[1], r[2])

        # ── Reverse migration recipe (printed to stdout for documentation) ─
        if not dry_run:
            print("\n── REVERSE MIGRATION RECIPE (if needed) ──────────────────────")
            for row_id, vals in originals.items():
                print(
                    f"UPDATE trades SET superseded={vals['superseded']}, "
                    f"exit_reason={repr(vals['exit_reason'])}, "
                    f"updated_at=datetime('now') WHERE id={row_id};"
                )
            print("─" * 60)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="CHTR RCA-1B forensic ledger correction (dry-run by default)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually apply the migration (default: dry-run only)",
    )
    args = parser.parse_args(argv)
    apply_migration(dry_run=not args.apply)


if __name__ == "__main__":
    main()
