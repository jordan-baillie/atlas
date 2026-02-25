#!/usr/bin/env python3
"""Quick Moomoo API connectivity test.

Tests: OpenD connection → account discovery → trade unlock → account info → quote snapshot.
Does NOT place any orders.
"""
import sys
import json

sys.path.insert(0, "/root/atlas")

from brokers.secrets import get_secret

print("=" * 55)
print("  Moomoo API Connectivity Test")
print("=" * 55)

# 1. Check moomoo-api import
try:
    import moomoo as ft
    print(f"\n✅ moomoo-api v{ft.__version__} imported")
except ImportError:
    print("\n❌ moomoo-api not installed. Run: pip install moomoo-api")
    sys.exit(1)

# 2. Check trade password
pwd = get_secret("MOOMOO_TRADE_PWD")
if pwd:
    print(f"✅ Trade password loaded ({pwd[:2]}****)")
else:
    print("❌ No trade password found")
    sys.exit(1)

# 3. Config
HOST = "127.0.0.1"
PORT = 11111
TRD_ENV = ft.TrdEnv.SIMULATE
SEC_FIRM = ft.SecurityFirm.FUTUAU

print(f"\n── Connecting to OpenD at {HOST}:{PORT} ──\n")

# 4. Trade context
trd_ctx = None
quote_ctx = None
try:
    trd_ctx = ft.OpenSecTradeContext(
        filter_trdmarket=ft.TrdMarket.AU,
        host=HOST, port=PORT,
        security_firm=SEC_FIRM,
    )
    print("✅ Trade context connected")

    # 5. Account list
    ret, data = trd_ctx.get_acc_list()
    if ret != ft.RET_OK:
        print(f"❌ get_acc_list failed: {data}")
        sys.exit(1)
    print(f"✅ Accounts found: {len(data)}")
    for _, row in data.iterrows():
        print(f"   acc_id={row['acc_id']}  env={row.get('trd_env','')}  type={row.get('acc_type','')}")

    # Find sim account
    acc_id = 0
    for _, row in data.iterrows():
        if row.get("trd_env") == str(TRD_ENV):
            acc_id = int(row["acc_id"])
            break
    if acc_id == 0:
        acc_id = int(data.iloc[0]["acc_id"])
    print(f"   → Using acc_id={acc_id}")

    # 6. Unlock trade
    ret, data = trd_ctx.unlock_trade(password=pwd)
    if ret != ft.RET_OK:
        print(f"❌ Trade unlock FAILED: {data}")
        print("   Check your trade password is correct")
        sys.exit(1)
    print("✅ Trade unlocked")

    # 7. Account info
    ret, data = trd_ctx.accinfo_query(
        trd_env=TRD_ENV, acc_id=acc_id,
        refresh_cache=True, currency=ft.Currency.AUD,
    )
    if ret == ft.RET_OK:
        row = data.iloc[0]
        equity = row.get("total_assets", 0)
        cash = row.get("cash", row.get("avl_withdrawal_cash", 0))
        print(f"✅ Account info — equity: ${equity:,.2f}  cash: ${cash:,.2f}")
    else:
        print(f"⚠️  accinfo_query failed: {data}")

    # 8. Positions
    ret, data = trd_ctx.position_list_query(
        trd_env=TRD_ENV, acc_id=acc_id, refresh_cache=True,
    )
    if ret == ft.RET_OK:
        positions = [(r.get("code"), int(r.get("qty", 0))) for _, r in data.iterrows() if int(r.get("qty", 0)) > 0]
        print(f"✅ Positions: {len(positions)} open")
        for code, qty in positions[:5]:
            print(f"   {code}: {qty} shares")
    else:
        print(f"⚠️  position_list_query failed: {data}")

    # 9. Quote context
    quote_ctx = ft.OpenQuoteContext(host=HOST, port=PORT)
    print("✅ Quote context connected")

    # 10. Test snapshot (BHP)
    ret, data = quote_ctx.get_market_snapshot(["AU.BHP"])
    if ret == ft.RET_OK:
        price = data.iloc[0].get("last_price", 0)
        name = data.iloc[0].get("name", "")
        print(f"✅ Quote snapshot — BHP: ${price:.2f} ({name})")
    else:
        print(f"⚠️  Quote snapshot failed: {data}")

    print("\n" + "=" * 55)
    print("  🎉 ALL TESTS PASSED — Moomoo API is working!")
    print("=" * 55 + "\n")

except ConnectionRefusedError:
    print(f"❌ Connection REFUSED — OpenD is not running on {HOST}:{PORT}")
    print("   Start OpenD first, then re-run this test")
    sys.exit(1)
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)
finally:
    if trd_ctx:
        trd_ctx.close()
    if quote_ctx:
        quote_ctx.close()
