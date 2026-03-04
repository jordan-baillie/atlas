#!/usr/bin/env python3
"""Alpaca Markets API connectivity test.

Manual integration test — connects to Alpaca paper (or live) trading
and exercises: account info → positions → place test order → cancel → quote.

Usage:
    python3 scripts/test_alpaca.py           # paper trading (default)
    python3 scripts/test_alpaca.py --live    # live trading (USE WITH CARE)

Credentials are read from ~/.atlas-secrets.json or environment:
    ALPACA_API_KEY    — Alpaca API key ID
    ALPACA_SECRET_KEY — Alpaca API secret key

If credentials are missing the script prints a clear error and exits.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _hdr(title: str):
    """Print a section header."""
    print(f"\n{'─' * 55}")
    print(f"  {title}")
    print(f"{'─' * 55}")


def _pp(label: str, value):
    """Pretty-print a labelled value."""
    if isinstance(value, float):
        print(f"  {label:<30} {value:,.4f}")
    else:
        print(f"  {label:<30} {value}")


def main():
    parser = argparse.ArgumentParser(description="Alpaca API connectivity test")
    parser.add_argument(
        "--live", action="store_true",
        help="Use live (real-money) account instead of paper",
    )
    parser.add_argument(
        "--order-symbol", default="AAPL",
        help="Symbol for test order (default: AAPL)",
    )
    args = parser.parse_args()

    mode = "LIVE (real money)" if args.live else "PAPER"
    print("=" * 55)
    print(f"  Alpaca Markets API Test  [{mode}]")
    print("=" * 55)

    # ── 1. Check credentials ────────────────────────────────────────────────
    _hdr("1. Credentials")

    from brokers.secrets import get_secret

    api_key = get_secret("ALPACA_API_KEY")
    secret_key = get_secret("ALPACA_SECRET_KEY")

    if not api_key:
        print("❌ No ALPACA_API_KEY configured")
        print()
        print("  Add to ~/.atlas-secrets.json:")
        print('  {')
        print('    "ALPACA_API_KEY": "your-key-id",')
        print('    "ALPACA_SECRET_KEY": "your-secret-key"')
        print('  }')
        print()
        print("  Or set environment variables:")
        print("    export ALPACA_API_KEY='your-key-id'")
        print("    export ALPACA_SECRET_KEY='your-secret-key'")
        sys.exit(1)

    print(f"  ✅ ALPACA_API_KEY    {api_key[:6]}{'*' * (len(api_key) - 6)}")
    print(f"  ✅ ALPACA_SECRET_KEY {'*' * 8}")

    # ── 2. Import alpaca-py ────────────────────────────────────────────────
    _hdr("2. Import alpaca-py")

    try:
        import alpaca
        print(f"  ✅ alpaca-py v{alpaca.__version__} imported")
    except ImportError as e:
        print(f"  ❌ alpaca-py not installed: {e}")
        print("     Run: pip install alpaca-py")
        sys.exit(1)

    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import (
            MarketOrderRequest,
            LimitOrderRequest,
            GetOrdersRequest,
        )
        from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
        print("  ✅ Trading client imports OK")
    except ImportError as e:
        print(f"  ❌ alpaca-py import failed: {e}")
        sys.exit(1)

    # ── 3. Connect ──────────────────────────────────────────────────────────
    _hdr("3. Connect")

    paper = not args.live
    try:
        client = TradingClient(
            api_key=api_key,
            secret_key=secret_key,
            paper=paper,
        )
        print(f"  ✅ TradingClient created ({'paper' if paper else 'LIVE'})")
    except Exception as e:
        print(f"  ❌ Failed to create TradingClient: {e}")
        sys.exit(1)

    # ── 4. Account info ─────────────────────────────────────────────────────
    _hdr("4. Account Info")

    try:
        account = client.get_account()
        _pp("Account Number", account.account_number)
        _pp("Equity",         float(account.equity or 0))
        _pp("Cash",           float(account.cash or 0))
        _pp("Buying Power",   float(account.buying_power or 0))
        _pp("Currency",       account.currency)
        _pp("Trading Blocked", account.trading_blocked)
        _pp("Account Blocked", account.account_blocked)
        print("  ✅ Account info OK")
    except Exception as e:
        print(f"  ❌ get_account failed: {e}")
        import traceback; traceback.print_exc()

    # ── 5. Positions ─────────────────────────────────────────────────────────
    _hdr("5. Positions")

    try:
        positions = client.get_all_positions()
        if not positions:
            print("  (no open positions)")
        else:
            print(f"  {len(positions)} position(s):")
            for pos in positions:
                pnl = float(pos.unrealized_pl or 0)
                pnl_pct = float(pos.unrealized_plpc or 0) * 100
                print(
                    f"    {pos.symbol:<8} {int(float(pos.qty or 0)):>5} shares  "
                    f"@ ${float(pos.avg_entry_price or 0):.2f}  "
                    f"→ ${float(pos.current_price or 0):.2f}  "
                    f"PnL: {'+' if pnl >= 0 else ''}{pnl:.2f} ({pnl_pct:+.2f}%)"
                )
        print("  ✅ Positions OK")
    except Exception as e:
        print(f"  ❌ get_all_positions failed: {e}")
        import traceback; traceback.print_exc()

    # ── 6. Open orders ────────────────────────────────────────────────────
    _hdr("6. Open Orders")

    try:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        open_orders = client.get_orders(filter=req)
        if not open_orders:
            print("  (no open orders)")
        else:
            print(f"  {len(open_orders)} open order(s):")
            for order in open_orders:
                limit = float(order.limit_price or 0)
                qty = int(float(order.qty or 0))
                print(
                    f"    {order.symbol:<8} {order.side.value.upper():<4} {qty:>5} shares  "
                    f"limit=${limit:.2f}  status={order.status.value}"
                )
        print("  ✅ Open orders OK")
    except Exception as e:
        print(f"  ❌ get_orders failed: {e}")
        import traceback; traceback.print_exc()

    # ── 7. Place test order (far below market — immediately cancellable) ──
    symbol = args.order_symbol
    _hdr(f"7. Place Test Order ({symbol})")

    test_order_id = None
    try:
        # Limit order far below market to avoid accidental fills
        test_req = LimitOrderRequest(
            symbol=symbol,
            qty=1,
            side=OrderSide.BUY,
            limit_price=0.01,   # deliberately too low to fill
            time_in_force=TimeInForce.DAY,
        )
        order = client.submit_order(order_data=test_req)
        test_order_id = str(order.id)
        print(f"  ✅ Order placed: id={test_order_id}")
        print(f"     symbol={order.symbol}  qty={order.qty}  limit=$0.01")
        print(f"     status={order.status.value}")
    except Exception as e:
        print(f"  ❌ submit_order failed: {e}")
        print("     (This is OK if market is closed or account is restricted)")

    # ── 8. Cancel test order ──────────────────────────────────────────────
    _hdr("8. Cancel Test Order")

    if test_order_id:
        try:
            import uuid
            client.cancel_order_by_id(uuid.UUID(test_order_id))
            print(f"  ✅ Order {test_order_id[:8]}... cancelled")
        except Exception as e:
            print(f"  ❌ cancel_order_by_id failed: {e}")
    else:
        print("  (skipped — no test order to cancel)")

    # ── 9. AlpacaBroker adapter test ──────────────────────────────────────
    _hdr("9. AlpacaBroker Adapter Smoke Test")

    try:
        from brokers.alpaca.broker import AlpacaBroker

        config = {
            "market": "sp500",
            "trading": {
                "broker": "alpaca",
                "live_enabled": True,
            },
            "alpaca": {
                "paper": paper,
                "data_feed": "iex",
            },
        }

        broker = AlpacaBroker(config, live=not paper)
        connected = broker.connect()

        if connected:
            print(f"  ✅ AlpacaBroker connected: {broker!r}")

            account_info = broker.get_account_info()
            print(f"  ✅ AccountInfo: equity=${account_info.equity:,.2f} "
                  f"cash=${account_info.cash:,.2f}")

            positions = broker.get_positions()
            print(f"  ✅ get_positions: {len(positions)} position(s)")

            open_orders = broker.get_open_orders()
            print(f"  ✅ get_open_orders: {len(open_orders)} order(s)")

            broker.disconnect()
            print("  ✅ Disconnected cleanly")
        else:
            print("  ❌ AlpacaBroker.connect() returned False")
    except Exception as e:
        print(f"  ❌ AlpacaBroker adapter error: {e}")
        import traceback; traceback.print_exc()

    # ── Summary ──────────────────────────────────────────────────────────
    print()
    print("=" * 55)
    print(f"  Alpaca connectivity test complete  [{mode}]")
    print("=" * 55)
    print()


if __name__ == "__main__":
    main()
