#!/usr/bin/env python3
"""Add regime_state column to research_experiments, then backfill from regime_history.

Run:
  python3 scripts/migrations/2026-04-23-add-regime-state.py --dry-run
  python3 scripts/migrations/2026-04-23-add-regime-state.py --apply

CLI args:
  --dry-run   (default) Show planned changes without writing.
  --apply     Execute ALTER TABLE + backfill UPDATE inside a transaction.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve()
_ATLAS_ROOT = _HERE.parent.parent.parent
sys.path.insert(0, str(_ATLAS_ROOT))

logger = logging.getLogger("add_regime_state")


# ── DB path resolution ─────────────────────────────────────────────────────────

def _resolve_db_path() -> Path:
    """Return active DB path — respects ATLAS_DB_PATH env var and atlas_db override."""
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


# ── Column existence check ─────────────────────────────────────────────────────

def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if column already exists in table."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


# ── Core migration logic ───────────────────────────────────────────────────────

def run_migration(db_path: Path, dry_run: bool) -> int:
    """Add regime_state column and backfill.  Returns 0 on success, 1 on error."""
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    try:
        # ── Pre-count ──────────────────────────────────────────────────────────
        total_rows = conn.execute(
            "SELECT COUNT(*) FROM research_experiments"
        ).fetchone()[0]
        logger.info("research_experiments total rows: %d", total_rows)

        # ── Check column existence ─────────────────────────────────────────────
        col_exists = _column_exists(conn, "research_experiments", "regime_state")
        if col_exists:
            logger.info("Column 'regime_state' already exists in research_experiments — skipping ALTER TABLE.")
            populated = conn.execute(
                "SELECT COUNT(*) FROM research_experiments WHERE regime_state IS NOT NULL"
            ).fetchone()[0]
            logger.info(
                "Current state: %d / %d rows have regime_state populated (%.1f%%)",
                populated, total_rows,
                (populated / total_rows * 100) if total_rows else 0.0,
            )
            return 0

        # ── Project backfill coverage ──────────────────────────────────────────
        projected = conn.execute("""
            SELECT COUNT(*)
            FROM research_experiments re
            WHERE EXISTS (
                SELECT 1 FROM regime_history rh
                WHERE rh.date = DATE(re.created_at)
            )
        """).fetchone()[0]

        logger.info(
            "Planned changes:\n"
            "  1. ALTER TABLE research_experiments ADD COLUMN regime_state TEXT DEFAULT NULL\n"
            "  2. Backfill UPDATE from regime_history on DATE(created_at) = regime_history.date\n"
            "  Projected: %d / %d rows will have regime_state populated after backfill (%.1f%%)\n"
            "  Remaining NULL: %d (dates outside regime_history coverage — expected)",
            projected, total_rows,
            (projected / total_rows * 100) if total_rows else 0.0,
            total_rows - projected,
        )

        if dry_run:
            logger.info("DRY RUN: no changes written. Re-run with --apply to execute.")
            return 0

        # ── Apply inside a transaction ─────────────────────────────────────────
        conn.execute("BEGIN")
        try:
            conn.execute(
                "ALTER TABLE research_experiments ADD COLUMN regime_state TEXT DEFAULT NULL"
            )
            logger.info("ALTER TABLE: added regime_state column.")

            conn.execute("""
                UPDATE research_experiments
                SET regime_state = (
                    SELECT regime_state FROM regime_history
                    WHERE date = DATE(research_experiments.created_at)
                )
                WHERE regime_state IS NULL
            """)
            logger.info("UPDATE: backfill complete.")

            conn.commit()
            logger.info("COMMITTED successfully.")
        except Exception as exc:
            conn.execute("ROLLBACK")
            logger.error("ROLLED BACK due to: %s", exc, exc_info=True)
            return 1

        # ── Post-count ─────────────────────────────────────────────────────────
        populated_after = conn.execute(
            "SELECT COUNT(*) FROM research_experiments WHERE regime_state IS NOT NULL"
        ).fetchone()[0]
        logger.info(
            "Post-migration: %d / %d rows have regime_state populated (%.1f%%). "
            "%d rows remain NULL (dates outside regime_history — expected).",
            populated_after, total_rows,
            (populated_after / total_rows * 100) if total_rows else 0.0,
            total_rows - populated_after,
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
        help="Show planned changes without writing (default).",
    )
    mode_group.add_argument(
        "--apply", action="store_true", default=False,
        help="Execute ALTER TABLE + backfill inside a transaction.",
    )
    args = parser.parse_args(argv)

    dry_run = not args.apply

    ts_str = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    log_path = _ATLAS_ROOT / "logs" / f"add_regime_state_{ts_str}.log"
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

    rc = run_migration(db_path, dry_run=dry_run)
    logger.info("Exit code: %d", rc)
    sys.exit(rc)


if __name__ == "__main__":
    main()
