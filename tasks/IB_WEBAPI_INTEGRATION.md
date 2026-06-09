# IB Web REST API — integration map (for the IB micro-futures adapter)

The operator supplied the **IB Web API** OpenAPI spec (`api.ibkr.com`, v2.34). This is the **headless REST**
path to IB — far better for our autonomous VPS than the `ib_insync` transport (which needs a running TWS/IB
Gateway **GUI**). Decision: build the live IB adapter against the **Web API**; keep the ib_insync `IBBroker`
as a fallback only. Both implement the same `brokers/base.py` `BrokerAdapter`, so `target_executor` is unchanged.

## Two retail-viable auth modes (pick one)
1. **Client Portal Gateway (recommended for retail).** Run IBKR's small headless Java gateway; log in via browser
   once; then call the SAME endpoint paths at `https://localhost:5000/v1/api/...` with **no OAuth signing**. Keep
   alive with `POST /tickle` (~every 60s). Lightest path; no OAuth registration needed.
2. **OAuth (institutional/registered).** `POST /oauth2/api/v1/token` (or OAuth 1.0a: request→access→live_session
   token) → Bearer; then `POST /iserver/auth/ssodh/init` to start the brokerage session. Needs a registered consumer.

## Session lifecycle (both modes)
`POST /iserver/auth/ssodh/init` (start brokerage session) → `POST /iserver/auth/status` (verify `authenticated`)
→ `POST /tickle` every ~60s to keep alive → `GET /iserver/accounts` (MUST call before any order) →
`GET /portfolio/accounts` (MUST call before any /portfolio endpoint).

## Endpoint → BrokerAdapter method map
| BrokerAdapter | IB Web API |
|---|---|
| `connect()` | ssodh/init → auth/status → /iserver/accounts → /portfolio/accounts; start /tickle keepalive |
| **contract resolution** (futures→conid) | `GET /trsrv/futures?symbols=MES,MNQ&exchange=CME` (non-expired front month → `conid`) |
| `get_account_info()` | `GET /iserver/account/{acct}/summary/balances` (or `/portfolio/{acct}/ledger` → `netliquidationvalue`) |
| `get_positions()` | `GET /portfolio2/{acct}/positions` (near-real-time, signed `position`, `avgCost`) |
| `get_prices()` | `GET /iserver/marketdata/snapshot?conids=&fields=31` (31=last, 84=bid, 86=ask) — pre-flight may return empty |
| `place_order()` | `POST /iserver/account/{acct}/orders` body `{conid, orderType, side, quantity, tif, price}` |
| `cancel_order()` | `DELETE /iserver/account/{acct}/order/{orderId}` |
| `get_open_orders()` | `GET /iserver/account/orders` |
| `get_order_status()` | `GET /iserver/account/order/status/{orderId}` |

## Two gotchas that change the adapter vs ib_insync
1. **Order-reply confirmation.** `POST .../orders` frequently returns an array of *order reply messages* (warnings,
   each with a `replyId` UUID + `messageIds`). The order is NOT placed until you `POST /iserver/reply/{replyId}`
   with `{"confirmed": true}` (may chain several). Alternatively pre-suppress known warnings via
   `POST /iserver/questions/suppress` with the `messageIds` (e.g. `o354` no-market-data, `o163` price-pct,
   `o403` immediate-fill) once per session. The adapter must loop confirm-or-suppress until a real `order_id` returns.
2. **Long-short + sizing.** `side` is `BUY`/`SELL` (shorts supported). Futures are sized in integer contracts by
   `conid`; the MICRO_FUTURES multiplier table (MES=5, MNQ=2, MGC=10, …) is used by `target_executor` for sizing —
   the Web API itself just takes contract `quantity`.

## Build
`brokers/ib_web/broker.py` → `IBWebBroker(BrokerAdapter)` over a thin HTTP client (base URL configurable:
localhost CP-Gateway vs api.ibkr.com OAuth). Inject the HTTP layer so the translation + reply-confirm loop are
unit-testable with a fake (no live gateway), like the ib_insync `IBBroker` test. Register `"ib_web"` in the
broker registry. Live connection lands when the gateway/creds are set up (for BOREAS, ~2026-08-28).

`paper` vs `live` is the IBKR account you log the gateway into (paper user → paper account), not a port.
