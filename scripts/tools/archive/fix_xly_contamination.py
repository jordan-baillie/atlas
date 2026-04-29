#!/usr/bin/env python3
"""One-shot cleanup: move XLY from live_commodity_etfs.json → live_sector_etfs.json.

XLY (Consumer Discretionary SPDR) was incorrectly written into the
commodity_etfs state file (strategy='unknown') via a reconcile_entry_fills
ghost-write.  Its canonical universe is sector_etfs, and trades.id=167
records it correctly (strategy='momentum_breakout').

Usage:
    python3 scripts/fix_xly_contamination.py            # dry-run (default)
    python3 scripts/fix_xly_contamination.py --dry-run  # explicit dry-run
    python3 scripts/fix_xly_contamination.py --apply    # execute
    python3 scripts/fix_xly_contamination.py --verify   # assert clean state
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("fix_xly_contamination")

STATE_DIR = PROJECT / "brokers" / "state"
COMMODITY_STATE = STATE_DIR / "live_commodity_etfs.json"
SECTOR_STATE = STATE_DIR / "live_sector_etfs.json"

# XLY canonical values (confirmed from SQLite trades.id=167)
XLY_TICKER = "XLY"
XLY_STRATEGY_CORRECT = "momentum_breakout"
XLY_SHARES = 10


def _load_json(path: Path) -> dict:
    """Load JSON state file, return empty template if missing."""
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON to .tmp file, fsync, then rename (atomic)."""
    tmp = path.with_suffix(".tmp")
    content = json.dumps(data, indent=2)
    with open(tmp, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    tmp.rename(path)


def _get_xly_from_db() -> dict | None:
    """Pull XLY entry fields from SQLite trades table (id=167)."""
    try:
        from db.atlas_db import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT ticker, strategy, entry_date, entry_price, stop_price "
                "FROM trades WHERE ticker = ? AND exit_date IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (XLY_TICKER,),
            ).fetchone()
            if row:
                return dict(row)
    except Exception as e:
        logger.warning("Could not read XLY from SQLite: %s", e)
    return None


def _verify_broker_xly() -> tuple[bool, int, float]:
    """Check Alpaca actually holds XLY.  Returns (found, shares, entry_price)."""
    try:
        from brokers.registry import get_broker
        from utils.config import get_active_config
        cfg = get_active_config("sp500")  # shares same Alpaca account
        broker = get_broker("sp500", cfg)
        if not broker or not broker.connect():
            return False, 0, 0.0
        positions = broker.get_positions()
        for p in positions:
            if p.ticker == XLY_TICKER:
                return True, int(p.shares), float(p.entry_price)
    except Exception as e:
        logger.warning("Broker check failed: %s", e)
    return False, 0, 0.0


def run(dry_run: bool = True, verify_only: bool = False) -> int:
    """Main logic.  Returns 0 on success, 1 on failure."""

    # ── VERIFY MODE ──────────────────────────────────────────────
    if verify_only:
        errors = []

        # commodity_etfs must NOT have XLY
        commodity = _load_json(COMMODITY_STATE)
        commodity_tickers = [p.get("ticker") for p in commodity.get("positions", [])]
        if XLY_TICKER in commodity_tickers:
            errors.append(f"FAIL: XLY still present in {COMMODITY_STATE.name}")
        else:
            print(f"  ✓ {COMMODITY_STATE.name}: no XLY (clean)")

        # sector_etfs MUST have XLY
        sector = _load_json(SECTOR_STATE)
        sector_tickers = [p.get("ticker") for p in sector.get("positions", [])]
        if XLY_TICKER not in sector_tickers:
            errors.append(f"FAIL: XLY NOT present in {SECTOR_STATE.name}")
        else:
            xly_entry = next(p for p in sector.get("positions", []) if p.get("ticker") == XLY_TICKER)
            print(f"  ✓ {SECTOR_STATE.name}: XLY present "
                  f"(strategy={xly_entry.get('strategy')}, "
                  f"entry_price={xly_entry.get('entry_price')})")

        if errors:
            for e in errors:
                logger.error(e)
            return 1
        print("Verify PASSED — state is clean")
        return 0

    # ── MAIN LOGIC ────────────────────────────────────────────────

    # Step 1: Read commodity_etfs state
    if not COMMODITY_STATE.exists():
        logger.error("commodity_etfs state file not found: %s", COMMODITY_STATE)
        return 1
    commodity = _load_json(COMMODITY_STATE)
    commodity_positions = commodity.get("positions", [])
    commodity_tickers = [p.get("ticker") for p in commodity_positions]

    xly_in_commodity = XLY_TICKER in commodity_tickers

    # Step 2: Verify broker holds XLY
    print("Checking broker for XLY position...")
    broker_ok, broker_shares, broker_entry = _verify_broker_xly()
    if not broker_ok:
        logger.error(
            "ABORT: broker does not hold XLY — cannot safely move state entry. "
            "XLY may have already been sold. Check Alpaca manually."
        )
        return 1
    print(f"  ✓ Broker holds XLY: {broker_shares} shares @ ${broker_entry:.2f}")

    # Step 3: Get SQLite metadata for XLY
    db_row = _get_xly_from_db()
    if db_row:
        entry_price = db_row.get("entry_price") or broker_entry
        stop_price = db_row.get("stop_price") or 0.0
        strategy = XLY_STRATEGY_CORRECT  # always use canonical value
        entry_date_raw = db_row.get("entry_date") or ""
        # Normalise ISO datetime to date-only
        entry_date = entry_date_raw[:10] if entry_date_raw else "2026-04-22"
        logger.info("SQLite XLY: strategy=%s entry_date=%s entry_price=%.2f stop_price=%.4f",
                    strategy, entry_date, entry_price, stop_price)
    else:
        logger.warning("XLY not found in SQLite open trades — using broker + defaults")
        entry_price = broker_entry
        stop_price = 110.618  # last known stop from original state file
        strategy = XLY_STRATEGY_CORRECT
        entry_date = "2026-04-22"

    # Step 4: Read or create sector_etfs state
    if SECTOR_STATE.exists():
        sector = _load_json(SECTOR_STATE)
    else:
        sector = {
            "market_id": "sector_etfs",
            "mode": "live",
            "positions": [],
            "closed_trades": [],
            "equity_history": [],
        }
    sector_positions = sector.get("positions", [])
    sector_tickers = [p.get("ticker") for p in sector_positions]
    xly_in_sector = XLY_TICKER in sector_tickers

    # ── Summary ──────────────────────────────────────────────────
    print("\n── Before ──────────────────────────────────────────")
    print(f"  commodity_etfs positions: {commodity_tickers}")
    print(f"  sector_etfs positions:    {sector_tickers}")
    print(f"  XLY in commodity_etfs:    {xly_in_commodity}")
    print(f"  XLY in sector_etfs:       {xly_in_sector}")

    # Check idempotency: already clean
    if not xly_in_commodity and xly_in_sector:
        print("\n✓ State already clean (XLY not in commodity_etfs, present in sector_etfs). "
              "Nothing to do.")
        return 0

    if not xly_in_commodity and not xly_in_sector:
        print("\n  Note: XLY already removed from commodity_etfs (likely cleaned by intraday_monitor).")
        print("  Will still create sector_etfs entry for XLY (broker confirms holding).")

    # Build XLY position entry
    xly_entry: dict = {
        "ticker": XLY_TICKER,
        "strategy": strategy,
        "entry_date": entry_date,
        "entry_price": entry_price,
        "shares": broker_shares,
        "stop_price": stop_price,
        "order_id": "",
    }

    # Build new commodity_etfs positions (XLY removed)
    new_commodity_positions = [p for p in commodity_positions if p.get("ticker") != XLY_TICKER]
    new_commodity = dict(commodity)
    new_commodity["positions"] = new_commodity_positions

    # Build new sector_etfs positions (XLY added/updated)
    new_sector_positions = [p for p in sector_positions if p.get("ticker") != XLY_TICKER]
    new_sector_positions.append(xly_entry)
    new_sector = dict(sector)
    new_sector["positions"] = new_sector_positions
    new_sector.setdefault("market_id", "sector_etfs")
    new_sector.setdefault("mode", "live")
    new_sector.setdefault("closed_trades", [])
    new_sector.setdefault("equity_history", [])

    print("\n── After (planned) ─────────────────────────────────")
    print(f"  commodity_etfs positions: {[p['ticker'] for p in new_commodity_positions]}")
    print(f"  sector_etfs positions:    {[p['ticker'] for p in new_sector_positions]}")
    print(f"  XLY entry: {json.dumps(xly_entry)}")

    if dry_run:
        print("\n[DRY RUN] No files written. Pass --apply to execute.")
        return 0

    # ── Write files atomically ────────────────────────────────────
    logger.info("Writing commodity_etfs state → removing XLY")
    _atomic_write(COMMODITY_STATE, new_commodity)

    logger.info("Writing sector_etfs state → adding XLY")
    _atomic_write(SECTOR_STATE, new_sector)

    print("\n✓ State files updated successfully.")
    print(f"  {COMMODITY_STATE.name}: {[p['ticker'] for p in new_commodity_positions]}")
    print(f"  {SECTOR_STATE.name}: {[p['ticker'] for p in new_sector_positions]}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=False,
                       help="Show what would change, do NOT write (default)")
    group.add_argument("--apply", action="store_true", default=False,
                       help="Execute the state file changes")
    group.add_argument("--verify", action="store_true", default=False,
                       help="Assert clean state (no XLY in commodity_etfs, XLY in sector_etfs)")
    args = parser.parse_args()

    if args.verify:
        sys.exit(run(dry_run=False, verify_only=True))
    elif args.apply:
        sys.exit(run(dry_run=False))
    else:
        # Default: dry-run
        sys.exit(run(dry_run=True))


if __name__ == "__main__":
    main()
