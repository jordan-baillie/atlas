#!/usr/bin/env python3
"""One-off backfill: add cumulative realized P&L to historical equity_curve rows.

The equity() method previously computed:
    atlas_equity = starting_equity - entry_cost + position_value

It should have been:
    atlas_equity = starting_equity - entry_cost + realized_pnl + position_value

This script corrects all historical equity_curve rows by adding the cumulative
realized P&L at each date.
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.atlas_db import get_db


def main():
    # 1. Load closed trades from state file
    state_path = "brokers/state/live_sp500.json"
    with open(state_path) as f:
        state = json.load(f)
    
    closed_trades = state.get("closed_trades", [])
    print(f"Loaded {len(closed_trades)} closed trades")
    
    # 2. Build a mapping: date -> cumulative realized PnL up to that date
    # Sort trades by exit_date
    trades_by_date = {}
    for t in closed_trades:
        exit_date = t.get("exit_date", "")
        pnl = t.get("pnl", 0)
        if exit_date and pnl:
            if exit_date not in trades_by_date:
                trades_by_date[exit_date] = 0
            trades_by_date[exit_date] += pnl
    
    # Build cumulative PnL timeline
    sorted_dates = sorted(trades_by_date.keys())
    cumulative = {}
    running = 0
    for d in sorted_dates:
        running += trades_by_date[d]
        cumulative[d] = running
    
    print(f"Realized PnL dates: {len(cumulative)}")
    for d, v in cumulative.items():
        print(f"  {d}: cumulative realized = ${v:.2f}")
    
    # 3. Update equity_curve rows
    with get_db() as db:
        rows = db.execute(
            "SELECT date, equity FROM equity_curve WHERE market_id='sp500' ORDER BY date ASC"
        ).fetchall()
        
        print(f"\nUpdating {len(rows)} equity_curve rows:")
        
        for row in rows:
            row_date = row["date"]
            old_equity = row["equity"]
            
            # Find cumulative realized PnL at this date
            # (sum of all trades with exit_date <= row_date)
            cum_pnl = 0
            for d, v in cumulative.items():
                if d <= row_date:
                    cum_pnl = v  # cumulative is already running total
                else:
                    break
            
            new_equity = round(old_equity + cum_pnl, 2)
            
            if cum_pnl != 0:
                print(f"  {row_date}: ${old_equity:.2f} + ${cum_pnl:.2f} realized = ${new_equity:.2f}")
            else:
                print(f"  {row_date}: ${old_equity:.2f} (no realized trades yet)")
            
            db.execute(
                "UPDATE equity_curve SET equity = ? WHERE date = ? AND market_id = 'sp500'",
                (new_equity, row_date),
            )
        
        # 4. Recalculate day_pnl based on corrected equity values
        updated_rows = db.execute(
            "SELECT date, equity FROM equity_curve WHERE market_id='sp500' ORDER BY date ASC"
        ).fetchall()
        
        prev_eq = None
        for row in updated_rows:
            if prev_eq is not None:
                day_pnl = round(row["equity"] - prev_eq, 2)
                db.execute(
                    "UPDATE equity_curve SET day_pnl = ? WHERE date = ? AND market_id = 'sp500'",
                    (day_pnl, row["date"]),
                )
            prev_eq = row["equity"]
        
        print("\nday_pnl recalculated for all rows.")
    
    # 5. Also update equity_history in the state file
    equity_history = state.get("equity_history", [])
    for entry in equity_history:
        realized = entry.get("total_realized_pnl", 0)
        old_eq = entry["equity"]
        entry["equity"] = round(old_eq + realized, 2)
    
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2, default=str)
    
    print(f"\nUpdated {len(equity_history)} equity_history entries in state file.")
    print("Backfill complete!")


if __name__ == "__main__":
    main()
