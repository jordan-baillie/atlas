"""Migration: Add market_id column to portfolio_snapshots.

- Adds market_id TEXT DEFAULT 'sp500' (idempotent)
- Backfills existing rows using position ticker heuristic
- Adds composite index on (market_id, timestamp)

Run: python3 scripts/migrations/2026-04-24-add-market-id-to-portfolio-snapshots.py
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

PROJECT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT / "data" / "atlas.db"

# Tickers that are strong indicators of commodity_etfs universe
# (never appear exclusively in sp500 plans)
_COMMODITY_MARKERS: frozenset[str] = frozenset({
    "GLD", "GLDM", "IAU",        # gold ETFs
    "SLV", "PSLV",               # silver ETFs
    "UNG", "BOIL", "KOLD",       # natural gas ETFs
    "USO", "UCO", "SCO",         # oil ETFs
    "DBA", "DBB", "DBC",         # commodity basket ETFs
    "GDX", "GDXJ",               # gold miners ETFs
    "XLE", "XLB", "PDBC",        # energy/materials ETFs
    "CCJ", "NLR",                # uranium
})


def _infer_market_id(positions_json: str | None) -> str:
    """Infer market_id from the position tickers JSON blob."""
    if not positions_json:
        return "sp500"
    try:
        positions = json.loads(positions_json)
        tickers = {p.get("ticker", "") for p in positions if isinstance(p, dict)}
        if tickers & _COMMODITY_MARKERS:
            return "commodity_etfs"
    except (json.JSONDecodeError, TypeError):
        pass
    return "sp500"


def run(db_path: Path = DB_PATH) -> None:
    """Execute the migration."""
    if not db_path.exists():
        log.error("Database not found: %s", db_path)
        sys.exit(1)

    # ── 1. Backup ────────────────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.parent / f"atlas_backup_{ts}_before_market_id_migration.db"
    shutil.copy2(db_path, backup_path)
    log.info("Backup written: %s", backup_path)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row

    try:
        # ── 2. Idempotent column addition ────────────────────────────────────
        existing_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(portfolio_snapshots)").fetchall()
        }
        if "market_id" in existing_cols:
            log.info("Column market_id already exists — skipping ALTER TABLE")
        else:
            conn.execute(
                "ALTER TABLE portfolio_snapshots ADD COLUMN market_id TEXT DEFAULT 'sp500'"
            )
            conn.commit()
            log.info("Added column market_id TEXT DEFAULT 'sp500'")

        # ── 3. Backfill existing rows ────────────────────────────────────────
        rows = conn.execute(
            "SELECT id, positions, market_id FROM portfolio_snapshots"
        ).fetchall()

        updates: list[tuple[str, int]] = []
        for row in rows:
            row_id = row["id"]
            # Skip rows that were already explicitly set (non-default) by a
            # previous partial run — only backfill rows still at 'sp500' default.
            current_mid = row["market_id"] or "sp500"
            if current_mid not in ("sp500",):
                # Already assigned a non-sp500 value — leave it
                continue
            inferred = _infer_market_id(row["positions"])
            if inferred != current_mid:
                updates.append((inferred, row_id))

        if updates:
            conn.executemany(
                "UPDATE portfolio_snapshots SET market_id=? WHERE id=?",
                updates,
            )
            conn.commit()
            log.info(
                "Backfilled %d rows → commodity_etfs (remainder stay sp500)", len(updates)
            )
        else:
            log.info("No rows needed backfilling")

        # ── 4. Composite index ───────────────────────────────────────────────
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_market_ts
            ON portfolio_snapshots(market_id, timestamp)
            """
        )
        conn.commit()
        log.info("Index idx_portfolio_snapshots_market_ts ensured")

        # ── 5. Summary ───────────────────────────────────────────────────────
        summary = conn.execute(
            """
            SELECT market_id, COUNT(*) AS n, MAX(timestamp) AS latest
            FROM portfolio_snapshots
            GROUP BY market_id
            ORDER BY market_id
            """
        ).fetchall()
        log.info("Result breakdown:")
        for s in summary:
            log.info("  market_id=%-15s rows=%-4d latest=%s", s["market_id"], s["n"], s["latest"])

    finally:
        conn.close()

    log.info("Migration complete.")


if __name__ == "__main__":
    run()
