#!/usr/bin/env python3
"""
Backfill dual-write: bring SQLite in sync with JSON sources of truth.

Covers the three failures diagnosed on 2026-04-24 (P0-A):

  Trades  -- CROSS/momentum test-pollution in live_commodity_etfs.json was
             not in SQLite (and never should have been). Removed by the
             P0-A direct fix to live_commodity_etfs.json; the trades section
             here double-checks and reports any remaining orphan positions.

  Signals -- VERIFY_P19/momentum_breakout and NVDA/test_signal were written
             to decision_journal.json by the P1-9 verification run but never
             dual-written to SQLite. This backfills them so the
             verify_dual_write.py "latest 5 match" spot-check passes.

  Plans   -- record_plan() hardcoded status='pending' instead of using the
             plan's actual status ('PENDING_APPROVAL' etc.). Today's plan
             row (id=190, date=2026-04-24, sp500) was inserted with the
             wrong status. This UPDATE fixes it.

Usage:
    python3 scripts/migrations/2026-04-24-backfill-dual-write.py          # dry run
    python3 scripts/migrations/2026-04-24-backfill-dual-write.py --apply   # apply

Idempotent: each section checks before inserting/updating. Running twice
produces the same final state.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

import db.atlas_db as atlas_db  # noqa: E402

BROKER_STATE_DIR = PROJECT / "brokers" / "state"
DECISION_JOURNAL = PROJECT / "journal" / "decision_journal.json"
PLANS_DIR = PROJECT / "plans"

# Known test-pollution tickers that should NOT be inserted into SQLite
POLLUTION_TICKERS = frozenset({
    "CROSS", "WIN", "LOSE", "TEST", "GHOST", "DUP", "WARNTEST",
    "REOPEN", "GRACE", "IDCHECK",
})


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

def backfill_trades(apply):
    """Report any broker open positions missing from SQLite.

    Test-pollution tickers (CROSS etc.) are excluded from backfill.
    Returns the count of rows inserted (0 on dry-run or if already in sync).
    """
    print("\n=== Trades ===")
    inserted = 0
    skipped_pollution = []

    state_files = sorted(BROKER_STATE_DIR.glob("live_*.json"))
    if not state_files:
        print("  No broker state files found.")
        return 0

    broker_positions = {}
    for sf in state_files:
        try:
            data = json.loads(sf.read_text())
        except Exception as exc:
            print(f"  WARN: could not read {sf.name}: {exc}")
            continue
        for pos in data.get("positions", []):
            ticker = pos.get("ticker", "")
            if not ticker:
                continue
            if ticker not in broker_positions:
                broker_positions[ticker] = {**pos, "_source": sf.name}

    with atlas_db.get_db() as db:
        sqlite_open = {
            row["ticker"]
            for row in db.execute(
                "SELECT ticker FROM trades WHERE status='open'"
            ).fetchall()
        }

    missing = []
    for ticker, pos in broker_positions.items():
        if ticker in POLLUTION_TICKERS:
            skipped_pollution.append(ticker)
            continue
        if ticker not in sqlite_open:
            missing.append(pos)

    if skipped_pollution:
        print(f"  Skipped test-pollution tickers: {', '.join(sorted(skipped_pollution))}")

    if not missing:
        print("  Trades: all broker open positions present in SQLite. OK")
        return 0

    for pos in missing:
        ticker = pos["ticker"]
        strategy = pos.get("strategy") or "reconciled"
        if strategy == "unknown":
            strategy = "reconciled"
        entry_price = float(pos.get("entry_price") or 0)
        shares = int(pos.get("shares") or 0)
        stop_price = float(pos.get("stop_price") or 0)
        source = pos.get("_source", "?")
        universe = source.replace("live_", "").replace(".json", "") or "unknown"

        print(
            f"  {'[DRY RUN] Would INSERT' if not apply else 'INSERT'}: "
            f"{ticker}/{strategy} {shares}sh @ {entry_price:.2f} "
            f"stop={stop_price:.2f} [from {source}]"
        )

        if apply:
            try:
                result = atlas_db.record_trade_entry(
                    ticker=ticker,
                    strategy=strategy,
                    universe=universe,
                    entry_price=entry_price,
                    shares=shares,
                    stop_price=stop_price,
                    take_profit=None,
                    confidence=0.0,
                    regime_state=None,
                    direction="long",
                )
                if result:
                    print(f"    -> inserted id={result}")
                    inserted += 1
                else:
                    print(f"    -> UNIQUE violation (already exists as open trade)")
            except Exception as exc:
                print(f"    -> ERROR: {exc}")

    return inserted


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def backfill_signals(apply):
    """Insert any decision_journal.json entries missing from SQLite signals table.

    Uses (timestamp, ticker, strategy) as the deduplication key.
    Returns the count of rows inserted.
    """
    print("\n=== Signals ===")
    inserted = 0

    if not DECISION_JOURNAL.exists():
        print(f"  WARN: {DECISION_JOURNAL} missing -- skipping signal backfill.")
        return 0

    try:
        journal = json.loads(DECISION_JOURNAL.read_text())
    except Exception as exc:
        print(f"  ERROR reading decision_journal.json: {exc}")
        return 0

    sorted_journal = sorted(
        journal, key=lambda x: x.get("timestamp", ""), reverse=True
    )

    with atlas_db.get_db() as db:
        missing = []
        for entry in sorted_journal:
            ts = entry.get("timestamp", "")
            ticker = entry.get("ticker", "")
            strategy = entry.get("strategy", "")
            if not (ts and ticker and strategy):
                continue
            found = db.execute(
                "SELECT id FROM signals WHERE timestamp=? AND ticker=? AND strategy=? LIMIT 1",
                (ts, ticker, strategy),
            ).fetchone()
            if not found:
                missing.append(entry)

    if not missing:
        print(f"  Signals: all {len(journal)} JSON entries present in SQLite. OK")
        return 0

    print(f"  Found {len(missing)} signal(s) in JSON but not in SQLite.")

    for entry in missing:
        ts = entry.get("timestamp", "")
        ticker = entry.get("ticker", "")
        strategy = entry.get("strategy", "")
        action = entry.get("action", "")
        market_id = entry.get("market_id", "sp500")

        print(
            f"  {'[DRY RUN] Would INSERT' if not apply else 'INSERT'}: "
            f"{ts} {ticker}/{strategy} action={action}"
        )

        if apply:
            try:
                atlas_db.record_signal(
                    timestamp=ts,
                    ticker=ticker,
                    strategy=strategy,
                    universe=market_id,
                    entry_price=float(entry.get("entry_price") or 0),
                    stop_price=float(entry.get("stop_price") or 0),
                    position_size=int(entry.get("position_size") or 0),
                    position_value=float(entry.get("position_value") or 0),
                    risk_amount=float(entry.get("risk_amount") or 0),
                    confidence=float(entry.get("confidence") or 0),
                    action=action,
                    direction=entry.get("direction", "long"),
                    take_profit=entry.get("take_profit"),
                    rationale=entry.get("rationale"),
                    features=entry.get("features"),
                    sector=entry.get("sector"),
                    action_reason=entry.get("action_reason"),
                    config_version=entry.get("config_version"),
                    market_id=market_id,
                )
                print(f"    -> inserted")
                inserted += 1
            except Exception as exc:
                print(f"    -> ERROR: {exc}")

    return inserted


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

def backfill_plans(apply):
    """Fix status mismatches between plan JSON files and SQLite plans table.

    The P0-A bug in record_plan() hardcoded status='pending' regardless of
    the plan's actual status. This UPDATE corrects affected rows.

    Returns the count of rows updated.
    """
    print("\n=== Plans ===")
    updated = 0

    if not PLANS_DIR.exists():
        print(f"  WARN: {PLANS_DIR} missing -- skipping plan backfill.")
        return 0

    plan_files = sorted(PLANS_DIR.glob("plan_*.json"))
    if not plan_files:
        print("  No plan JSON files found.")
        return 0

    for plan_file in plan_files:
        try:
            plan_data = json.loads(plan_file.read_text())
        except Exception as exc:
            print(f"  WARN: could not read {plan_file.name}: {exc}")
            continue

        trade_date = plan_data.get("trade_date", "")
        market_id = plan_data.get("market_id", "sp500")
        json_status = plan_data.get("status", "").lower()

        if not (trade_date and json_status):
            continue

        with atlas_db.get_db() as db:
            row = db.execute(
                "SELECT id, status FROM plans WHERE date=? AND market_id=? ORDER BY id DESC LIMIT 1",
                (trade_date, market_id),
            ).fetchone()

        if not row:
            continue

        sqlite_status = row[1].lower()
        sqlite_norm = sqlite_status.replace("_", "")
        json_norm = json_status.replace("_", "")

        if sqlite_norm == json_norm:
            continue

        plan_id = row[0]
        print(
            f"  {'[DRY RUN] Would UPDATE' if not apply else 'UPDATE'}: "
            f"plan id={plan_id} date={trade_date} market={market_id}: "
            f"status '{sqlite_status}' -> '{json_status}'"
        )

        if apply:
            try:
                with atlas_db.get_db() as db:
                    db.execute(
                        "UPDATE plans SET status=? WHERE id=?",
                        (json_status, plan_id),
                    )
                print(f"    -> updated")
                updated += 1
            except Exception as exc:
                print(f"    -> ERROR: {exc}")

    return updated


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Backfill dual-write: sync SQLite with JSON sources of truth."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually apply changes. Without this flag, runs in dry-run mode.",
    )
    args = parser.parse_args()

    apply = args.apply
    mode = "APPLY" if apply else "DRY RUN"
    print(f"\n{'='*60}")
    print(f"  Dual-Write Backfill -- {mode}")
    print(f"  Project: {PROJECT}")
    print(f"{'='*60}")

    trades_inserted  = backfill_trades(apply)
    signals_inserted = backfill_signals(apply)
    plans_updated    = backfill_plans(apply)

    print(f"\n{'='*60}")
    if apply:
        print(f"  Applied: trades={trades_inserted} signals={signals_inserted} plans={plans_updated}")
    else:
        print(f"  Dry run complete (use --apply to apply changes).")
        print(f"  Pending: trades={trades_inserted} signals={signals_inserted} plans={plans_updated}")
    print(f"{'='*60}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
