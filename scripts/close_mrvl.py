#!/usr/bin/env python3
"""Close orphaned MRVL position at Alpaca.

Trade #117 (MRVL, 4 shares) was marked closed in Atlas DB but the
broker still holds the position. This script submits a market sell.
"""
import sys, os, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(Path(__file__).resolve().parent.parent)

from brokers.registry import get_live_broker
from brokers.base import OrderSide, OrderType

config_path = Path("config/active/sp500.json")
with open(config_path) as f:
    config = json.load(f)

broker = get_live_broker(config)
if not broker:
    print("ERROR: Could not get live broker")
    sys.exit(1)

if not broker.connect():
    print("ERROR: Broker connect failed")
    sys.exit(1)

# Check positions
positions = broker.get_positions()
mrvl = [p for p in positions if p.ticker == "MRVL"]
if not mrvl:
    print("MRVL not found at broker. Nothing to sell.")
    sys.exit(0)

pos = mrvl[0]
qty = pos.shares
print(f"Found MRVL: {qty} shares @ entry ${pos.entry_price:.2f}, current ${pos.current_price:.2f}")

# Cancel open orders for MRVL
try:
    open_orders = broker.get_open_orders()
    mrvl_orders = [o for o in open_orders if getattr(o, 'ticker', '') == 'MRVL' or
                   getattr(getattr(o, 'raw', {}), 'get', lambda k, d='': d)('symbol', '') == 'MRVL']
    for o in mrvl_orders:
        oid = getattr(o, 'order_id', '') or str(getattr(getattr(o, 'raw', {}), 'get', lambda k, d='': d)('id', ''))
        if oid:
            print(f"Canceling order {oid} for MRVL")
            broker.cancel_order(oid)
    if mrvl_orders:
        time.sleep(1.5)
        print(f"Cancelled {len(mrvl_orders)} MRVL order(s)")
    else:
        print("No open orders for MRVL")
except Exception as e:
    print(f"Warning: could not cancel orders: {e}")

# Submit market sell
print(f"Submitting market sell for {qty} shares of MRVL...")
result = broker.place_order(
    ticker="MRVL",
    side=OrderSide.SELL,
    qty=qty,
    price=0.0,
    order_type=OrderType.MARKET,
    remark="cleanup_orphan",
)
print(f"Order result: success={result.success}, order_id={result.order_id}")
if result.fill_price:
    print(f"Fill price: ${result.fill_price:.2f}")
if not result.success:
    print(f"Order FAILED: {result.message}")
    sys.exit(1)
print("DONE: MRVL sell order submitted successfully")
