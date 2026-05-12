#!/usr/bin/env python3
"""Backfill NULL fields on closed trades with computed data."""
import sys
import os
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atlas_bootstrap import PROJECT_ROOT as PROJECT
from db.atlas_db import get_db

def compute_mae_mfe(db, ticker, entry_price, entry_date, exit_date):
    """Compute MAE and MFE from OHLCV data."""
    ed = entry_date[:10]
    xd = exit_date[:10]
    rows = db.execute(
        "SELECT low, high FROM ohlcv WHERE ticker=? AND date BETWEEN ? AND ?",
        (ticker, ed, xd)
    ).fetchall()
    if not rows:
        return None, None
    min_low = min(r['low'] for r in rows)
    max_high = max(r['high'] for r in rows)
    mae = round((min_low - entry_price) / entry_price * 100, 4)
    mfe = round((max_high - entry_price) / entry_price * 100, 4)
    return mae, mfe

def get_confidence(db, ticker, strategy, entry_date):
    """Get confidence from signals table."""
    ed = entry_date[:10]
    row = db.execute(
        "SELECT confidence FROM signals WHERE ticker=? AND strategy=? "
        "AND action IN ('accepted','proposed') "
        "AND substr(timestamp,1,10) = ? ORDER BY action ASC LIMIT 1",
        (ticker, strategy, ed)
    ).fetchone()
    if row:
        return row['confidence']
    # Fallback: ±1 day
    row = db.execute(
        "SELECT confidence FROM signals WHERE ticker=? AND strategy=? "
        "AND action IN ('accepted','proposed') "
        "AND substr(timestamp,1,10) BETWEEN date(?, '-1 day') AND date(?, '+1 day') "
        "ORDER BY action ASC, timestamp DESC LIMIT 1",
        (ticker, strategy, ed, ed)
    ).fetchone()
    return row['confidence'] if row else None

def get_regime(db, date_str):
    """Get regime state at a given date."""
    d = date_str[:10]
    row = db.execute(
        "SELECT regime_state FROM regime_history WHERE date <= ? ORDER BY date DESC LIMIT 1",
        (d,)
    ).fetchone()
    return row['regime_state'] if row else None

def main():
    parser = argparse.ArgumentParser(description="Backfill NULL fields on closed trades")
    parser.add_argument("--apply", action="store_true", help="Actually apply updates (default is dry run)")
    args = parser.parse_args()

    with get_db() as db:
        trades = db.execute(
            "SELECT id, ticker, strategy, entry_date, exit_date, entry_price, "
            "mae, mfe, confidence, regime_at_entry, regime_at_exit, config_version, universe "
            # Intentionally includes superseded rows — backfilling MAE/MFE for
            # audit completeness.  Do NOT add superseded=0 filter here.
            "FROM trades WHERE status='closed' ORDER BY id"
        ).fetchall()

        print(f"Found {len(trades)} closed trades to process\n")

        updates = []
        for t in trades:
            trade_id = t['id']
            ticker = t['ticker']
            strategy = t['strategy']
            entry_date = t['entry_date']
            exit_date = t['exit_date']
            entry_price = t['entry_price']

            # Compute what needs filling
            fields = {}

            if t['mae'] is None or t['mfe'] is None:
                mae, mfe = compute_mae_mfe(db, ticker, entry_price, entry_date, exit_date)
                if mae is not None:
                    fields['mae'] = mae
                if mfe is not None:
                    fields['mfe'] = mfe

            if t['confidence'] is None:
                conf = get_confidence(db, ticker, strategy, entry_date)
                if conf is not None:
                    fields['confidence'] = conf

            if t['regime_at_entry'] is None:
                regime = get_regime(db, entry_date)
                if regime:
                    fields['regime_at_entry'] = regime

            if t['regime_at_exit'] is None:
                regime = get_regime(db, exit_date)
                if regime:
                    fields['regime_at_exit'] = regime

            if t['config_version'] is None:
                fields['config_version'] = 'v3.2'

            if t['universe'] is None:
                fields['universe'] = 'sp500'

            if fields:
                updates.append((trade_id, ticker, strategy, fields))
                print(f"  Trade #{trade_id} {ticker} ({strategy}):")
                for k, v in fields.items():
                    old = t[k]
                    print(f"    {k}: {old} → {v}")
            else:
                print(f"  Trade #{trade_id} {ticker} ({strategy}): nothing to update")

        print(f"\n{'='*60}")
        print(f"Total trades: {len(trades)}, trades to update: {len(updates)}")

        if not updates:
            print("Nothing to backfill!")
                return

        if not args.apply:
            print("\nDRY RUN — use --apply to execute updates")
                return

        # Apply updates
        print("\nApplying updates...")
        for trade_id, ticker, strategy, fields in updates:
            set_clauses = ", ".join(f"{k}=?" for k in fields)
            values = list(fields.values()) + [trade_id]
            db.execute(
                f"UPDATE trades SET {set_clauses}, updated_at=datetime('now') WHERE id=?",
                values
            )
        print(f"Updated {len(updates)} trades.")

        # Print after state
        print(f"\n{'='*60}")
        print("AFTER STATE:")
        print(f"{'ID':>4} {'Ticker':<6} {'Strategy':<18} {'MAE':>8} {'MFE':>8} {'Conf':>6} {'Regime Entry':<22} {'Regime Exit':<22} {'CfgVer':<6}")
        print("-" * 120)
        trades_after = db.execute(
            "SELECT id, ticker, strategy, mae, mfe, confidence, regime_at_entry, regime_at_exit, config_version "
            # Intentionally includes superseded rows — backfilling MAE/MFE for audit completeness.
            "FROM trades WHERE status='closed' ORDER BY id"
        ).fetchall()
        for t in trades_after:
            print(f"{t['id']:>4} {t['ticker']:<6} {t['strategy']:<18} "
                  f"{t['mae'] or 'NULL':>8} {t['mfe'] or 'NULL':>8} "
                  f"{t['confidence'] or 'NULL':>6} "
                  f"{(t['regime_at_entry'] or 'NULL'):<22} "
                  f"{(t['regime_at_exit'] or 'NULL'):<22} "
                  f"{t['config_version'] or 'NULL':<6}")

        print("\nBackfill complete!")

if __name__ == "__main__":
    main()
