# Wave A — Stream A8: Insufficient Buying Power Investigation
**Date**: 2026-04-28  
**Status**: Investigation only — no code changes.

---

## TL;DR

The 24 "Insufficient buying power" rejections split cleanly into **three epochs with two distinct root causes**. The dominant cause (Apr 15–23, ~22 of 24 hits) is a **config bug, not a capital shortage**: `sector_etfs` ran with `live_enabled=False`, so `LivePortfolio` never connected to Alpaca and left `cash=$0`, `buying_power=$0`; every signal was immediately killed. A secondary config bug fired on Apr 24 (`alpaca.paper=true` on live keys → HTTP 401). Only the Apr 27 pair are genuine buying-power events: the single shared Alpaca account was at 174% gross exposure ($9,491 MV vs $5,428 equity) and the remaining RegT margin capacity ($1,342) was $28 short of the UNG order cost ($1,371). The proposed "1.75× equity cap lift" is not reflected in any codebase artefact found — if it was applied, it addressed the wrong layer. The fixes needed are: (1) confirm `sector_etfs.live_enabled=True` + `alpaca.paper=false` are stable going forward (both now correct in v1.0.2), and (2) implement cross-universe position accounting so plan-time buying-power checks reflect the full shared account deployment, not just the calling universe's own positions.

---

## Findings

### 1. Per-day rejection counts

All rejections occur in `sector_etfs` plans except the two Apr 27 UNG/XLK entries which appear in the **sp500 regime-aware plan** (because `generate_regime_plan()` evaluates all active universes).

| Date | sector_etfs | commodity_etfs | sp500 plan (cross-universe) | root cause |
|------|-------------|----------------|------------------------------|-----------|
| 2026-04-15 | **6** (XLY, XLK, XLC, XLRE, XLI, XLF) | 1 (DBB — max positions) | 0 | `live_enabled=False` → cash=$0 |
| 2026-04-17 | **3** (XLK, XLC, XLRE) | 0 | 0 | `live_enabled=False` → cash=$0 |
| 2026-04-20 | **6** (XLK, XLY, XLRE, XLC, XLF, XLI) | 3 (USO, DBA, CCJ — max positions) | 0 | `live_enabled=False` → cash=$0 |
| 2026-04-21 | **5** (XLK, XLRE, XLF, XLI, XLB) | 0 | 0 | `live_enabled=False` → cash=$0 |
| 2026-04-22 | **1** (XLK) | 1 (DBB — max positions) | 0 | `live_enabled=False` → cash=$0 |
| 2026-04-23 | **1** (XLK) | 0 | 0 | `live_enabled=False` → cash=$0 |
| 2026-04-24 | **1** (XLI; also risk excess) | 1 (DBA — max positions) | 0 | `alpaca.paper=true` on live keys → 401 → cash=$0 |
| 2026-04-27 | 0 (no new signals) | 0 (no new signals) | **2** (UNG $1,371 > $1,343 BP; XLK also risk excess) | Genuine: account at 174% exposure |
| **TOTAL** | **23** | 0 buying-power | **2** | — |

> Note: commodity_etfs buying-power rejections = **0**. All commodity_etfs entries in this table are rejected for "Max positions (5) would be exceeded", not buying power.

---

### 2. Buying-power timeline

`cash` and `buying_power` from plan snapshots (`plans.plan_data → portfolio_snapshot`). For `sector_etfs`, `cash=0.0` is the disconnected-portfolio sentinel value, not the broker's real cash. For sp500/commodity_etfs, cash = Alpaca cash (same shared account). `Alpaca BP` (last column) is the real `buying_power` field from the broker where directly observable.

| Date | sector_etfs cash | commodity_etfs cash | sp500/shared cash | Gross exposure (est.) |
|------|-----------------|---------------------|-------------------|-----------------------|
| 2026-04-15 | **$0.00** ❌ disconnected | $2,082 | $2,082 | ~60% |
| 2026-04-17 | **$0.00** ❌ disconnected | $802 | $802 | ~85% |
| 2026-04-20 | **$0.00** ❌ disconnected | $520 | $520 | ~90% |
| 2026-04-21 | **$0.00** ❌ disconnected | $1,318 | $1,318 | ~75% |
| 2026-04-22 | **$0.00** ❌ disconnected | $1,158 | $1,158 | ~78% |
| 2026-04-23 | **$0.00** ❌ disconnected | $1,158 | $1,158 | ~78% |
| 2026-04-24 | **$0.00** ❌ 401 auth failure | $99 | $99 | ~98% |
| 2026-04-27 | -$4,062 (margin) ✅ connected | -$4,062 (margin) | -$4,062 | ~174% ⚠️ |

**Apr 27 buying-power detail** (from sp500 plan rejected_entries JSON):
- `self.buying_power = $1,342.88` (Alpaca RegT remaining margin capacity)
- UNG needed $1,371.23 → over by $28.35 → rejected
- XLK needed $2,082.86 (also risk-excess at $61.91 > $53.05 max) → rejected

> `portfolio_snapshots.jsonl` note: that file stores `buying_power = cash` (a copy-field bug). On RegT margin accounts the real Alpaca `buying_power` field is different (positive even when cash is negative). This is a separate pre-existing logging issue; it does not affect live execution.

---

### 3. Sample rejection log lines

```
# Apr 15 — sector_etfs, live_enabled=False
[2026-04-15 19:01:28] WARNING atlas.live_portfolio: LivePortfolio: no broker configured
  for sector_etfs (live_enabled=False)
[2026-04-15 19:01:28] WARNING atlas.cli: Broker connect failed for sector_etfs —
  returning disconnected LivePortfolio
  XLY (momentum_breakout): Insufficient buying power: need $815.08, have $0.00
  XLK (momentum_breakout): Insufficient buying power: need $739.70, have $0.00
  XLC (momentum_breakout): Insufficient buying power: need $1049.04, have $0.00
  XLRE (momentum_breakout): Insufficient buying power: need $1129.18, have $0.00
  XLI (momentum_breakout): Insufficient buying power: need $866.75, have $0.00
  XLF (momentum_breakout): Insufficient buying power: need $932.04, have $0.00

# Apr 22 — sector_etfs, still live_enabled=False (confirmed by logs/plan-sector_etfs-20260422.log)
[2026-04-22 19:02:40] WARNING atlas.live_portfolio: LivePortfolio: no broker configured
  for sector_etfs (live_enabled=False)
  XLK (momentum_breakout): Insufficient buying power: need $773.45, have $0.00

# Apr 24 — sector_etfs, live_enabled=True but alpaca.paper=true (HTTP 401)
[pi-cron-premarket-20260424] Alpaca broker auth failed — APIError: request is not
  authorized (40110000) — plan fell back to disconnected LivePortfolio with cash=$0
  XLI: Risk $29.05 > max $25.00; Insufficient buying power ($0 cash)

# Apr 27 — sp500 regime-aware plan, genuine $28 shortfall
plan_data.rejected_entries for market_id=sp500 (date=2026-04-27):
  {"ticker": "UNG", "strategy": "connors_rsi2", "position_value": 1371.23,
   "rejection_reason": "Insufficient buying power: need $1371.23, have $1342.88"}
  {"ticker": "XLK", "strategy": "momentum_breakout", "position_value": 2082.86,
   "rejection_reason": "Risk $61.91 exceeds max $53.05; Insufficient buying power:
   need $2082.86, have $1342.88"}
```

---

### 4. Hypothesis test results

**H1 (sp500 timing monopolizes buying power)**: **REFUTED as primary cause.**

Cron timing: all three premarket jobs fire simultaneously at `00 19 * * 1-5` (19:00 AEST = 09:00 UTC). From DB `created_at` timestamps, sector_etfs and commodity_etfs plans consistently complete at 09:02–09:03 UTC, sp500 at 09:05–09:07 (larger universe = longer). The ETF markets finish first, not last.

For execute_approved: commodity_etfs and sp500 fire at `15 23 * * 1-5` (same time), sector_etfs at `20 23 * * 1-5` (5 min later). A mild race condition exists but has not produced documented buying-power rejections — those happen at plan time, not execution time.

Evidence: Plans DB timestamps 2026-04-15 → 2026-04-27 show sector_etfs always precedes sp500 by 3–5 minutes. This conclusively rules out H1 for plan-time rejections.

**H2 (allocation config not respected)**: **HELD — primary cause for 22 of 24 rejections.**

Two successive config bugs prevented `sector_etfs` from ever connecting to Alpaca:

- **Bug A (Apr 15–23)**: `trading.live_enabled=false` in `config/active/sector_etfs.json`.  
  `brokers/registry.get_broker()` returns `None` when `live_enabled=False` (line 81: `if not (live_enabled or monitoring_enabled)`).  
  `LivePortfolio.connect()` (line 230) warns "no broker configured" and returns `False`.  
  `self.cash` and `self.buying_power` stay at their initialised defaults of `0.0` (lines 64–65).  
  Every entry signal then hits `check_risk_limits()` line 776: `cost > 0 > 0.00` → rejection.

- **Bug B (Apr 24)**: `alpaca.paper=true` in sector_etfs config after `live_enabled` was corrected.  
  `AlpacaBroker.__init__(paper=True)` connects to `paper-api.alpaca.markets`.  
  Live credentials return HTTP 401 from the paper endpoint.  
  `broker.connect()` raises `APIError(40110000)`. `LivePortfolio.connect()` catches it, returns `False`.  
  Same downstream behaviour: cash=$0, buying_power=$0.  
  Root cause confirmed in `logs/pi-cron-postclose-20260425_080001.md` (verbatim excerpt):  
  _"config/active/sector_etfs.json had alpaca.paper: true, but the only Alpaca credentials  
  in ~/.atlas-secrets.json are live-account keys. Live keys return 401 against  
  paper-api.alpaca.markets."_

Current state (v1.0.2): `live_enabled=True`, `mode=live`, `alpaca.paper=false`. Both bugs resolved as of audit-fix-5 (2026-04-27).

**H3 (cash-scoping issue)**: **HELD for Apr 27 only — contributory cause for 2 of 24 rejections.**

All three universes share one Alpaca brokerage account. On Apr 27, combined deployment:
- sp500: 5 positions, MV ≈ $9,491 (after margin). Equity $5,428. RegT buying_power = $1,342.88.
- sector_etfs: 3 positions (XLI, XLK, XLY). Equity $5,036.
- commodity_etfs: 5 positions (CCJ, FCX, GLD, SLV, UNG). Equity $4,962.

`check_risk_limits` (line 775 `live_portfolio.py`) uses `self.buying_power` which is populated directly from `acct.buying_power` via the Alpaca API. This reflects the true total remaining capacity after all cross-universe positions — the check is globally correct, not per-universe siloed.

The scoping issue is structural: `generate_regime_plan()` in `brokers/plan.py` runs with the sp500 portfolio's buying_power ($1,342.88). It evaluates UNG (from commodity universe) and XLK (from sector universe) within that same plan. UNG at $1,371 exceeds the remaining account capacity by $28. The architecture is working as designed — the account genuinely has insufficient margin remaining.

---

### 5. Root cause hypothesis

**Causal chain — primary (22/24 rejections):**

```
1. sector_etfs.json: trading.live_enabled = false  [Apr 15–23]
   → brokers/registry.get_broker() returns None
   → LivePortfolio.connect() returns False immediately (line 230)
   → self.cash = 0.0, self.buying_power = 0.0 (Python default init, lines 64–65)
   → check_risk_limits(): cost > 0 > available_buying_power ($0)
   → "Insufficient buying power: need $X, have $0.00"
   → Plan saved with 0 proposed_entries, all signals in rejected_entries

2. sector_etfs.json: alpaca.paper = true  [Apr 24, after live_enabled fixed]
   → AlpacaBroker routes to paper-api.alpaca.markets
   → Live API key → HTTP 401 APIError(40110000)
   → LivePortfolio.connect() catches error, returns False
   → Same: cash=$0, buying_power=$0, all signals rejected
```

**Causal chain — secondary (2/24 rejections, Apr 27):**

```
3. All configs correct. 3 universes sharing one Alpaca account.
   Combined positions: ~13 open, MV ≈ $9,491, equity ≈ $5,428 (174% gross exposure).
   Alpaca RegT buying_power = 2×equity − positions_at_cost ≈ $1,343.
   UNG connors_rsi2: 133 shares × $10.31 = $1,371 → over limit by $28.
   sp500 generate_regime_plan() evaluates cross-universe signals against sp500 portfolio BP.
   Rejection is correct and real — account has no room.
```

**The "1.75× cap lift"**: Not found in any code artefact. `leverage` in both ETF configs = `1.0`. No `buying_power_multiplier`, `leverage_cap`, or `gross_exposure_limit` parameter exists in the codebase. If applied, it was conceptual/verbal only and had no effect because the signal pipeline was gated by the config bugs at an earlier layer (no broker connection → cash=$0).

---

### 6. Proposed fix options (DO NOT IMPLEMENT)

**Option A — Accept residual structural constraint, monitor exposure ceiling**  
_Description_: No code change. The two config bugs are already fixed (v1.0.2). The Apr 27 UNG rejection ($28 shortfall) is a natural consequence of running 3 universes in one Alpaca account at near-RegT capacity. Accept that signals may fail the buying-power check when gross exposure > ~160%.  
_Pros_: Zero risk. Already partially addressed.  
_Cons_: Leaves the H3 structural issue unaddressed; will recur whenever margin is nearly full.  
_Effort_: 0

**Option B — Per-universe buying-power soft partitioning**  
_Description_: New config key `risk.universe_bp_fraction` (e.g. sp500=0.5, commodity_etfs=0.3, sector_etfs=0.2). At `_refresh_from_broker`, set `self.buying_power = total_alpaca_bp × universe_bp_fraction`. Each universe has a notional capital slice.  
_Pros_: Prevents sp500 from consuming all margin before ETF plans run.  
_Cons_: Static allocation; sp500 can't borrow unused ETF headroom. Requires careful tuning. Doesn't track actual cross-universe positions deployed.  
_Effort_: Small (~50 lines + 3 config updates + tests)

**Option C — Gross-exposure hard gate in check_risk_limits** ← *Recommended*  
_Description_: Add a `MAX_GROSS_EXPOSURE` guard in `check_risk_limits()`: if `(broker_mv + proposed_cost) / broker_equity > max_gross_exposure`, reject the signal. Config key e.g. `risk.max_gross_exposure_pct: 1.75`. This is where the "1.75× cap" from the brief would take effect as real code.  
_Pros_: Directly prevents the Apr 27 pattern. Intuitive. Portfolio-level guard. Only requires `self._broker_equity` and `self.broker_mv` (already available or trivially computed). Configurable per universe.  
_Cons_: Needs a `broker_mv` property in LivePortfolio (sum of position costs or MV). If set to 1.75 and account is already at 1.74×, any new entry from any universe is blocked regardless of which universe initiated it.  
_Effort_: Medium (~80 lines + tests)

**Option D — Separate Alpaca sub-accounts per universe**  
_Description_: Dedicated Alpaca account (paper or live) per universe, each seeded with its own capital. No shared buying-power pool.  
_Pros_: Cleanest isolation. No code changes to buying-power logic.  
_Cons_: Requires Alpaca sub-account provisioning, per-market credential management, secrets file updates. High operational overhead.  
_Effort_: Large (multiple days)

**Recommended**: **Option C** (gross-exposure gate). The config bugs are already fixed. The remaining structural issue is the single-account exposure ceiling, and a per-plan gross-exposure check is the minimal, testable, production-safe way to enforce it. Option A is acceptable as a hold until Option C is implemented.

---

## Files / queries used

| File / query | Purpose |
|---|---|
| `grep -rn "Insufficient buying power" logs/` (16 log-line hits) | Enumerate all rejections |
| `sqlite3 data/atlas.db "SELECT date, market_id, MIN/MAX(created_at) FROM plans WHERE date >= '2026-04-15' GROUP BY date, market_id"` | Plan generation timing per universe |
| `sqlite3 ... json_extract(plan_data, '$.portfolio_snapshot.equity/cash')` | Per-day portfolio state from plan JSON |
| `sqlite3 ... json_extract(plan_data, '$.rejected_entries')` | Full rejection reasons and amounts |
| `logs/pi-cron-premarket-20260415_190001.log-20260417` | First rejection batch — `live_enabled=False` WARNING confirmed |
| `logs/plan-sector_etfs-20260422.log-20260424` | Confirms `live_enabled=False` still present Apr 22 |
| `logs/pi-cron-premarket-20260424_190001.log-20260426` | Apr 24: `alpaca.paper=true` → 401 |
| `logs/pi-cron-postclose-20260425_080001.md` | Root-cause diagnosis of the 401 + paper=true fix |
| `logs/pi-cron-premarket-20260427_190001.log` + `.md` | Apr 27: broker connected, 174% exposure, $1,342 remaining BP |
| `logs/portfolio_snapshots.jsonl` | Account equity/cash EOD timeline |
| `brokers/live_portfolio.py` lines 64–65, 226–284, 760–777 | LivePortfolio init, `connect()`, `check_risk_limits()` |
| `brokers/registry.py` lines 64–93 | `get_broker()` returns None when `live_enabled=False` |
| `brokers/plan.py` lines 553–640 | `_run_regime_aware_plan` — evaluates ALL active universes in sp500 plan |
| `config/active/sector_etfs.json` | Current state (v1.0.2): live_enabled=true, paper=false, mode=live |
| `config/active/commodity_etfs.json` | Current state (v1.2): live_enabled=true, paper=false |
| `crontab -l` | execute_approved timing: sp500+commodity 23:15 UTC, sector_etfs 23:20 UTC |
