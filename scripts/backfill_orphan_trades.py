#!/usr/bin/env python3
"""Backfill SQLite trades to match broker JSON open positions.

Purpose
-------
The dual-write bridge occasionally leaves broker-JSON open positions orphaned
in SQLite (not inserted) or with wrong strategy (backfilled as 'reconciled').
This script reconciles SQLite to the broker-JSON source of truth for OPEN
positions only — closed trades are left alone.

Resolution logic per ticker:
  1. If SQLite has NO open row → INSERT with strategy from broker JSON (if
     non-'unknown') else from most-recent plan else 'reconciled'.
  2. If SQLite has ONE open row but strategy differs from broker JSON (and
     broker JSON's strategy isn't 'unknown') → UPDATE strategy.
  3. If SQLite has MULTIPLE open rows for the same ticker → keep the oldest
     (lowest id), delete the rest (duplicates from re-reconciliation).

Usage:
    python3 scripts/backfill_orphan_trades.py --dry-run   # preview
    python3 scripts/backfill_orphan_trades.py             # apply
    python3 scripts/backfill_orphan_trades.py --quiet     # cron-friendly
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# ── Project root ──────────────────────────────────────────────────────────────
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

# ── Paths ─────────────────────────────────────────────────────────────────────
BROKER_STATE_DIR = PROJECT / "brokers" / "state"
PLANS_DIR = PROJECT / "plans"

logger = logging.getLogger(__name__)


# ── Strategy resolution ───────────────────────────────────────────────────────

def _load_plan_strategy(ticker: str, market_id: str) -> Optional[str]:
    """Return strategy for ticker from the most recent plan file for market_id.

    Scans plan_{market_id}_*.json files in descending date order and returns
    the first non-blank, non-'unknown' strategy found.
    """
    pattern = f"plan_{market_id}_*.json"
    plan_files = sorted(PLANS_DIR.glob(pattern), reverse=True)  # newest first
    for plan_file in plan_files:
        try:
            with open(plan_file) as fh:
                plan = json.load(fh)
            for entry in plan.get("proposed_entries", []):
                if entry.get("ticker") == ticker:
                    strat = entry.get("strategy", "")
                    if strat and strat != "unknown":
                        logger.debug(
                            "Found strategy for %s in %s: %s",
                            ticker, plan_file.name, strat,
                        )
                        return strat
        except Exception as exc:
            logger.warning("Failed to read plan %s: %s", plan_file.name, exc)
    return None


def resolve_strategy(
    ticker: str,
    market_id: str,
    broker_strategy: str,
) -> tuple[str, str]:
    """Return (strategy, source) using priority: broker JSON → plan → fallback.

    Parameters
    ----------
    ticker:          Ticker symbol.
    market_id:       Market identifier (e.g. 'sp500', 'commodity_etfs').
    broker_strategy: Strategy from broker JSON (may be 'unknown').

    Returns
    -------
    (strategy, source) where source is 'broker', 'plan', or 'fallback'.
    """
    if broker_strategy and broker_strategy not in ("unknown", ""):
        return broker_strategy, "broker"

    plan_strat = _load_plan_strategy(ticker, market_id)
    if plan_strat:
        return plan_strat, "plan"

    logger.warning(
        "Could not resolve strategy for %s/%s — falling back to 'reconciled'",
        ticker,
        market_id,
    )
    return "reconciled", "fallback"


# ── Broker state loading ──────────────────────────────────────────────────────

def load_broker_positions() -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Load the union of open positions from all live_*.json files.

    Returns
    -------
    (positions_by_ticker, market_counts)
      positions_by_ticker: dict keyed by ticker; each value is the position
        dict with an extra 'market_id' key derived from the filename.
      market_counts: dict mapping market_id → position count.

    Deduplication rule: when a ticker appears in multiple files, prefer the
    entry with a non-'unknown' strategy.  Ties broken by first-seen order.
    This mirrors the logic in verify_dual_write.py.
    """
    broker_files = sorted(
        f for f in BROKER_STATE_DIR.glob("live_*.json")
        if f.suffix == ".json"
    )

    positions_by_ticker: dict[str, dict[str, Any]] = {}
    market_counts: dict[str, int] = {}

    for state_file in broker_files:
        # Derive market_id: live_sp500.json → sp500
        market_id = state_file.stem.removeprefix("live_")

        try:
            with open(state_file) as fh:
                data = json.load(fh)
        except Exception as exc:
            logger.error("Failed to load %s: %s", state_file.name, exc)
            continue

        positions = data.get("positions", [])
        market_counts[market_id] = len(positions)

        for pos in positions:
            ticker = pos.get("ticker", "")
            if not ticker:
                continue

            pos_enriched = dict(pos)
            pos_enriched["market_id"] = market_id

            existing = positions_by_ticker.get(ticker)
            if existing is None:
                positions_by_ticker[ticker] = pos_enriched
            elif (
                existing.get("strategy", "unknown") in ("unknown", "")
                and pos_enriched.get("strategy", "unknown") not in ("unknown", "")
            ):
                # Upgrade to entry with a known strategy
                positions_by_ticker[ticker] = pos_enriched

    return positions_by_ticker, market_counts


# ── SQLite open trades ────────────────────────────────────────────────────────

def load_sqlite_open_trades() -> dict[str, list[dict[str, Any]]]:
    """Return all open trades from SQLite, grouped by ticker.

    Returns dict[ticker → list of trade dicts], sorted by ascending id within
    each ticker group (lowest id first → oldest row kept on dedup).
    """
    from db import atlas_db

    with atlas_db.get_db() as db:
        rows = db.execute(
            "SELECT id, ticker, strategy, universe, entry_date, entry_price, "
            "shares, stop_price FROM trades WHERE status='open' ORDER BY id"
        ).fetchall()

    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        t = row["ticker"]
        if t not in by_ticker:
            by_ticker[t] = []
        by_ticker[t].append(dict(row))

    return by_ticker


# ── Repair actions ────────────────────────────────────────────────────────────

def _do_insert(
    pos: dict[str, Any],
    strategy: str,
    market_id: str,
    dry_run: bool,
) -> bool:
    """INSERT a new open trade row for a broker position.

    Returns True on success (or in dry-run mode).  Returns False if the
    INSERT raises an exception.
    """
    from db import atlas_db

    ticker = pos["ticker"]
    entry_price = float(pos.get("entry_price") or 0)
    shares = int(pos.get("shares") or 0)

    stop_price_raw = pos.get("stop_price")
    if stop_price_raw is None or float(stop_price_raw) == 0.0:
        stop_price = round(entry_price * 0.95, 4)
    else:
        stop_price = float(stop_price_raw)

    # Prefer broker JSON entry_date to preserve historical timing
    entry_date: str = pos.get("entry_date") or datetime.now().isoformat()

    if dry_run:
        return True

    try:
        with atlas_db.get_db() as db:
            db.execute(
                """
                INSERT INTO trades
                    (ticker, strategy, universe, direction, entry_date,
                     entry_price, shares, stop_price, take_profit, confidence,
                     regime_at_entry, status, config_version)
                VALUES (?, ?, ?, 'long', ?, ?, ?, ?, NULL, 0.0, NULL, 'open', NULL)
                """,
                (ticker, strategy, market_id, entry_date,
                 entry_price, shares, stop_price),
            )
        logger.info("Inserted open trade: %s/%s (market=%s)", ticker, strategy, market_id)
        return True
    except Exception as exc:
        logger.error("INSERT failed for %s: %s", ticker, exc)
        return False


def _do_update_strategy(
    trade_id: int,
    ticker: str,
    old_strategy: str,
    new_strategy: str,
    dry_run: bool,
) -> bool:
    """UPDATE the strategy column of an existing open trade.

    Returns True on success (or dry-run).
    """
    from db import atlas_db

    if dry_run:
        return True

    try:
        with atlas_db.get_db() as db:
            db.execute(
                "UPDATE trades SET strategy=?, updated_at=datetime('now') WHERE id=?",
                (new_strategy, trade_id),
            )
        logger.info(
            "Updated strategy: %s id=%d '%s' → '%s'",
            ticker, trade_id, old_strategy, new_strategy,
        )
        return True
    except Exception as exc:
        logger.error(
            "UPDATE strategy failed for trade id=%d %s: %s", trade_id, ticker, exc
        )
        return False


def _do_delete(trade_id: int, ticker: str, dry_run: bool) -> bool:
    """DELETE a duplicate open trade row.

    Returns True on success (or dry-run).
    """
    from db import atlas_db

    if dry_run:
        return True

    try:
        with atlas_db.get_db() as db:
            db.execute("DELETE FROM trades WHERE id=?", (trade_id,))
        logger.info("Deleted duplicate trade row: %s id=%d", ticker, trade_id)
        return True
    except Exception as exc:
        logger.error(
            "DELETE failed for trade id=%d %s: %s", trade_id, ticker, exc
        )
        return False


# ── Main logic ────────────────────────────────────────────────────────────────

def run(dry_run: bool = False, quiet: bool = False) -> int:
    """Execute the orphan-backfill logic.

    Returns 0 on success, 1 if any repair action failed.

    Parameters
    ----------
    dry_run: Print intended changes without executing them.
    quiet:   Suppress stdout output (errors still log via logger).
    """
    today = datetime.now().strftime("%Y-%m-%d")

    def _print(msg: str) -> None:
        if not quiet:
            print(msg)

    _print(f"Backfill orphan trades — {today}")

    # ── Load broker positions ──────────────────────────────────────────────
    broker_positions, market_counts = load_broker_positions()

    # ── Load SQLite open trades ────────────────────────────────────────────
    sqlite_open = load_sqlite_open_trades()
    sqlite_total = sum(len(v) for v in sqlite_open.values())

    market_summary = ", ".join(
        f"{m}: {c}" for m, c in sorted(market_counts.items())
    )
    _print(
        f"  Broker positions (union): {len(broker_positions)}"
        f" ({market_summary})"
    )
    _print(f"  SQLite open trades:        {sqlite_total}")

    if dry_run:
        _print("  Mode: DRY RUN (no changes will be applied)")

    inserts: list[str] = []
    updates: list[str] = []
    deletes: list[str] = []
    sqlite_only: list[str] = []
    failures: list[str] = []

    # ── Process each broker position in ticker order ───────────────────────
    for ticker in sorted(broker_positions):
        pos = broker_positions[ticker]
        market_id = pos["market_id"]
        broker_strategy = (pos.get("strategy") or "unknown").strip()

        sqlite_rows = sqlite_open.get(ticker, [])

        # ── Case 3: duplicate open rows → keep min(id), delete the rest ──
        if len(sqlite_rows) > 1:
            keep_id = min(r["id"] for r in sqlite_rows)
            for row in sqlite_rows:
                if row["id"] != keep_id:
                    label = (
                        f"DELETE: {ticker} duplicate id={row['id']}"
                        f" (kept id={keep_id})"
                    )
                    deletes.append(label)
                    _print(f"  {label}")
                    if not _do_delete(row["id"], ticker, dry_run):
                        failures.append(label)
            # Reduce to just the kept row for subsequent checks
            sqlite_rows = [r for r in sqlite_rows if r["id"] == keep_id]

        # ── Case 1: no SQLite match → INSERT ──────────────────────────────
        if not sqlite_rows:
            strategy, source = resolve_strategy(ticker, market_id, broker_strategy)
            label = (
                f"INSERT: {ticker}/{strategy} ({market_id})"
                f" — from {source}"
            )
            inserts.append(label)
            _print(f"  {label}")
            if not _do_insert(pos, strategy, market_id, dry_run):
                failures.append(label)
            continue

        # ── Case 2: one row — check for strategy mismatch ─────────────────
        row = sqlite_rows[0]
        sqlite_strategy = (row.get("strategy") or "").strip()

        # Only update if broker JSON has a definitive (non-unknown) strategy
        # that differs from what SQLite currently has.
        if (
            broker_strategy not in ("unknown", "")
            and broker_strategy != sqlite_strategy
        ):
            label = (
                f"UPDATE: {ticker} strategy '{sqlite_strategy}'"
                f" → '{broker_strategy}' (id={row['id']}) — from broker"
            )
            updates.append(label)
            _print(f"  {label}")
            if not _do_update_strategy(
                row["id"], ticker, sqlite_strategy, broker_strategy, dry_run
            ):
                failures.append(label)

    # ── Log SQLite-only positions (no broker backing) ──────────────────────
    broker_tickers = set(broker_positions.keys())
    for ticker in sorted(sqlite_open):
        if ticker not in broker_tickers:
            for row in sqlite_open[ticker]:
                msg = (
                    f"WARNING: SQLite open without broker backing:"
                    f" {ticker}/{row['strategy']}/id={row['id']}"
                )
                sqlite_only.append(msg)
                logger.warning(
                    "SQLite open without broker backing: %s/%s id=%d",
                    ticker, row["strategy"], row["id"],
                )
                _print(f"  {msg}")

    # ── Summary ────────────────────────────────────────────────────────────
    total_changes = len(inserts) + len(updates) + len(deletes)
    _print(f"  SQLite-only (no broker): {len(sqlite_only)}")
    mode_label = "Preview" if dry_run else "Applied"
    dry_note = " (DRY RUN — no changes applied)" if dry_run else ""
    _print(f"  {mode_label}: {total_changes} changes{dry_note}")

    if failures:
        logger.error(
            "Backfill completed with %d failure(s): %s",
            len(failures),
            failures,
        )
        return 1

    return 0


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill SQLite trades to match broker JSON open positions"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without applying them",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stdout output (errors still go to logger)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    sys.exit(run(dry_run=args.dry_run, quiet=args.quiet))


if __name__ == "__main__":
    main()
