#!/usr/bin/env python3
"""
Atlas: Reconcile SQLite trades table to match broker state files.

Reads all brokers/state/live_*.json files, unions their open positions
(deduplicating by ticker — prefers non-'unknown' strategy), then ensures
SQLite's trades table has a matching open row for every broker position.

Actions taken:
  - If a broker position has NO SQLite row at all   → INSERT as open
  - If a broker position has a CLOSED SQLite row    → reopen it (clear exit fields)
  - If a broker position is already open in SQLite  → no-op

Usage:
    python3 scripts/reconcile_sqlite_to_broker.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Project root ──────────────────────────────────────────────────────────────
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
os.chdir(PROJECT)

from db import atlas_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

BROKER_STATE_DIR = PROJECT / "brokers" / "state"


def _universe_from_filename(fname: str) -> str:
    """Extract universe name from live_*.json filename.

    e.g. "live_sp500.json" → "sp500"
    """
    stem = Path(fname).stem
    return stem[5:] if stem.startswith("live_") else stem


def _load_broker_positions() -> list[dict[str, Any]]:
    """
    Load and union positions from all live_*.json broker state files.

    Deduplicates by ticker — prefers entry with non-'unknown' strategy.
    Attaches '_universe' key derived from filename.
    """
    all_positions: list[dict[str, Any]] = []

    state_files = sorted(BROKER_STATE_DIR.glob("live_*.json"))
    if not state_files:
        logger.error("No live_*.json files found in %s", BROKER_STATE_DIR)
        return []

    for sf in state_files:
        universe = _universe_from_filename(sf.name)
        try:
            with open(sf) as f:
                data = json.load(f)
        except Exception as exc:
            logger.warning("Could not load %s: %s", sf.name, exc)
            continue

        positions_in_file = data.get("positions", [])
        logger.info("Loaded %d positions from %s", len(positions_in_file), sf.name)
        for pos in positions_in_file:
            pos = dict(pos)
            pos["_source_file"] = sf.name
            pos["_universe"] = universe
            all_positions.append(pos)

    # Deduplicate by ticker — prefer non-'unknown' strategy
    seen: dict[str, dict[str, Any]] = {}
    for pos in all_positions:
        ticker = pos["ticker"]
        if ticker not in seen:
            seen[ticker] = pos
        else:
            existing_strategy = seen[ticker].get("strategy", "unknown")
            this_strategy = pos.get("strategy", "unknown")
            if existing_strategy == "unknown" and this_strategy != "unknown":
                logger.debug(
                    "Preferring %s/%s over %s/%s",
                    ticker, this_strategy, ticker, existing_strategy,
                )
                seen[ticker] = pos

    return list(seen.values())


def _best_sqlite_row(conn: Any, ticker: str, strategy: str) -> dict[str, Any] | None:
    """
    Return the most recent SQLite trades row for (ticker, strategy).

    If strategy is 'unknown', match by ticker only.
    Open rows are preferred; within the same status, latest entry_date wins.
    """
    if strategy == "unknown":
        row = conn.execute(
            """SELECT * FROM trades
               WHERE ticker = ?
               ORDER BY (status='open') DESC, entry_date DESC
               LIMIT 1""",
            (ticker,),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT * FROM trades
               WHERE ticker = ? AND strategy = ?
               ORDER BY (status='open') DESC, entry_date DESC
               LIMIT 1""",
            (ticker, strategy),
        ).fetchone()
    return dict(row) if row else None


def _is_open_in_sqlite(conn: Any, ticker: str, strategy: str) -> bool:
    """Return True if SQLite already has an open row for this (ticker, strategy)."""
    if strategy == "unknown":
        row = conn.execute(
            "SELECT id FROM trades WHERE ticker=? AND status='open' LIMIT 1",
            (ticker,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM trades "
            "WHERE ticker=? AND strategy=? AND status='open' LIMIT 1",
            (ticker, strategy),
        ).fetchone()
    return row is not None


def reconcile(*, dry_run: bool) -> int:
    """
    Main reconciliation logic.

    Returns the number of changes made (or that would be made in dry-run).
    """
    mode_tag = "[DRY-RUN] " if dry_run else ""
    changes = 0

    broker_positions = _load_broker_positions()
    if not broker_positions:
        logger.error("No broker positions loaded — aborting.")
        return 0

    logger.info("Broker union: %d unique positions (deduplicated by ticker)", len(broker_positions))

    now_utc = datetime.now(timezone.utc).isoformat()

    with atlas_db.get_db() as conn:
        for pos in broker_positions:
            ticker   = pos["ticker"]
            strategy = pos.get("strategy", "unknown")
            universe = pos.get("_universe", "sp500")

            # ── Already open in SQLite → no-op ─────────────────────────────
            if _is_open_in_sqlite(conn, ticker, strategy):
                logger.debug("  %s/%s: already open in SQLite — skip", ticker, strategy)
                continue

            # ── Look for any existing row (open or closed) ──────────────────
            existing = _best_sqlite_row(conn, ticker, strategy)

            if existing is not None and existing["status"] == "closed":
                # Reopen the most recent closed row
                row_id = existing["id"]
                print(
                    f"{mode_tag}REOPEN  {ticker}/{strategy}  "
                    f"id={row_id}  was closed on {existing.get('exit_date', '?')}"
                )
                changes += 1
                if not dry_run:
                    conn.execute(
                        """UPDATE trades
                           SET status='open',
                               exit_date=NULL,
                               exit_price=NULL,
                               exit_reason=NULL,
                               pnl=NULL,
                               pnl_pct=NULL,
                               hold_days=NULL,
                               updated_at=?
                           WHERE id=?""",
                        (now_utc, row_id),
                    )
                    conn.commit()
                    logger.info("Reopened id=%d  %s/%s", row_id, ticker, strategy)

            else:
                # No SQLite row at all — insert as new open trade
                entry_date  = pos.get("entry_date", now_utc[:10])
                entry_price = float(pos.get("entry_price", 0.0))
                shares      = int(pos.get("shares", 0))
                stop_price  = pos.get("stop_price")
                take_profit = pos.get("take_profit")
                stop_order  = pos.get("stop_order_id", "")
                tp_order    = pos.get("tp_order_id", "")

                print(
                    f"{mode_tag}INSERT   {ticker}/{strategy}/{universe}  "
                    f"entry={entry_date}@{entry_price:.4f}  shares={shares}  "
                    f"stop={stop_price}"
                )
                changes += 1
                if not dry_run:
                    conn.execute(
                        """INSERT INTO trades
                           (ticker, strategy, universe, direction,
                            entry_date, entry_price, shares,
                            stop_price, take_profit,
                            status, stop_order_id, tp_order_id,
                            created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,'open',?,?,?,?)""",
                        (
                            ticker, strategy, universe, "long",
                            entry_date, entry_price, shares,
                            stop_price, take_profit,
                            stop_order or "", tp_order or "",
                            now_utc, now_utc,
                        ),
                    )
                    conn.commit()
                    logger.info(
                        "Inserted %s/%s/%s  entry=%s@%.4f  shares=%d",
                        ticker, strategy, universe, entry_date, entry_price, shares,
                    )

    return changes


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile SQLite trades table to match broker state files."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned changes without writing to SQLite.",
    )
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print("  Atlas Broker → SQLite Trade Reconciliation")
    if args.dry_run:
        print("  MODE: DRY-RUN  (no writes)")
    else:
        print("  MODE: LIVE     (writes to SQLite)")
    print(f"{'='*60}\n")

    changes = reconcile(dry_run=args.dry_run)

    print(f"\n{'='*60}")
    if args.dry_run:
        print(f"  DRY-RUN complete — {changes} change(s) would be made")
    else:
        print(f"  Reconciliation complete — {changes} change(s) applied")
    print(f"{'='*60}\n")

    if changes == 0:
        print("  ✅ SQLite already matches broker state — nothing to do.\n")
    elif args.dry_run:
        print("  Re-run without --dry-run to apply changes.\n")
    else:
        print("  ✅ SQLite updated successfully.\n")


if __name__ == "__main__":
    main()
