#!/usr/bin/env python3
"""One-shot fix: add missing CARR/MRVL to ledger + SQLite, sync closed trades, fix plan status."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

PROJECT = Path("/root/atlas")
LEDGER = PROJECT / "journal" / "trade_ledger.json"
BROKER = PROJECT / "brokers" / "state" / "live_sp500.json"
DB = PROJECT / "data" / "atlas.db"

now = datetime.now().isoformat()

# ── Step 1: Add CARR and MRVL to trade_ledger.json ──
with open(LEDGER) as f:
    ledger = json.load(f)

# Check what's already there for Apr 14
apr14_tickers = {e["ticker"] for e in ledger if e.get("timestamp","").startswith("2026-04-14") and e.get("type") == "entry"}
print(f"Apr 14 entry tickers already in ledger: {apr14_tickers}")

new_entries = []
if "CARR" not in apr14_tickers:
    new_entries.append({
        "type": "entry",
        "ticker": "CARR",
        "strategy": "momentum_breakout",
        "shares": 11,
        "fill_price": 63.31,
        "planned_price": 63.31,
        "stop_price": 60.2969,
        "slippage_bps": 0,
        "order_id": "",
        "timestamp": "2026-04-14T00:12:35",
        "direction": "long",
        "confidence": 0.9996,
        "market_id": "sp500",
        "config_version": "v3.2",
        "regime_state": "recovery_early",
        "recorded_at": now,
        "note": "Backfilled — limit order fill not reconciled inline"
    })

if "MRVL" not in apr14_tickers:
    new_entries.append({
        "type": "entry",
        "ticker": "MRVL",
        "strategy": "momentum_breakout",
        "shares": 4,
        "fill_price": 130.08,
        "planned_price": 130.08,
        "stop_price": 121.759,
        "slippage_bps": 0,
        "order_id": "",
        "timestamp": "2026-04-14T00:12:35",
        "direction": "long",
        "confidence": 0.8825,
        "market_id": "sp500",
        "config_version": "v3.2",
        "regime_state": "recovery_early",
        "recorded_at": now,
        "note": "Backfilled — limit order fill not reconciled inline"
    })

if new_entries:
    ledger.extend(new_entries)
    with open(LEDGER, "w") as f:
        json.dump(ledger, f, indent=2, default=str)
    print(f"Added {len(new_entries)} entries to ledger: {[e['ticker'] for e in new_entries]}")
else:
    print("No new ledger entries needed")

# ── Step 2: Add CARR and MRVL to SQLite trades table ──
conn = sqlite3.connect(str(DB))

existing_open = {r[0] for r in conn.execute("SELECT ticker FROM trades WHERE status='open'")}
print(f"Existing open trades in SQLite: {existing_open}")

if "CARR" not in existing_open:
    conn.execute("""
        INSERT INTO trades (ticker, strategy, universe, direction, entry_date, entry_price,
                           shares, stop_price, take_profit, confidence, regime_at_entry, status, config_version)
        VALUES ('CARR', 'momentum_breakout', 'sp500', 'long', '2026-04-14T00:12:35', 63.31,
                11, 60.2969, 75.3625, 0.9996, 'recovery_early', 'open', 'v3.2')
    """)
    print("Added CARR to SQLite trades")

if "MRVL" not in existing_open:
    conn.execute("""
        INSERT INTO trades (ticker, strategy, universe, direction, entry_date, entry_price,
                           shares, stop_price, take_profit, confidence, regime_at_entry, status, config_version)
        VALUES ('MRVL', 'momentum_breakout', 'sp500', 'long', '2026-04-14T00:12:35', 130.08,
                4, 121.759, 163.3642, 0.8825, 'recovery_early', 'open', 'v3.2')
    """)
    print("Added MRVL to SQLite trades")

# ── Step 3: Sync missing closed trades ──
with open(BROKER) as f:
    broker = json.load(f)

broker_closed = broker.get("closed_trades", [])
sqlite_closed = conn.execute("SELECT ticker, entry_date FROM trades WHERE status='closed'").fetchall()
# Build set of (ticker, entry_date[:10]) for matching
sqlite_set = {(r[0], r[1][:10]) for r in sqlite_closed}
print(f"Broker closed: {len(broker_closed)}, SQLite closed: {len(sqlite_closed)}")

missing = []
for t in broker_closed:
    ticker = t.get("ticker", "")
    entry_date = t.get("entry_date", "")[:10]
    if not ticker or not entry_date:
        continue
    if (ticker, entry_date) not in sqlite_set:
        missing.append(t)
        print(f"  Missing closed trade: {ticker} entry={entry_date}")

for t in missing:
    conn.execute("""
        INSERT INTO trades (ticker, strategy, universe, direction, entry_date, entry_price,
                           shares, stop_price, take_profit, exit_date, exit_price, exit_reason,
                           pnl, pnl_pct, confidence, regime_at_entry, status, config_version)
        VALUES (?, ?, 'sp500', 'long', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed', 'v3.2')
    """, (
        t.get("ticker"), t.get("strategy", "unknown"),
        t.get("entry_date", ""), float(t.get("entry_price", 0)),
        int(t.get("shares", 0)), float(t.get("stop_price", 0) or 0),
        float(t.get("take_profit", 0) or 0) if t.get("take_profit") else None,
        t.get("exit_date", ""), float(t.get("exit_price", 0)),
        t.get("exit_reason", ""), float(t.get("pnl", 0)),
        float(t.get("pnl_pct", 0)), float(t.get("confidence", 0)),
        t.get("regime_at_entry", ""),
    ))
    print(f"  Inserted closed trade: {t.get('ticker')} entry={t.get('entry_date','')[:10]}")

# ── Step 4: Fix plan status ──
conn.execute("""
    UPDATE plans SET status='executed', executed_at='2026-04-14T00:12:53.139819' WHERE id=121
""")
result = conn.execute("SELECT id, status, executed_at FROM plans WHERE id=121").fetchone()
print(f"Plan 121 updated: {result}")

conn.commit()
conn.close()

# ── Step 5: Verify ──
print("\n=== Verification ===")
conn2 = sqlite3.connect(str(DB))
open_trades = conn2.execute("SELECT ticker FROM trades WHERE status='open'").fetchall()
closed_count = conn2.execute("SELECT COUNT(*) FROM trades WHERE status='closed'").fetchone()[0]
plan_status = conn2.execute("SELECT status FROM plans WHERE id=121").fetchone()
conn2.close()

print(f"Open trades: {[r[0] for r in open_trades]}")
print(f"Closed trades: {closed_count}")
print(f"Plan 121 status: {plan_status}")
