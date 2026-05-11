"""Migration: 2026-05-11 — Dedup zombie trade rows + natural-key UNIQUE INDEX (#315).

Run:
    python3 scripts/migrations/2026-05-11-trades-natural-key-unique.py [--db data/atlas.db]

Order of operations
-------------------
1. Snapshot the DB before any changes.
2a. Delete all superseded=1 rows with exit_date (already stale-tagged).
2b. Dedup same-date natural key — keep oldest id per (ticker, DATE(exit_date), exit_price, shares).
2c. Delete cross-date reconciled zombies where an earlier non-reconciled row exists.
3.  Create the UNIQUE INDEX (idempotent via IF NOT EXISTS).

Idempotent: safe to re-run.  Steps 2a/2b/2c are no-ops when already clean.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "atlas.db"


def run(db_path: str = str(DEFAULT_DB)) -> dict:
    """Execute migration; return dict of step row counts."""
    db = Path(db_path)
    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")

    # ── 1. Snapshot ──────────────────────────────────────────────────────────
    ts = int(time.time())
    backup = db.parent / f"atlas.db.pre-315-{ts}"
    shutil.copy2(db, backup)
    print(f"[1] Backup created: {backup}")

    conn = sqlite3.connect(str(db), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # ── Pre-cleanup dup count ─────────────────────────────────────────────────
    pre_dup = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT ticker, DATE(exit_date), exit_price, shares
              FROM trades
             WHERE exit_date IS NOT NULL AND status = 'closed'
             GROUP BY 1, 2, 3, 4
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    print(f"[2] Pre-cleanup dup groups (natural key count > 1): {pre_dup}")

    # ── 2a. Delete superseded zombies ─────────────────────────────────────────
    cur = conn.execute(
        "DELETE FROM trades WHERE superseded = 1 AND exit_date IS NOT NULL"
    )
    step_a = cur.rowcount
    conn.commit()
    print(f"[3a] Deleted superseded=1 rows with exit_date: {step_a}")

    # ── 2b. Dedup same-date natural key — keep oldest id ─────────────────────
    cur = conn.execute(
        """
        DELETE FROM trades
         WHERE id IN (
             SELECT id FROM trades t1
              WHERE exit_date IS NOT NULL
                AND status = 'closed'
                AND id > (
                    SELECT MIN(id) FROM trades t2
                     WHERE t2.ticker          = t1.ticker
                       AND DATE(t2.exit_date) = DATE(t1.exit_date)
                       AND t2.exit_price      = t1.exit_price
                       AND t2.shares          = t1.shares
                       AND t2.status          = 'closed'
                       AND t2.exit_date IS NOT NULL
                )
         )
        """
    )
    step_b = cur.rowcount
    conn.commit()
    print(f"[3b] Deleted same-date natural-key dups (kept oldest id): {step_b}")

    # ── 2c. Dedup cross-date: reconciled rows superseded by earlier non-reconciled
    cur = conn.execute(
        """
        DELETE FROM trades
         WHERE strategy = 'reconciled'
           AND status   = 'closed'
           AND id IN (
               SELECT t1.id FROM trades t1
                WHERE t1.strategy = 'reconciled'
                  AND EXISTS (
                      SELECT 1 FROM trades t2
                       WHERE t2.ticker     = t1.ticker
                         AND t2.exit_price = t1.exit_price
                         AND t2.shares     = t1.shares
                         AND t2.strategy  != 'reconciled'
                         AND t2.id         < t1.id
                  )
           )
        """
    )
    step_c = cur.rowcount
    conn.commit()
    print(f"[3c] Deleted cross-date reconciled zombies: {step_c}")

    # ── Post-cleanup dup count ────────────────────────────────────────────────
    post_dup = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT ticker, DATE(exit_date), exit_price, shares
              FROM trades
             WHERE exit_date IS NOT NULL AND status = 'closed'
             GROUP BY 1, 2, 3, 4
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    print(f"[4] Post-cleanup dup groups remaining: {post_dup}")

    if post_dup > 0:
        remaining = conn.execute(
            """
            SELECT ticker, DATE(exit_date) AS exit_dt, exit_price, shares, COUNT(*) as cnt
              FROM trades
             WHERE exit_date IS NOT NULL AND status = 'closed'
             GROUP BY 1, 2, 3, 4
            HAVING COUNT(*) > 1
            ORDER BY cnt DESC
            LIMIT 20
            """
        ).fetchall()
        print("  Remaining dup groups:")
        for r in remaining:
            print(f"    {tuple(r)}")
        conn.close()
        raise RuntimeError(
            f"Post-cleanup still has {post_dup} dup group(s) — cannot create unique index safely"
        )

    # ── 3. Create partial unique index ────────────────────────────────────────
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_natural_key
          ON trades(ticker, DATE(exit_date), exit_price, shares)
         WHERE exit_date IS NOT NULL AND status = 'closed'
        """
    )
    conn.commit()
    print("[5] UNIQUE INDEX uq_trades_natural_key created (or already existed).")

    # ── Verify index ──────────────────────────────────────────────────────────
    idx = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE name='uq_trades_natural_key'"
    ).fetchone()
    print(f"[6] Index verified: {idx['name']!r}")
    print(f"      SQL: {idx['sql']}")

    # ── Remaining rows for key tickers ───────────────────────────────────────
    print("\n[7] Remaining closed rows for key tickers:")
    for ticker in ("SYK", "MCHP", "FSLR", "EBAY", "CRWD"):
        rows = conn.execute(
            "SELECT id, ticker, strategy, DATE(exit_date) AS exit_dt, "
            "exit_price, shares, ROUND(pnl,2) AS pnl, superseded "
            "FROM trades WHERE ticker=? AND status='closed' ORDER BY id",
            (ticker,),
        ).fetchall()
        print(f"  {ticker} — {len(rows)} closed row(s):")
        for r in rows:
            print(
                f"    id={r['id']} strategy={r['strategy']} "
                f"exit_dt={r['exit_dt']} exit_price={r['exit_price']} "
                f"shares={r['shares']} pnl={r['pnl']} superseded={r['superseded']}"
            )

    conn.close()
    print("\n[OK] Migration 2026-05-11-trades-natural-key-unique complete.")

    return {
        "backup": str(backup),
        "pre_dup_groups": pre_dup,
        "step_a_superseded_deleted": step_a,
        "step_b_same_date_dedup": step_b,
        "step_c_cross_date_reconciled": step_c,
        "post_dup_groups": post_dup,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to atlas.db")
    args = parser.parse_args()
    run(db_path=args.db)


if __name__ == "__main__":
    main()
