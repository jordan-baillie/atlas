#!/usr/bin/env python3
"""
Migration: backfill Good Friday 2026-04-03 into regime_history.

US markets were closed on 2026-04-03 (Good Friday / Easter weekend).
The regime model never ran, so no row exists for that date.  Both
adjacent rows (2026-04-02 and 2026-04-06) show transition_uncertain with
an identical composite score (+0.13), so a carry-forward from 2026-04-02
is unambiguous.

Also patches trade #127 (MRVL/momentum_breakout, entry 2026-04-03):
  - regime_at_entry  → 'transition_uncertain'
  - regime_at_exit   → already populated or left unchanged if present

Usage:
    python3 scripts/migrations/2026-04-28-backfill-good-friday-regime.py
        (dry-run; prints what it would do)
    python3 scripts/migrations/2026-04-28-backfill-good-friday-regime.py --apply
        (writes to DB; backs up atlas.db first)
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
ATLAS_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = ATLAS_ROOT / "data" / "atlas.db"
BACKUPS_DIR = ATLAS_ROOT / "data" / "backups"

TARGET_DATE = "2026-04-03"
SOURCE_DATE = "2026-04-02"
REGIME_VALUE = "transition_uncertain"
TRADE_ID = 127
CARRY_SUFFIX = " [carry-forward from 2026-04-02; market closed Good Friday]"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────

def _backup(db_path: Path) -> Path:
    """Copy atlas.db to data/backups/ with a UTC timestamp suffix."""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = BACKUPS_DIR / f"atlas.db.good-friday-backfill-{ts}"
    shutil.copy2(db_path, dest)
    logger.info("Backup written: %s", dest)
    return dest


def _get_source_row(conn: sqlite3.Connection) -> dict | None:
    """Return the 2026-04-02 regime_history row as a dict, or None."""
    row = conn.execute(
        "SELECT date, regime_state, trend_score, risk_score, active_universes, "
        "sizing_multiplier, enabled_strategies, reasoning, model_version "
        "FROM regime_history WHERE date = ?",
        (SOURCE_DATE,),
    ).fetchone()
    if row is None:
        return None
    keys = [
        "date", "regime_state", "trend_score", "risk_score", "active_universes",
        "sizing_multiplier", "enabled_strategies", "reasoning", "model_version",
    ]
    return dict(zip(keys, row))


def _target_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM regime_history WHERE date = ?", (TARGET_DATE,)
    ).fetchone()
    return row is not None


def _get_trade_127(conn: sqlite3.Connection) -> dict | None:
    """Return trade id=127 fields relevant to the patch."""
    row = conn.execute(
        "SELECT id, ticker, entry_date, regime_at_entry, regime_at_exit "
        "FROM trades WHERE id = ?",
        (TRADE_ID,),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(["id", "ticker", "entry_date", "regime_at_entry", "regime_at_exit"], row))


# ── Main logic ─────────────────────────────────────────────────────────────

def run(apply: bool) -> int:
    """Execute the migration.  Returns 0 on success, 1 on error."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        # ── Idempotency check ───────────────────────────────────────────────
        if _target_exists(conn):
            logger.info(
                "regime_history already has a row for %s — nothing to do (idempotent).",
                TARGET_DATE,
            )
            trade = _get_trade_127(conn)
            if trade:
                logger.info("Trade #127 current state: %s", trade)
            conn.close()
            return 0

        # ── Read source row ─────────────────────────────────────────────────
        source = _get_source_row(conn)
        if source is None:
            logger.error(
                "Source row %s not found in regime_history — cannot carry forward.",
                SOURCE_DATE,
            )
            conn.close()
            return 1

        logger.info("Source row (%s): %s", SOURCE_DATE, source)

        # ── Build the new row ───────────────────────────────────────────────
        new_reasoning = (source["reasoning"] or "") + CARRY_SUFFIX
        new_row = (
            TARGET_DATE,
            source["regime_state"],
            source["trend_score"],
            source["risk_score"],
            source["active_universes"],
            source["sizing_multiplier"],
            source["enabled_strategies"],
            new_reasoning,
            source["model_version"],
        )

        logger.info(
            "%s INSERT regime_history date=%s regime_state=%s reasoning=...%s",
            "[DRY-RUN]" if not apply else "[APPLY]",
            TARGET_DATE,
            source["regime_state"],
            CARRY_SUFFIX,
        )

        # ── Trade #127 patch ────────────────────────────────────────────────
        trade = _get_trade_127(conn)
        trade_patch_needed = False
        if trade is None:
            logger.info("Trade #%d not found — skipping trade patch.", TRADE_ID)
        else:
            logger.info("Trade #%d current state: %s", TRADE_ID, trade)
            if not trade["regime_at_entry"]:
                trade_patch_needed = True
                logger.info(
                    "%s UPDATE trades id=%d SET regime_at_entry='%s'",
                    "[DRY-RUN]" if not apply else "[APPLY]",
                    TRADE_ID,
                    REGIME_VALUE,
                )
            else:
                logger.info(
                    "Trade #%d regime_at_entry already set ('%s') — no patch needed.",
                    TRADE_ID,
                    trade["regime_at_entry"],
                )

        if not apply:
            logger.info("Dry-run complete — use --apply to commit changes.")
            conn.close()
            return 0

        # ── Take backup ─────────────────────────────────────────────────────
        conn.close()
        _backup(DB_PATH)
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")

        # ── Write regime_history row ────────────────────────────────────────
        conn.execute(
            "INSERT INTO regime_history "
            "(date, regime_state, trend_score, risk_score, active_universes, "
            " sizing_multiplier, enabled_strategies, reasoning, model_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            new_row,
        )
        conn.commit()
        logger.info("Inserted regime_history row for %s.", TARGET_DATE)

        # ── Patch trade #127 ────────────────────────────────────────────────
        if trade_patch_needed:
            conn.execute(
                "UPDATE trades SET regime_at_entry = ? WHERE id = ?",
                (REGIME_VALUE, TRADE_ID),
            )
            conn.commit()
            logger.info("Patched trade #%d regime_at_entry = '%s'.", TRADE_ID, REGIME_VALUE)

        # ── Final verification read ─────────────────────────────────────────
        rows = conn.execute(
            "SELECT date, regime_state FROM regime_history "
            "WHERE date BETWEEN '2026-04-01' AND '2026-04-10' ORDER BY date"
        ).fetchall()
        logger.info("regime_history 2026-04-01..10 AFTER migration:")
        for r in rows:
            logger.info("  %s  %s", r[0], r[1])

        if trade is not None:
            updated = conn.execute(
                "SELECT id, ticker, entry_date, regime_at_entry, regime_at_exit "
                "FROM trades WHERE id = ?",
                (TRADE_ID,),
            ).fetchone()
            logger.info("Trade #%d after patch: %s", TRADE_ID, updated)

        conn.close()
        return 0

    except Exception as exc:
        logger.error("Migration failed: %s", exc, exc_info=True)
        try:
            conn.close()
        except Exception:
            pass
        return 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually write to atlas.db (default: dry-run only)",
    )
    args = parser.parse_args(argv)
    sys.exit(run(apply=args.apply))


if __name__ == "__main__":
    main()
