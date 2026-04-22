#!/usr/bin/env python3
"""Fix trades where universe='sp500' but ticker belongs to a different universe.

Run:
  python3 scripts/migrations/2026-04-22-fix-trade-universe-mismatches.py --dry-run
  python3 scripts/migrations/2026-04-22-fix-trade-universe-mismatches.py --apply

CLI args:
  --dry-run   (default) Show proposed changes without writing.
  --apply     Execute UPDATEs inside a transaction, backup CSV first.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve()
_ATLAS_ROOT = _HERE.parent.parent.parent
sys.path.insert(0, str(_ATLAS_ROOT))

logger = logging.getLogger("fix_trade_universes")


# ── DB path resolution ─────────────────────────────────────────────────────────

def _resolve_db_path() -> Path:
    """Return active DB path — respects atlas_db._db_path_override for tests."""
    import os
    env_override = os.environ.get("ATLAS_DB_PATH")
    if env_override:
        return Path(env_override)
    try:
        from db import atlas_db
        override = getattr(atlas_db, "_db_path_override", None)
        if override:
            return Path(override)
    except Exception:
        pass
    return _ATLAS_ROOT / "data" / "atlas.db"


# ── CSV backup ─────────────────────────────────────────────────────────────────

def _backup_csv(db_path: Path, csv_path: Path) -> None:
    """Write all columns of trades table to CSV."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM trades ORDER BY id").fetchall()
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            if rows:
                writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                for row in rows:
                    writer.writerow(dict(row))
            else:
                f.write("no rows\n")
    finally:
        conn.close()
    logger.info("Backup written: %s", csv_path)


# ── Core migration logic ───────────────────────────────────────────────────────

def _find_mismatched_rows(conn: sqlite3.Connection) -> list[dict]:
    """Return all trade rows where universe does not match ticker membership."""
    from universe.membership import derive_universe
    from universe.definitions import UNIVERSES

    # Fetch all open and closed trades that have a non-NULL universe
    rows = conn.execute(
        "SELECT id, ticker, universe FROM trades WHERE universe IS NOT NULL ORDER BY id"
    ).fetchall()

    mismatches: list[dict] = []
    for row in rows:
        trade_id = row[0]
        ticker = row[1]
        current_universe = row[2]

        udef = UNIVERSES.get(current_universe)
        if not udef:
            # Unknown universe — skip (handled by other migrations)
            continue

        if udef.get("method") == "static":
            if ticker in udef.get("tickers", []):
                continue  # ticker IS in this universe → correct
        else:
            # dynamic (sp500) — check via builder
            try:
                from universe.builder import get_universe_tickers
                if ticker in set(get_universe_tickers(current_universe)):
                    continue  # ticker IS in dynamic universe
            except Exception:
                continue  # can't check dynamic universe → skip

        # Ticker is NOT in its stated universe — derive correct one
        correct_universe = derive_universe(ticker, None)  # no hint — let it decide
        if correct_universe is None:
            logger.warning(
                "id=%d %s: derive_universe returned None (unknown ticker) — skipping",
                trade_id, ticker,
            )
            continue

        mismatches.append({
            "id": trade_id,
            "ticker": ticker,
            "old_universe": current_universe,
            "new_universe": correct_universe,
        })

    return mismatches


def run_migration(
    db_path: Path,
    dry_run: bool,
    backup_csv_path: Path | None,
) -> int:
    """Fix trades with wrong universe. Returns exit code: 0 on success, 1 on error."""
    from universe.membership import clear_cache
    from universe.definitions import UNIVERSES
    clear_cache()  # ensure fresh membership table

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        mismatches = _find_mismatched_rows(conn)

        if not mismatches:
            logger.info("0 mismatches — nothing to do")
            return 0

        logger.info("Found %d mismatch(es):", len(mismatches))
        for m in mismatches:
            logger.info(
                "  id=%-5d %-6s  universe: %-16s → %-20s (source: universe.membership)",
                m["id"], m["ticker"], m["old_universe"], m["new_universe"],
            )

        if dry_run:
            logger.info("DRY RUN: would update %d row(s). Re-run with --apply to apply.", len(mismatches))
            return 0

        # ── Assertion: all resolved universes exist in UNIVERSES ──────────────
        for m in mismatches:
            if m["new_universe"] not in UNIVERSES:
                logger.error(
                    "SAFETY ABORT: resolved universe %r for id=%d ticker=%s is NOT in "
                    "universe.definitions.UNIVERSES — refusing to apply",
                    m["new_universe"], m["id"], m["ticker"],
                )
                return 1

        # ── Backup BEFORE any writes ──────────────────────────────────────────
        if backup_csv_path is None:
            ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H%M%S")
            backup_csv_path = (
                _ATLAS_ROOT / "data" / "backups" / f"trades_universe_fix_{ts}.csv"
            )
        _backup_csv(db_path, backup_csv_path)

        # ── Apply in a single transaction ─────────────────────────────────────
        conn.execute("BEGIN")
        try:
            for m in mismatches:
                conn.execute(
                    "UPDATE trades SET universe = ?, updated_at = datetime('now') WHERE id = ?",
                    (m["new_universe"], m["id"]),
                )
                logger.info("UPDATED id=%d: %s → %s", m["id"], m["old_universe"], m["new_universe"])

            conn.commit()
            logger.info("COMMITTED successfully")
        except Exception as exc:
            conn.execute("ROLLBACK")
            logger.error("ROLLED BACK due to exception: %s", exc, exc_info=True)
            return 1

        logger.info(
            "Updated %d rows, 0 unresolved, backup at %s",
            len(mismatches), backup_csv_path,
        )
        return 0

    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
        return 1
    finally:
        conn.close()


# ── CLI ────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Show proposed changes without writing (default).",
    )
    mode_group.add_argument(
        "--apply", action="store_true", default=False,
        help="Execute UPDATEs inside a transaction after backing up.",
    )
    parser.add_argument(
        "--backup-csv",
        type=Path,
        default=None,
        help="Path for CSV backup (default: data/backups/trades_universe_fix_{ts}.csv).",
    )
    args = parser.parse_args(argv)

    dry_run = not args.apply

    # Logging
    ts_str = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    log_path = _ATLAS_ROOT / "logs" / f"fix_trade_universes_{ts_str}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(str(log_path))
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    db_path = _resolve_db_path()
    logger.info("DB path: %s", db_path)
    logger.info("Mode: %s", "DRY RUN" if dry_run else "APPLY")

    rc = run_migration(db_path, dry_run=dry_run, backup_csv_path=args.backup_csv)
    logger.info("Exit code: %d", rc)
    sys.exit(rc)


if __name__ == "__main__":
    main()
