#!/usr/bin/env python3
"""Backfill poison strategies on closed trades via plan/broker-state/ledger lookup.

Run:
  python3 scripts/migrations/2026-04-22-backfill-closed-trade-strategies.py --dry-run
  python3 scripts/migrations/2026-04-22-backfill-closed-trade-strategies.py --apply

CLI args:
  --dry-run     (default) Show proposed changes without writing.
  --apply       Execute UPDATEs inside a transaction, backup CSV first.
  --backup-csv  Path for CSV backup (default: data/backups/trades_backup_{ts}.csv).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Path bootstrap ────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve()
_ATLAS_ROOT = _HERE.parent.parent.parent
sys.path.insert(0, str(_ATLAS_ROOT))

# ── Constants ─────────────────────────────────────────────────────────────────
_POISON_STRATEGIES: frozenset[str] = frozenset({"unknown", "reconciled", ""})
_PRICE_TOLERANCE: float = 0.01
_PLAN_LOOKBACK_DAYS: int = 7  # search up to N days before entry_date

logger = logging.getLogger("backfill_strategies")


# ── DB path resolution ────────────────────────────────────────────────────────

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


# ── CSV backup ────────────────────────────────────────────────────────────────

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


# ── Plan file search ──────────────────────────────────────────────────────────

def _extract_strategy_from_plan_item(item: dict) -> str | None:
    """Return strategy if item has one and it is not poison."""
    strat = item.get("strategy") or ""
    if strat and strat not in _POISON_STRATEGIES:
        return strat
    return None


def _price_matches(candidate: float | None, target: float) -> bool:
    if candidate is None:
        return False
    return abs(float(candidate) - target) <= _PRICE_TOLERANCE


def _search_plan_items(items: list, ticker: str, entry_price: float) -> str | None:
    """Return first non-poison strategy from a list of plan entry dicts."""
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("ticker") != ticker:
            continue
        # Price field may be entry_price, fill_price, or price
        candidate_price = (
            item.get("entry_price")
            or item.get("fill_price")
            or item.get("price")
        )
        if not _price_matches(candidate_price, entry_price):
            continue
        strat = _extract_strategy_from_plan_item(item)
        if strat:
            return strat
    return None


def _search_plan_file(plan_path: Path, ticker: str, entry_price: float) -> str | None:
    """Search all known fields in a plan JSON for a (ticker, entry_price) match."""
    try:
        with plan_path.open() as f:
            plan = json.load(f)
    except Exception as exc:
        logger.debug("Could not read %s: %s", plan_path.name, exc)
        return None

    # Search locations (spec: proposed_entries, portfolio_snapshot.positions,
    # active_positions, entries; plus actual fields found: open_positions,
    # portfolio_snapshot.open_positions)
    search_locations: list[list] = []

    # Top-level lists
    for key in ("proposed_entries", "active_positions", "entries", "open_positions"):
        val = plan.get(key)
        if isinstance(val, list):
            search_locations.append(val)

    # Nested under portfolio_snapshot
    snapshot = plan.get("portfolio_snapshot")
    if isinstance(snapshot, dict):
        for key in ("positions", "open_positions", "entries"):
            val = snapshot.get(key)
            if isinstance(val, list):
                search_locations.append(val)

    for items in search_locations:
        strat = _search_plan_items(items, ticker, entry_price)
        if strat:
            return strat

    return None


def _attribute_from_plans(
    ticker: str,
    entry_price: float,
    entry_date_str: str,
) -> tuple[str, str] | None:
    """
    Try to resolve strategy from plan files.

    Searches plans/plan_*_{date}.json for entry_date down to 7 days prior.
    Skips plan files where the found strategy is itself poison.

    Returns (strategy, source_label) or None.
    """
    try:
        entry_dt = datetime.fromisoformat(entry_date_str.replace("Z", "+00:00"))
        base_date = entry_dt.date()
    except ValueError:
        logger.warning("Cannot parse entry_date: %s", entry_date_str)
        return None

    plans_dir = _ATLAS_ROOT / "plans"
    if not plans_dir.exists():
        return None

    # Search from entry_date back _PLAN_LOOKBACK_DAYS days
    for days_back in range(_PLAN_LOOKBACK_DAYS + 1):
        check_date = base_date - timedelta(days=days_back)
        date_str = check_date.strftime("%Y-%m-%d")
        # Glob all plan files for this date (any market)
        matching_files = sorted(plans_dir.glob(f"plan_*_{date_str}.json"), reverse=True)
        for plan_path in matching_files:
            strat = _search_plan_file(plan_path, ticker, entry_price)
            if strat:
                return strat, f"plan:{plan_path.relative_to(_ATLAS_ROOT)}"

    return None


# ── Broker state search ───────────────────────────────────────────────────────

def _attribute_from_broker_state(
    ticker: str,
    entry_price: float,
) -> tuple[str, str] | None:
    """Search live_*.json broker state files for a matching closed_trade or position."""
    state_dir = _ATLAS_ROOT / "brokers" / "state"
    if not state_dir.exists():
        return None

    for state_file in sorted(state_dir.glob("live_*.json")):
        try:
            with state_file.open() as f:
                state = json.load(f)
        except Exception as exc:
            logger.debug("Cannot read %s: %s", state_file.name, exc)
            continue

        # Search closed_trades and positions
        for section_key in ("closed_trades", "positions"):
            section = state.get(section_key)
            items: list = []
            if isinstance(section, list):
                items = section
            elif isinstance(section, dict):
                items = list(section.values())

            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("ticker") != ticker:
                    continue
                candidate_price = (
                    item.get("entry_price")
                    or item.get("fill_price")
                    or item.get("price")
                )
                if not _price_matches(candidate_price, entry_price):
                    continue
                strat = _extract_strategy_from_plan_item(item)
                if strat:
                    return strat, f"broker_state:{state_file.relative_to(_ATLAS_ROOT)}"

    return None


# ── Trade ledger search ───────────────────────────────────────────────────────

def _attribute_from_ledger(
    ticker: str,
    entry_price: float,
) -> tuple[str, str] | None:
    """Search journal/trade_ledger.json for a matching entry with non-poison strategy."""
    ledger_path = _ATLAS_ROOT / "journal" / "trade_ledger.json"
    if not ledger_path.exists():
        return None

    try:
        with ledger_path.open() as f:
            raw = json.load(f)
    except Exception as exc:
        logger.debug("Cannot read ledger: %s", exc)
        return None

    trades: list = raw if isinstance(raw, list) else raw.get("trades", [])
    for item in trades:
        if not isinstance(item, dict):
            continue
        if item.get("ticker") != ticker:
            continue
        candidate_price = item.get("fill_price") or item.get("entry_price") or item.get("price")
        if not _price_matches(candidate_price, entry_price):
            continue
        strat = _extract_strategy_from_plan_item(item)
        if strat:
            return strat, "ledger"

    return None


# ── Attribution orchestrator ──────────────────────────────────────────────────

_FALLBACK_LABEL = "FALLBACK_LEGACY_UNKNOWN"
_FALLBACK_STRATEGY = "legacy_unknown"


def _attribute(
    trade_id: int,
    ticker: str,
    entry_price: float,
    entry_date: str,
    universe: str,
) -> tuple[str, str]:
    """
    Resolve the strategy for a poison trade.

    Tries (in order):
      a) Plan files (entry_date back to _PLAN_LOOKBACK_DAYS days)
      b) Broker state files
      c) Trade ledger
      d) Fallback: 'legacy_unknown'

    Returns (strategy, source_label).
    """
    # Step a: plan files
    result = _attribute_from_plans(ticker, entry_price, entry_date)
    if result:
        return result

    # Step b: broker state
    result = _attribute_from_broker_state(ticker, entry_price)
    if result:
        return result

    # Step c: trade ledger
    result = _attribute_from_ledger(ticker, entry_price)
    if result:
        return result

    # Step d: fallback
    return _FALLBACK_STRATEGY, _FALLBACK_LABEL


# ── Core migration logic ──────────────────────────────────────────────────────

def run_migration(
    db_path: Path,
    dry_run: bool,
    backup_csv_path: Path | None,
) -> int:
    """
    Backfill poison strategies on closed trades.

    Returns exit code: 0 on success, 1 on error.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        # ── Fetch poison rows ─────────────────────────────────────────────
        cur = conn.execute(
            """
            SELECT id, ticker, strategy, universe, entry_date, entry_price, shares
              FROM trades
             WHERE strategy IN ('unknown', 'reconciled', '')
                OR strategy IS NULL
             ORDER BY id
            """
        )
        poison_rows = cur.fetchall()

        if not poison_rows:
            logger.info("0 rows to update — nothing to do")
            return 0

        logger.info("Found %d poison row(s) to evaluate", len(poison_rows))

        # ── Build proposed updates ────────────────────────────────────────
        updates: list[dict] = []
        for row in poison_rows:
            trade_id = row["id"]
            ticker = row["ticker"]
            old_strategy = row["strategy"] or ""
            universe = row["universe"] or ""
            entry_date = row["entry_date"] or ""
            entry_price = float(row["entry_price"] or 0)
            shares = row["shares"]

            new_strategy, source = _attribute(
                trade_id, ticker, entry_price, entry_date, universe
            )

            # Warn if universe mismatch suspected (plan vs DB universe)
            if "commodity_etfs" in source and universe != "commodity_etfs":
                logger.warning(
                    "id=%d ticker=%s: source is commodity_etfs plan but universe='%s' in DB "
                    "(out of scope — leaving universe unchanged)",
                    trade_id, ticker, universe,
                )

            logger.info(
                "id=%d ticker=%s entry=%s entry_price=%.4f old=%s -> new=%s [source=%s]",
                trade_id, ticker, entry_date[:10], entry_price,
                old_strategy or "NULL", new_strategy, source,
            )
            updates.append({
                "id": trade_id,
                "new_strategy": new_strategy,
                "old_strategy": old_strategy,
                "source": source,
                "ticker": ticker,
            })

        if dry_run:
            logger.info(
                "DRY RUN: would update %d row(s). Re-run with --apply to apply.",
                len(updates),
            )
            return 0

        # ── Safety check: must not regress real strategies to poison ──────
        for u in updates:
            new_s = u["new_strategy"]
            if new_s in _POISON_STRATEGIES:
                logger.error(
                    "SAFETY ABORT: id=%d would be updated TO poison strategy '%s' — "
                    "refusing to apply (only 'legacy_unknown' is allowed as fallback)",
                    u["id"], new_s,
                )
                return 1

        # ── Backup BEFORE any writes ──────────────────────────────────────
        if backup_csv_path is None:
            ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H%M%S")
            backup_csv_path = (
                _ATLAS_ROOT / "data" / "backups" / f"trades_backup_{ts}.csv"
            )
        _backup_csv(db_path, backup_csv_path)

        # ── Apply in a single transaction ─────────────────────────────────
        conn.execute("BEGIN")
        try:
            for u in updates:
                conn.execute(
                    "UPDATE trades SET strategy = ? WHERE id = ?",
                    (u["new_strategy"], u["id"]),
                )
                logger.info("UPDATED id=%d: '%s' → '%s'", u["id"], u["old_strategy"], u["new_strategy"])

            # Post-commit assertion: no poison rows remain
            cur2 = conn.execute(
                """
                SELECT COUNT(*) FROM trades
                 WHERE strategy IN ('unknown', 'reconciled', '')
                    OR strategy IS NULL
                """
            )
            remaining_poison = cur2.fetchone()[0]

            cur3 = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE strategy = 'legacy_unknown'"
            )
            legacy_count = cur3.fetchone()[0]

            if remaining_poison != 0:
                conn.execute("ROLLBACK")
                logger.error(
                    "ROLLED BACK: %d poison rows still remain after updates — "
                    "check attribution logic",
                    remaining_poison,
                )
                return 1

            conn.commit()
            logger.info("COMMITTED successfully")

        except Exception as exc:
            conn.execute("ROLLBACK")
            logger.error("ROLLED BACK due to exception: %s", exc, exc_info=True)
            return 1

        # ── Summary ───────────────────────────────────────────────────────
        source_breakdown: dict[str, int] = {}
        for u in updates:
            key = u["source"]
            source_breakdown[key] = source_breakdown.get(key, 0) + 1

        legacy_count_final = len([u for u in updates if u["new_strategy"] == _FALLBACK_STRATEGY])
        logger.info(
            "Backfilled %d trade(s). Unresolved (legacy_unknown): %d. "
            "Source breakdown: %s",
            len(updates), legacy_count_final, source_breakdown,
        )
        return 0

    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
        return 1
    finally:
        conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

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
        help="Path for CSV backup (default: data/backups/trades_backup_{ts}.csv).",
    )
    args = parser.parse_args(argv)

    # --apply wins over --dry-run default
    dry_run = not args.apply

    # Logging: stdout + file
    ts_str = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    log_path = _ATLAS_ROOT / "logs" / f"backfill_strategies_{ts_str}.log"
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
