#!/usr/bin/env python3
"""One-time cleanup: close orphaned MRVL position at Alpaca.

Trade #117 (MRVL, 4 shares) was marked as closed by EOD settlement
on 2026-04-11 but no sell order was submitted to the broker. This
script closes the position at Alpaca and reconciles Atlas state.

Usage:
    python scripts/close_mrvl_orphan.py [--dry-run]
"""
import sys
import json
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

TICKER = "MRVL"
SHARES = 4
TRADE_ID = 117
STATE_FILE = PROJECT / "brokers" / "state" / "live_sp500.json"
DB_PATH = PROJECT / "data" / "atlas.db"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"=== MRVL Orphan Cleanup {'[DRY RUN]' if args.dry_run else '[LIVE]'} ===")

    # 1. Connect to Alpaca
    from utils.config import get_active_config
    config = get_active_config("sp500")
    from brokers.registry import get_live_broker
    broker = get_live_broker(config)
    if not broker or not broker.connect():
        print("ERROR: Cannot connect to broker")
        return 1

    # 2. Check current position
    positions = broker.get_positions()
    mrvl_pos = next((p for p in positions if p.ticker == TICKER), None)
    if not mrvl_pos:
        print(f"No {TICKER} position found at broker — may already be closed.")
        # Still fix the DB and state file
    else:
        print(f"Found {TICKER}: {mrvl_pos.shares} shares @ ${mrvl_pos.current_price:.2f}")

    actual_price = mrvl_pos.current_price if mrvl_pos else 0

    # 3. Submit market sell
    if mrvl_pos and not args.dry_run:
        from brokers.base import OrderSide, OrderType
        from brokers.live_executor import LiveExecutor

        # Cancel any existing protective orders first
        try:
            _exec = LiveExecutor.__new__(LiveExecutor)
            _exec._broker = broker
            _exec._connected = True
            cancelled = _exec._cancel_open_orders_for_ticker(TICKER)
            if cancelled:
                import time
                time.sleep(1.0)
                print(f"Cancelled {cancelled} protective order(s) for {TICKER}")
        except Exception as e:
            print(f"Warning: Could not cancel protective orders: {e}")

        result = broker.place_order(
            ticker=TICKER,
            side=OrderSide.SELL,
            qty=SHARES,
            price=0.0,
            order_type=OrderType.MARKET,
            remark="orphan_cleanup",
        )
        if result.success:
            if result.fill_price and result.fill_price > 0:
                actual_price = result.fill_price
            print(f"SELL order submitted: id={result.order_id}, price=${actual_price:.2f}")
        else:
            print(f"ERROR: Sell failed: {result.message}")
            broker.disconnect()
            return 1
    elif mrvl_pos:
        print(f"[DRY RUN] Would sell {SHARES} shares of {TICKER} @ ~${actual_price:.2f}")

    # 4. Fix SQLite trade #117 exit_price
    if actual_price > 0 and not args.dry_run:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()

        # Update exit_price from phantom $124.41 to actual market price
        cursor.execute(
            "UPDATE trades SET exit_price = ?, updated_at = ? WHERE id = ?",
            (round(actual_price, 4), datetime.now().isoformat(), TRADE_ID),
        )

        # Recalculate PnL
        cursor.execute("SELECT entry_price, shares FROM trades WHERE id = ?", (TRADE_ID,))
        row = cursor.fetchone()
        if row:
            entry_price, shares = row
            pnl = round((actual_price - entry_price) * shares, 2)
            pnl_pct = round((actual_price - entry_price) / entry_price * 100, 2) if entry_price else 0
            cursor.execute(
                "UPDATE trades SET pnl = ?, pnl_pct = ? WHERE id = ?",
                (pnl, pnl_pct, TRADE_ID),
            )
            print(f"SQLite trade #{TRADE_ID}: exit_price=${actual_price:.4f}, pnl=${pnl:.2f} ({pnl_pct:.2f}%)")

        conn.commit()
        conn.close()
    elif actual_price > 0:
        print(f"[DRY RUN] Would update trade #{TRADE_ID} exit_price to ${actual_price:.2f}")

    # 5. Remove MRVL from JSON state file
    if not args.dry_run:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                state = json.load(f)
            before = len(state.get("positions", []))
            state["positions"] = [p for p in state.get("positions", []) if p.get("ticker") != TICKER]
            after = len(state["positions"])
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
            print(f"State file: removed {TICKER} ({before} -> {after} positions)")
    else:
        print(f"[DRY RUN] Would remove {TICKER} from state file")

    # 6. Verify - run reconciliation check
    if not args.dry_run:
        print("\n--- Post-cleanup verification ---")
        positions = broker.get_positions()
        mrvl_after = next((p for p in positions if p.ticker == TICKER), None)
        if mrvl_after:
            print(f"WARNING: {TICKER} still held at broker ({mrvl_after.shares} shares)")
        else:
            print(f"✓ {TICKER} no longer held at broker")

        # Check state file
        with open(STATE_FILE) as f:
            state = json.load(f)
        mrvl_state = any(p.get("ticker") == TICKER for p in state.get("positions", []))
        if mrvl_state:
            print(f"WARNING: {TICKER} still in state file")
        else:
            print(f"✓ {TICKER} removed from state file")

        # Check SQLite
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        cursor.execute("SELECT exit_price, pnl, status FROM trades WHERE id = ?", (TRADE_ID,))
        row = cursor.fetchone()
        conn.close()
        if row:
            print(f"✓ Trade #{TRADE_ID}: exit_price=${row[0]:.4f}, pnl=${row[1]:.2f}, status={row[2]}")

    broker.disconnect()
    print("\n=== Cleanup complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
