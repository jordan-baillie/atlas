#!/usr/bin/env python3
"""
Migration: 2026-04-28-trade-dedup-superseded

What it does (in order inside a single transaction):
1. Add   `superseded INTEGER NOT NULL DEFAULT 0 CHECK (superseded IN (0,1))`
         column to `trades` (idempotent — skipped if already present).
2. Convert any rows with status='superseded' → status='closed', superseded=1
         (normalises the old text-status hack).
3. Identify dup clusters: (ticker, strategy, DATE(exit_date), ROUND(pnl,2))
         for status='closed' AND superseded=0 rows; keep lowest id, mark
         the rest superseded=1.
4. DROP   the now-redundant idx_trades_no_dup_closed index.
5. CREATE the new partial UNIQUE index uq_trades_active_closed keyed on
         (ticker, strategy, DATE(exit_date), ROUND(pnl,2))
         WHERE status='closed' AND superseded=0.
6. (Re-)create the trades_active convenience view.

Idempotent: safe to run twice.
Default = dry-run.  Pass --apply to commit changes.

Usage:
    python3 scripts/migrations/2026-04-28-trade-dedup-superseded.py
    python3 scripts/migrations/2026-04-28-trade-dedup-superseded.py --apply
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "atlas.db"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────────────────────

def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


def _has_index(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    return row is not None


def _has_view(conn: sqlite3.Connection, view_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name=?",
        (view_name,),
    ).fetchone()
    return row is not None


def _find_old_superseded(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Rows currently with status='superseded' (old-style)."""
    rows = conn.execute(
        "SELECT id, ticker, strategy, pnl FROM trades WHERE status='superseded'"
    ).fetchall()
    return [dict(r) for r in rows]


def _find_dup_clusters(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """
    Among status='closed' rows (excluding already-superseded in new-style),
    find groups sharing (ticker, strategy, DATE(exit_date), ROUND(pnl,2)).
    Returns list of {'canonical_id', 'superseded_ids', 'ticker', 'strategy',
                      'exit_day', 'pnl_r'}.
    """
    # When 'superseded' column exists, filter on it; otherwise use all closed rows.
    has_sup = _has_column(conn, "trades", "superseded")
    if has_sup:
        rows = conn.execute(
            """
            SELECT id, ticker, strategy,
                   DATE(exit_date) AS exit_day,
                   ROUND(pnl, 2)   AS pnl_r
            FROM trades
            WHERE status='closed' AND superseded=0
            ORDER BY id ASC
            """
        ).fetchall()
    else:
        # Pre-migration: treat both closed and superseded as the pool
        rows = conn.execute(
            """
            SELECT id, ticker, strategy,
                   DATE(exit_date) AS exit_day,
                   ROUND(pnl, 2)   AS pnl_r
            FROM trades
            WHERE status IN ('closed', 'superseded')
            ORDER BY id ASC
            """
        ).fetchall()

    groups: dict[tuple, list[int]] = defaultdict(list)
    meta: dict[tuple, dict] = {}
    for r in rows:
        key = (r["ticker"], r["strategy"], r["exit_day"], r["pnl_r"])
        groups[key].append(r["id"])
        if key not in meta:
            meta[key] = {
                "ticker": r["ticker"],
                "strategy": r["strategy"],
                "exit_day": r["exit_day"],
                "pnl_r": r["pnl_r"],
            }

    result = []
    for key, ids in groups.items():
        if len(ids) < 2:
            continue
        canonical_id = min(ids)
        result.append({
            "canonical_id": canonical_id,
            "superseded_ids": [i for i in ids if i != canonical_id],
            **meta[key],
        })
    return result


# ── dry-run analysis ──────────────────────────────────────────────────────────

def analyse(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return analysis without modifying anything."""
    old_sup_rows = _find_old_superseded(conn)
    clusters = _find_dup_clusters(conn)

    all_new_sup_ids: list[int] = []
    for c in clusters:
        all_new_sup_ids.extend(c["superseded_ids"])

    has_col = _has_column(conn, "trades", "superseded")
    has_old_idx = _has_index(conn, "idx_trades_no_dup_closed")
    has_new_idx = _has_index(conn, "uq_trades_active_closed")

    return {
        "has_superseded_col": has_col,
        "has_old_idx": has_old_idx,
        "has_new_idx": has_new_idx,
        "old_style_superseded_rows": old_sup_rows,
        "dup_clusters": clusters,
        "new_superseded_ids": all_new_sup_ids,
        "total_clusters": len(clusters),
        "total_new_sup_rows": len(all_new_sup_ids),
    }


def print_analysis(info: dict[str, Any]) -> None:
    print()
    print("=" * 65)
    print("  DRY-RUN: 2026-04-28-trade-dedup-superseded migration")
    print("=" * 65)
    print()
    print(f"  superseded column present:    {info['has_superseded_col']}")
    print(f"  old idx_trades_no_dup_closed: {info['has_old_idx']}")
    print(f"  new uq_trades_active_closed:  {info['has_new_idx']}")
    print()
    print(f"  status='superseded' rows to convert: {len(info['old_style_superseded_rows'])}")
    for r in info["old_style_superseded_rows"]:
        print(f"    id={r['id']:4d}  {r['ticker']}/{r['strategy']}  pnl={r['pnl']}")
    print()
    print(f"  Dup clusters (new superseded needed): {info['total_clusters']}")
    for c in info["dup_clusters"]:
        print(
            f"    {c['ticker']}/{c['strategy']}"
            f"  exit={c['exit_day']}  pnl={c['pnl_r']}"
            f"  → canonical={c['canonical_id']}"
            f"  supersede={c['superseded_ids']}"
        )
    print()
    print(f"  Rows to mark superseded=1 (net new): {info['total_new_sup_rows']}")
    old_sup_count = len(info["old_style_superseded_rows"])
    total = old_sup_count + info["total_new_sup_rows"]
    print(f"  Total superseded rows after migration: {total}")
    print()
    print("  Run with --apply to commit.")
    print("=" * 65)
    print()


# ── apply ─────────────────────────────────────────────────────────────────────

def apply_migration(conn: sqlite3.Connection) -> None:
    """Apply all changes inside a single transaction."""
    info = analyse(conn)

    conn.execute("BEGIN")
    try:
        changed_rows = 0
        changed_clusters = 0

        # ── Step 1: Add superseded column ──────────────────────────────────
        if not info["has_superseded_col"]:
            log.info("Step 1: Adding superseded column …")
            conn.execute(
                "ALTER TABLE trades ADD COLUMN "
                "superseded INTEGER NOT NULL DEFAULT 0 "
                "CHECK (superseded IN (0,1))"
            )
            log.info("  Column added.")
        else:
            log.info("Step 1: superseded column already present — skipped.")

        # ── Step 2: DROP old index FIRST (required before status conversions) ─
        # The old idx_trades_no_dup_closed fires on status='closed' rows even
        # during the superseded→closed conversion; drop it before touching rows.
        if info["has_old_idx"]:
            log.info("Step 2: Dropping idx_trades_no_dup_closed (before row changes) …")
            conn.execute("DROP INDEX IF EXISTS idx_trades_no_dup_closed")
        else:
            log.info("Step 2: idx_trades_no_dup_closed not present — skipped.")

        # ── Step 3: Convert old status='superseded' → closed + superseded=1 ─
        old_sup = info["old_style_superseded_rows"]
        if old_sup:
            log.info(
                "Step 3: Converting %d status='superseded' rows …", len(old_sup)
            )
            ph = ",".join("?" * len(old_sup))
            ids = [r["id"] for r in old_sup]
            conn.execute(
                f"UPDATE trades SET status='closed', superseded=1, "
                f"updated_at=datetime('now') WHERE id IN ({ph})",
                ids,
            )
            changed_rows += len(old_sup)
            log.info("  Converted %d rows.", len(old_sup))
        else:
            log.info("Step 3: No old-style superseded rows — skipped.")

        # ── Step 4: Dup detection ────────────────────────────────────────────
        # Re-run after step 3 so the pool is correct.
        clusters = _find_dup_clusters(conn)
        all_new_ids: list[int] = []
        for c in clusters:
            all_new_ids.extend(c["superseded_ids"])

        if all_new_ids:
            log.info(
                "Step 4: Marking %d rows superseded=1 across %d clusters …",
                len(all_new_ids), len(clusters),
            )
            for c in clusters:
                sup_ids = c["superseded_ids"]
                ph2 = ",".join("?" * len(sup_ids))
                conn.execute(
                    f"UPDATE trades SET superseded=1, "
                    f"updated_at=datetime('now') WHERE id IN ({ph2})",
                    sup_ids,
                )
                changed_rows += len(sup_ids)
                changed_clusters += 1
                log.info(
                    "  %s/%s exit=%s pnl=%s  → keep %d, supersede %s",
                    c["ticker"], c["strategy"], c["exit_day"], c["pnl_r"],
                    c["canonical_id"], c["superseded_ids"],
                )
        else:
            log.info("Step 4: No new dup clusters — skipped.")

        # ── Step 5: CREATE new unique index ─────────────────────────────────
        if not info["has_new_idx"] and not _has_index(conn, "uq_trades_active_closed"):
            log.info("Step 5: Creating uq_trades_active_closed …")
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_active_closed
                ON trades(ticker, strategy, DATE(exit_date), ROUND(pnl, 2))
                WHERE status='closed' AND superseded=0
                """
            )
            log.info("  Index created.")
        else:
            log.info("Step 5: uq_trades_active_closed already present — skipped.")

        # ── Step 6: trades_active view ───────────────────────────────────────
        log.info("Step 6: Recreating trades_active view …")
        conn.execute("DROP VIEW IF EXISTS trades_active")
        conn.execute(
            "CREATE VIEW trades_active AS SELECT * FROM trades WHERE superseded=0"
        )

        conn.execute("COMMIT")

    except Exception:
        conn.execute("ROLLBACK")
        log.error("Migration FAILED — rolled back.")
        raise

    # ── Summary ───────────────────────────────────────────────────────────────
    old_sup_count = len(info["old_style_superseded_rows"])
    total_sup = old_sup_count + len(all_new_ids) if all_new_ids else old_sup_count
    print()
    print("=" * 65)
    print("  APPLY: migration complete")
    print("=" * 65)
    print(f"  Converted old-style superseded rows:  {old_sup_count}")
    print(f"  New superseded rows (dup detection):  {len(all_new_ids) if all_new_ids else 0}")
    print(f"  Dup clusters processed:               {changed_clusters}")
    print(f"  Total rows now superseded=1:          {total_sup}")
    print(
        f"\n  Marked {old_sup_count + (len(all_new_ids) if all_new_ids else 0)} "
        f"rows as superseded across "
        f"{changed_clusters + (1 if old_sup_count else 0)} clusters."
    )
    print("=" * 65)
    print()


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Trade dedup superseded migration (2026-04-28)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit changes to the database (default: dry-run)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help=f"Path to atlas.db (default: {DB_PATH})",
    )
    args = parser.parse_args(argv)

    conn = _connect(args.db)
    try:
        if args.apply:
            apply_migration(conn)
        else:
            info = analyse(conn)
            print_analysis(info)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
