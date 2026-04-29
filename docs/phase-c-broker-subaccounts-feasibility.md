# Phase C.2 — Per-Market Broker Sub-Accounts: Feasibility Report

**Date**: 2026-04-29  
**Status**: RESEARCH ONLY — no code or broker setup changes  
**Owner**: Engineering Lead

---

## TL;DR

Alpaca's Trading API (our current setup) offers **no sub-account or account-hierarchy support** — each API key controls exactly one brokerage account, full stop. The Broker API supports multi-account structures but is designed for fintech platforms managing *external customers*, not single-trader multi-strategy use; it requires a 2-4 week due-diligence review with Alpaca Securities LLC. The practical path forward for the next 6+ months is **Option C: stay on the single account with virtual-ledger improvements**, with three targeted fixes to close the remaining isolation gaps. The primary blocker — per-market drawdown protection — is 80% addressable within the existing codebase without any broker-level changes.

---

## 1. Alpaca Account Architecture (Today)

Atlas currently uses a **single Alpaca live Trading API account**:

```
Account ID:     4065d840-b64e-4bd7-a73f-0f278b4905bb
Account Number: 193377562
Status:         ACTIVE
Equity:         $5,185.35   (2026-04-29)
Buying Power:   $6,003.22   (2× margin, shorting enabled)
Base URL:       https://api.alpaca.markets
```

All three Atlas markets (sp500, commodity_etfs, sector_etfs) route through **one API key pair** stored in `~/.atlas-secrets.json` (`ALPACA_API_KEY` / `ALPACA_SECRET_KEY`). The broker client is instantiated in `brokers/alpaca/broker.py → AlpacaBroker` and does not receive a market-ID parameter; market isolation is enforced purely in Atlas application code.

**Confirmed by live API probe:**
```bash
GET /v2/account         → HTTP 200 (single account object, no sub-account fields)
GET /v2/accounts        → HTTP 404 (endpoint does not exist in Trading API)
GET /v2/account/configurations  → HTTP 200 (per-account preferences only)
```

The account response object contains no fields related to sub-accounts, hierarchy, or market partitioning: `id`, `account_number`, `status`, `equity`, `buying_power`, `cash`, `margin_*`, etc.

---

## 2. Sub-Account Options Surveyed

### Option A: Multiple Trading API Accounts (Separate Users)

**Operational model**: Create 3 separate Alpaca individual brokerage accounts (separate email logins, separate API key pairs, separate equity pools). Each market routes to its own account. Atlas registry would need N broker instances with per-market key config.

| Dimension | Assessment |
|-----------|-----------|
| Provisioning | Manual sign-up per account (alpaca.markets/create-account). No programmatic provisioning via API. Requires separate email, separate identity verification. |
| Lead time | Minutes to days per account (KYC is mostly automated for US residents). Practically 1-3 business days to be operational. |
| Minimum equity | No stated minimum for a standard margin account, but margin trading (Reg-T) requires ≥$2,000 per account. For meaningful position sizing, ≥$5,000 per account is practical. With 3 accounts: **≥$15,000 total capital required**. |
| Fees | Commission-free. Regulatory pass-through fees (FINRA REG + SEC TAF) on sells (~$0.01-$0.05 per order). No per-account maintenance fees. |
| Margin pooling | **None** — each account is fully independent. Leverage headroom in one account has zero effect on another. |
| API changes | Requires per-market broker config keys; `brokers/registry.py _make_alpaca_broker()` would need a `market_id → (api_key, api_secret)` lookup table. Moderate change. |
| Pros | True isolation: drawdown, HWM, positions, buying-power all per-account. No virtual-ledger hacks needed. |
| Cons | Capital inefficient: $15k locked vs $5k today. Manual provisioning. No aggregate view without building it. PDT threshold ($25k) applied per-account, not pooled. Tax reporting: 3 separate 1099s. |

**Verdict**: Viable but requires tripling capital. Not appropriate at current AUM ($5k).

---

### Option B: Alpaca Broker API (Multi-End-User Program)

**Operational model**: Register as a "Broker" under Alpaca's Broker API program. Alpaca Broker API is designed for three architectures:

| Architecture | Description |
|-------------|-------------|
| **Fully-Disclosed** | Each "customer" gets a real individual brokerage account. Alpaca handles KYC/AML. You manage UX. |
| **Omnibus** | One master account; you maintain per-customer sub-ledgers yourself. Atlas would be the omnibus operator. |
| **OmniSub** | Alpaca's API-first sub-accounting ledger with real-time per-"customer" position and cash visibility. |

Alpaca Broker API URL: `https://broker-api.sandbox.alpaca.markets/v1/accounts` (returns HTTP 401 without Broker API credentials; endpoint structure confirmed). Key endpoints would be `POST /v1/accounts` (provision sub-account), `GET /v1/accounts/{account_id}/positions`, `GET /v1/accounts/{account_id}/orders`.

| Dimension | Assessment |
|-----------|-----------|
| Eligibility | Intended for fintech apps, RIAs, broker-dealers offering trading to *external customers*. A single trader running 3 internal strategies is **not the intended use case**. Approval is at Alpaca's discretion. |
| Approval process | Due-diligence review by Alpaca Securities LLC. Not self-service. **UNCONFIRMED — needs Alpaca support reply** on whether single-trader use is permitted. |
| Lead time | Typically 2-4 weeks for approval. |
| Fees | Base commission-free; Broker API program may have volume minimums or per-account fees. **UNCONFIRMED — needs Alpaca support reply**. |
| Margin per sub-account | Each Broker API sub-account has **independent margin** (not pooled). Omnibus mode can carry positions long/short independently by sub-entity. |
| Capital requirement | Unknown for self-directed multi-strategy use. For traditional Broker API customers, $0 minimum per sub-account (Alpaca handles the capital). **UNCONFIRMED** for our use case. |
| API keys | Under Fully-Disclosed model, each sub-account gets its own API key pair. Under Omnibus/OmniSub, master key with `account_id` parameter per-call. |
| Aggregate view | Yes — master account can query all sub-accounts for positions/equity/buying-power. |
| Pros | True isolation, aggregate view, single master key or per-account keys, built for programmatic multi-account management. |
| Cons | Approval overhead, likely not intended for single-user use, 2-4 week lead time minimum, regulatory scrutiny (FINRA/SEC broker requirements), likely needs business entity / compliance infrastructure. |

**Verdict**: Architecturally ideal but almost certainly over-engineered and ineligible for a single-trader use case. Broker API is a compliance-heavy program for actual brokerage businesses.

---

### Option C: Stay on Single Account + Virtual Ledger (Current Code Path)

**Operational model**: Continue using one Alpaca account. Per-market isolation is enforced in Atlas application code via: per-market state files (`brokers/state/live_{market}.json`), `universe.membership` for position attribution, `portfolio.market_equity_attribution` for equity pro-rata distribution, and `risk.cross_universe_guard` for global entry gates.

| Dimension | Assessment |
|-----------|-----------|
| Current state | ~90% in place. Cross-universe guard exists (currently disabled pending position attrition to ≤7). Market equity attribution in place (EOD reporting). Per-market state files partition position metadata. |
| Lead time | **0** — no provisioning needed. Remaining gaps (see §9) are code changes only, estimated 1-3 days of engineering. |
| Capital required | No additional capital. Single $5k account. |
| Fees | None additional. |
| Margin isolation | **Not isolated** — all markets share the same Alpaca margin pool. Sector_etfs using 2× leverage reduces sp500 buying power in real-time. Cross-universe guard partially addresses this via buying-power gate. |
| Drawdown isolation | **Currently broken** — all 3 markets use the same `broker_equity()` value for `check_daily_drawdown`. HWM is identical across all markets ($5,189.06). A total-account 2% drop halts all 3 markets simultaneously. Fix: use `market_equity_attribution` to compute per-market HWM. |
| Position count isolation | Cross-universe guard uses a single global cap (8 positions across all markets). This prevents over-allocation but doesn't give each market an independent budget. |
| Pros | No capital requirements, no approval, already 90% implemented, zero provisioning overhead. |
| Cons | True financial isolation impossible (one margin pool). Drawdown halt propagates across all markets. Position size anomalies when one market depletes cash (see §9). |

**Verdict**: Recommended path for the next 6+ months. The remaining gaps are fixable with targeted code changes, not requiring any broker infrastructure changes.

---

## 3. Cross-Account Margin Behavior

**Under all current Atlas markets (single account)**: There is ONE margin pool. Reg-T margin is calculated on aggregate positions across all markets:

- Current account: `initial_margin = $2,183.74`, `maintenance_margin = $1,310.24`, `long_market_value = $4,367.48`
- `effective_buying_power = $6,003.22` (2× margin on equity)
- When sector_etfs buys XLI (9 × $173 = $1,557), that capital comes from the shared pool. sp500 has $1,557 less buying power.

**Under Option A (separate accounts)**: Each account has its own margin calculation. GLD position in commodity_etfs account has no effect on sp500 account's buying power. Full isolation. Requires ~3× capital.

**Under Option B (Broker API Fully-Disclosed)**: Each sub-account has independent margin — same isolation as Option A, but provisioned programmatically. Omnibus mode gives the *master* account a consolidated margin view but per-sub-entity positions are tracked separately.

**Key finding**: True margin isolation requires either Option A or B. The current virtual-ledger approach cannot prevent cross-market margin contamination; it can only detect it after the fact (via cross_universe_guard buying-power check at entry time).

---

## 4. API Isolation

**Today (single account)**:  
- One API key pair controls all markets.
- No `account_id` parameter on Trading API calls.
- Market tagging in Atlas is via `client_order_id` prefix (`atlas_rca1a_xly_...`, `atlas_sync_trail_...`).
- Orders have `subtag` and `source` fields in responses, but these are **undocumented internal Alpaca fields** — not settable via the Trading API SDK, not filterable via API queries, and should not be relied upon.

**Under Option A**:  
- 3 API key pairs. Atlas registry would need a `market_id → (ALPACA_API_KEY, ALPACA_SECRET_KEY)` map in `~/.atlas-secrets.json`.
- `_make_alpaca_broker()` in `brokers/registry.py` would select keys by market_id.
- Change complexity: moderate (config + registry + broker connection logic).

**Under Option B (Broker API)**:  
- Either one master API key with `account_id` per-call, or per-sub-account keys.
- The alpaca-py SDK has a `BrokerClient` separate from `TradingClient` — would require parallel implementation.
- Change complexity: high (new SDK client, new endpoints, new response schemas).

**Confirmed**: Alpaca's `subtag` field in order responses (observed value `None` across 10 sampled orders) is NOT documented in the Trading API v2 reference and is NOT setable via `MarketOrderRequest`, `LimitOrderRequest`, or any other request class in the current alpaca-py SDK. It should be treated as an internal Alpaca field only.

---

## 5. Position Aggregation

**Under single account (today)**: Alpaca provides one consolidated view — `GET /v2/positions` returns all positions from all markets in one flat list. Atlas must derive market ownership via `universe.membership.derive_universe()`. This works but has an edge: tickers held by multiple markets simultaneously (e.g. FCX appeared in both sp500 and commodity_etfs state on 2026-04-22) would appear as one Alpaca position with ambiguous market attribution.

**Under Option A (separate accounts)**: Each account's `GET /v2/positions` returns only that market's positions. No ambiguity. No aggregate view without building a roll-up layer.

**Under Option B (Broker API)**: Master account can query all sub-accounts' positions. Aggregate equity/positions roll-up available at the broker level — no custom roll-up code needed.

**Under virtual ledger (Option C improvement)**: `market_equity_attribution.py` already does pro-rata equity distribution at EOD. The `market_equity_history` table has data:
```
sp500         2026-04-29   $971.01   pos_mv=$817.87
commodity_etfs 2026-04-29  $1,001.81  pos_mv=$843.82  
sector_etfs   2026-04-29   $3,216.13  pos_mv=$2,708.92
```
Sum = $5,188.95 ≈ $5,185.35 actual (within $3.60 rounding). This is the correct roll-up. The `starting_equity` values in configs ($5,011 / $5,000 / $5,000 = $15,011 total) are historical fiction — they represent original deposits, not current allocations.

---

## 6. Cost Analysis

| Option | Capital Required | Approval | Lead Time | Ops Overhead |
|--------|-----------------|----------|-----------|--------------|
| A (3 separate accounts) | ≥$15,000 | None (self-service) | 1-3 business days | 3× secrets, 3× reconcile scripts, 3 separate 1099s |
| B (Broker API) | Unknown (probably $0 per sub-acct) | Alpaca review (2-4 wks) | 2-6 weeks | New BrokerClient SDK, new route handlers, compliance overhead |
| C (virtual ledger) | $0 additional | None | 0 | 1-3 days engineering for remaining gaps |

**Tax note for Option A**: Three separate brokerage accounts mean three separate IRS Form 1099-B filings per tax year. Combined P&L tracking would require custom aggregation. Atlas currently produces a single P&L view — this would need per-account segmentation.

---

## 7. Recommendation

**Pursue Option C (single account + virtual ledger) for the next 6+ months.** The three remaining isolation gaps are engineering problems, not broker infrastructure problems:

1. **Per-market drawdown HWM** (Priority: High, Est. 0.5 days): `check_daily_drawdown()` in `live_portfolio.py` should read `market_equity_history` for the market's allocated equity instead of `broker_equity()`. This decouples halt triggers across markets.

2. **`starting_equity` recalibration** (Priority: Medium, Est. 0.5 days): Update sp500/commodity_etfs/sector_etfs configs to use their `market_equity_history.allocated_equity` values (~$971 / $1,002 / $3,216) as `starting_equity`. The current $5,000-$5,011 values create a phantom 3× overclaim that inflates per-market equity calculations.

3. **Cross-universe guard activation** (Priority: High, gate already built, Est. 0 days): Enable `cross_universe_guard.enabled=true` in `config/global_risk.json` once total open positions ≤7. Already built in `risk/cross_universe_guard.py`.

**Gate for re-evaluation**: Reassess Option A when total AUM reaches **≥$25,000** (PDT threshold removes a key operational constraint) and each market can be seeded with ≥$5,000 independently. That is the natural point at which true account isolation becomes worth the capital cost and operational overhead.

**Option B (Broker API) should not be pursued** without explicit confirmation from Alpaca support that single-trader multi-strategy use is permitted. It is a compliance-heavy program designed for fintech companies, not individual algorithmic traders.

---

## 8. Open Questions for Alpaca Support

Send to `support@alpaca.markets` if Option A or B is pursued:

1. **Broker API eligibility**: "We are a single individual trader running 3 internal strategies. Does Alpaca Broker API support self-directed multi-account use (i.e., each 'customer' account is our own strategy, not an external customer's account)? What are the eligibility requirements and approval criteria for this use case?"

2. **Broker API fees and capital requirements**: "Under Alpaca Broker API (Fully-Disclosed or OmniSub model), is there a minimum equity per sub-account, a minimum total AUM for the program, or any per-account monthly fees? What regulatory filings or compliance infrastructure would a single-trader program require?"

3. **Multiple Trading API accounts**: "Can a single individual open multiple separate Trading API accounts (one per strategy/market) using the same identity? Are there any restrictions on the number of individual brokerage accounts, or PDT considerations across accounts (e.g. does the $25k threshold apply per account or aggregated)?"

4. **`subtag` field**: "The Alpaca Trading API order response includes a `subtag` field (observed as `null` in all our orders). Is this field settable via order submission requests? If so, can orders be queried/filtered by subtag value? This would enable per-strategy tagging of orders without requiring separate accounts."

---

## 9. Pre-Existing Virtual-Ledger Gap Analysis

The current per-market state-file approach does NOT solve the following:

### Gap 1: Shared Drawdown HWM (Severity: HIGH, affects live trading)
All 3 markets have identical `daily_high_water = $5,189.06` stored in their state files. This is the raw `broker_equity()` value. When `check_daily_drawdown()` runs for any market, it compares against the TOTAL account equity — not the market's allocated share. **Result**: A 2% drop in total account equity (≈$104) halts all 3 markets simultaneously, even if one market is profitable and the drawdown comes entirely from another. The RCA #4D fix (`market_equity_attribution.py`) built the right data; it just hasn't been wired into `check_daily_drawdown`.

### Gap 2: Starting Equity Fiction (Severity: MEDIUM, affects position sizing)
Each market's `starting_equity` in config ($5,011 / $5,000 / $5,000 = $15,011 total) was set at account creation and never recalibrated. The real per-market allocated equity as of 2026-04-29 is:
- sp500: $971 (was claiming $5,011 → 5.2× overclaim)
- commodity_etfs: $1,002 (was claiming $5,000 → 5.0× overclaim)
- sector_etfs: $3,216 (was claiming $5,000 → 1.6× overclaim)

The `equity()` method (`starting_equity - deployed + pnl`) returns values like $5,287 for sp500, which do not reflect that sp500 has only $971 of the real account. This inflates risk calculations, position sizes, and drawdown percentages. **This is RCA latent #6 — the $24.45 distribution delta is a symptom of this overclaim.**

### Gap 3: Margin Cross-Contamination (Severity: MEDIUM, structural)
sp500 is configured with `leverage=2.0`, commodity_etfs and sector_etfs at `leverage=1.0`. But all three markets draw from the same Alpaca margin pool. If sector_etfs places a large long position, it consumes buying power that sp500 intended to use. The cross_universe_guard's `require_positive_cash` check mitigates over-deployment but does not enforce per-market capital budgets.

### Gap 4: Cross-Market Ticker Contamination (Severity: MEDIUM, operational history)
FCX appeared in both sp500 and commodity_etfs state files simultaneously (2026-04-22 incident). Since Alpaca sees only one position for FCX, both markets tried to manage protective orders for it. Fixed by adding `state_tickers` scoping to `sync_protective_orders.py`, but the root cause (shared ticker namespace) remains — any ticker appearing in multiple universes will route to one Alpaca position.

### Gap 5: No Per-Market Stop-Loss Coverage Visibility (Severity: LOW)
`healthz_tp_coverage.py` and related scripts check coverage across all open positions. There is no per-market stop/TP coverage report — it is impossible to know "commodity_etfs has 100% stop coverage, sector_etfs has 66%" without custom filtering. Under true sub-accounts, this would be trivially isolated.

### Gap 6: PDT Counter Shared (Severity: LOW, currently)
Alpaca's `daytrade_count` is account-wide. If sector_etfs executes a day-trade, it counts toward the same PDT tally that sp500 uses. With current account size ($5,185 < $25k), a market PDT-triggered halt could propagate to all markets via `_is_pdt_retry_window()` in `sync_protective_orders.py`.

---

## References

- Alpaca Trading API v2 reference: https://docs.alpaca.markets/reference/getallorders-1
- Alpaca account object schema: https://docs.alpaca.markets/reference/getaccount-1  
- Alpaca Broker API overview: https://docs.alpaca.markets/docs/about-broker-api
- Alpaca Broker API accounts: https://docs.alpaca.markets/reference/createaccount-1
- Alpaca `BrokerClient` in alpaca-py SDK: https://github.com/alpacahq/alpaca-py/blob/master/alpaca/broker/client.py
- Atlas `market_equity_attribution.py`: `portfolio/market_equity_attribution.py`
- Atlas `cross_universe_guard.py`: `risk/cross_universe_guard.py`
- Atlas virtual-ledger equity method: `brokers/live_portfolio.py → equity()` (~line 714)
- Atlas drawdown check: `brokers/live_portfolio.py → check_daily_drawdown()` (~line 799)
- Atlas per-market state files: `brokers/state/live_{market}.json`
- Atlas global risk config: `config/global_risk.json`
