# Atlas Dashboard Data Audit Report

**Audit Date:** 2026-03-03 10:30 AEST  
**Dashboard Snapshot:** `dashboard/data/dashboard-data.json` (generated 2026-03-03T10:30:07)  
**Auditor:** Builder-1 / Swarm Agent  
**Scope:** Full audit of every datapoint in dashboard-data.json against source-of-truth files  

---

## Executive Summary

The Atlas dashboard is **structurally sound** for core accounting (equity, cash, FX conversion, P&L math for SP500 positions). However, **3 significant bugs** and **8 warnings** were found, covering stale ASX prices, an inconsistent XOP manual-position block, a currency-mixing bug in the combined strategy summary, and several design-level issues.

| Severity | Count | Description |
|----------|-------|-------------|
| ❌ **Critical** | 3 | Stale ASX prices (P&L wrong), XOP position fields inconsistent, strategy MV currency mixing |
| ⚠️ **Warning** | 8 | Top-level risk shows SP500 only, ONDL trades missing from realized P&L, state file stale, SP500 benchmark curve gap, plan section misleading, sector enrichment broken, buying_power anomaly, plan partial execution gap |
| ✅ **Correct** | 34 | Equity, cash, FX conversions, SP500 P&Ls, starting equity, commissions, exposure %, equity curves, benchmarks, research stats, config versions, etc. |

---

## 1. Top-Level Metadata

| Field | Expected | Actual | Verdict |
|-------|----------|--------|---------|
| `timestamp` | Recent (today) | 2026-03-03T10:30:07+10:00 | ✅ Fresh (within last minute) |
| `trading_mode` | "live" (any market live) | "live" | ✅ Correct |
| `data_source` | "broker" (connected) | "broker" | ✅ Correct |
| `exchange_rates.AUDUSD` | Live market rate | 0.70581 | ⚠️ Unverifiable without FX feed; cross-check: 1/0.70581 = 1.41681 = USDAUD ✅ internally consistent |
| `exchange_rates.USDAUD` | Inverse of AUDUSD | 1/0.70581 = 1.41676 | ✅ Rounds to 1.41681 (consistent) |
| `config_version.asx` | "asx_ibkr_tf_only_v1.0" | "asx_ibkr_tf_only_v1.0" | ✅ Matches `config/active/asx.json` |
| `config_version.sp500` | "v2.2" | "v2.2" | ✅ Matches `config/active/sp500.json` |
| `config_version.hk` | "v1.1" | "v1.1" | ✅ Matches `config/active/hk.json` |
| `broker.asx` | "ibkr" | "ibkr" | ✅ Matches config |
| `broker.sp500` | "moomoo" | "moomoo" | ✅ Matches config |
| `broker.hk` | "moomoo" | "moomoo" | ✅ Matches config (switched from IBKR in v1.1) |

**Note on AUDUSD rate:** AUD/USD was approximately 0.62–0.65 in early 2026 based on macroeconomic context. The rate shown (0.70581) appears elevated relative to typical early-2026 levels, but cannot be verified without a live FX feed at audit time. The internal consistency between AUDUSD and USDAUD is confirmed.

---

## 2. Account Section (Combined AUD)

**Source:** `account.*` — computed by summing per-market values in AUD

| Field | Expected | Actual | Verdict |
|-------|----------|--------|---------|
| `account.equity` | ASX(3999) + SP500(4002.73 × 1.41681) = 9670.11 | 9670.11 | ✅ Correct |
| `account.cash` | ASX_cash(1747.8) + SP500_cash(3570.5 × 1.41681) = 6806.52 | 6806.52 | ✅ Correct |
| `account.buying_power` | Same as cash (no margin) | 6806.52 | ✅ Correct |
| `account.currency` | "AUD" | "AUD" | ✅ Correct |

**Math verified:**
```
ASX equity:   3999.00 AUD
SP500 equity: 4002.73 USD × 1.41681 = 5671.11 AUD
Total:        9670.11 AUD ✅

ASX cash:     1747.80 AUD
SP500 cash:   3570.50 USD × 1.41681 = 5058.72 AUD
Total cash:   6806.52 AUD ✅
```

---

## 3. Portfolio Section (Combined)

| Field | Expected | Actual | Verdict |
|-------|----------|--------|---------|
| `portfolio.equity` | 9670.11 AUD | 9670.11 | ✅ Matches account.equity |
| `portfolio.cash` | 6806.52 AUD | 6806.52 | ✅ Correct |
| `portfolio.starting_equity` | 3999 + 4000×1.41681 = 9666.24 | 9666.24 | ✅ Correct — sums ASX(3999) + SP500(4000) at current FX |
| `portfolio.total_pnl` | 9670.11 − 9666.24 = 3.87 | 3.87 | ✅ Correct |
| `portfolio.total_pnl_pct` | 3.87 / 9666.24 × 100 = 0.04% | 0.04 | ✅ Correct |
| `portfolio.num_open` | 4 ASX + 2 SP500 = 6 | 6 | ✅ Correct |
| `portfolio.market_pnl` | ASX(0.0) + SP500(2.73 × 1.41681) = 3.87 | 3.87 | ✅ Correct |
| `portfolio.realized_pnl` | 0 (no closed Atlas trades) | 0.0 | ✅ Correct for Atlas positions |
| `portfolio.total_commissions` | ASX(24.0) + SP500(2.2 × 1.41681) = 27.12 | 27.12 | ✅ Correct |
| `portfolio.win_rate` | 0 (no closed trades) | 0 | ✅ Correct |

---

## 4. Per-Market Data

### 4a. ASX Market (`markets.asx`)

| Field | Expected | Actual | Verdict |
|-------|----------|--------|---------|
| `trading_mode` | "live" (ibkr live_enabled=true) | "live" | ✅ |
| `broker` | "ibkr" | "ibkr" | ✅ |
| `config_version` | "asx_ibkr_tf_only_v1.0" | "asx_ibkr_tf_only_v1.0" | ✅ |
| `portfolio.starting_equity` | 3999 (config) | 3999 | ✅ |
| `portfolio.equity` | cash(1747.8) + positions(2251.2) = 3999.0 | 3999.0 | ✅ |
| `portfolio.cash` | 3999 − entry_values(2251.2) = 1747.8 | 1747.8 | ✅ |
| `portfolio.buying_power` | Broker-reported buying power | 1747.8 | ✅ (equals cash — correct for IBKR AU) |
| `portfolio.num_open` | 4 | 4 | ✅ |
| `portfolio.num_atlas` | 4 | 4 | ✅ |
| `portfolio.market_pnl` | Sum of position pnl = 0.0 (current=entry) | 0.0 | ⚠️ Mathematically correct but **stale** — see critical finding #1 |
| `portfolio.total_pnl` | 0.0 (equity unchanged) | 0.0 | ⚠️ Stale — same issue |
| `portfolio.commission_per_trade` | 6.0 (config.fees) | 6.0 | ✅ |
| `portfolio.total_commissions` | 4 × 6.0 = 24.0 | 24.0 | ✅ |
| `portfolio.realized_pnl` | 0 (no closed trades) | 0 | ✅ |
| `risk.max_positions` | 7 (config.risk) | 7 | ✅ |
| `risk.halted` | false | false | ✅ |
| `risk.risk_per_trade` | 0.005 | 0.005 | ✅ |
| `risk.exposure_pct` | 2251.2 / 3999 × 100 = 56.3% | 56.3 | ✅ |
| `data_freshness.broker_connected` | true | true | ✅ |
| `data_freshness.data_source` | "broker" | "broker" | ✅ |
| `data_freshness.plan_date` | "2026-03-03" | "2026-03-03" | ✅ |
| `plan.status` | APPROVED (matches plan file) | "APPROVED" | ✅ |
| `plan.trade_date` | "2026-03-03" | "2026-03-03" | ✅ |
| `benchmark_ticker` | "IOZ.AX" (config.universe) | "IOZ.AX" | ✅ |
| `benchmark_return_pct` | (3983.84 − 3999) / 3999 × 100 = −0.38% | −0.38 | ✅ |

**⚠️ ASX plan partial execution gap:**  
The plan shows 7 APPROVED entries (REH, AGL, NXT, ADH, PDN, ANZ, ORA) with `risk_summary.positions_after=7` and `cash_after_entries=57.79`. But only **4 positions** appear in the portfolio (REH, AGL, ADH, ORA). NXT.AX, PDN.AX, and ANZ.AX were approved but **not executed**. If all 7 MOO orders had filled, cash would be ~$57.79 — but actual cash is $1,747.80, confirming only 4 of 7 orders were placed. No rejection recorded in the dashboard. Root cause unknown — possible broker connectivity issue or manual partial execution.

### 4b. HK Market (`markets.hk`)

| Field | Expected | Actual | Verdict |
|-------|----------|--------|---------|
| `trading_mode` | "offline" (`live_enabled=false` in config) | "offline" | ✅ |
| `data_source` | "offline" | "offline" | ✅ |
| `broker` | "moomoo" (v1.1 changed from ibkr) | "moomoo" | ✅ |
| `config_version` | "v1.1" | "v1.1" | ✅ |
| `portfolio.equity` | 0 (no allocation) | 0 | ✅ |
| `portfolio.starting_equity` | 0 (config.risk.starting_equity=0) | 0 | ✅ |
| `risk.max_positions` | 10 (config) | 10 | ✅ |
| `risk.halted` | false | false | ✅ |
| `benchmark_ticker` | "2800.HK" (config.universe) | "2800.HK" | ✅ |
| All zeros | Expected for no-allocation market | All zero | ✅ |

### 4c. SP500 Market (`markets.sp500`)

| Field | Expected | Actual | Verdict |
|-------|----------|--------|---------|
| `trading_mode` | "live" | "live" | ✅ |
| `broker` | "moomoo" | "moomoo" | ✅ |
| `config_version` | "v2.2" | "v2.2" | ✅ |
| `portfolio.starting_equity` | 4000 (config) | 4000 | ✅ |
| `portfolio.equity` | 3570.5 + 432.23 = 4002.73 USD | 4002.73 | ✅ |
| `portfolio.cash` | 4000 − (3×67.04 + 1×228.38) = 3570.50 | 3570.5 | ✅ Verified: actual ON fill was $67.04, not planned $68.16 |
| `portfolio.buying_power` | Broker-reported | 3673.54 | ⚠️ Exceeds cash by $103.04 — see warning #8 |
| `portfolio.num_open` | 2 (ON + CHTR) | 2 | ✅ |
| `portfolio.market_pnl` | −1.89 + 4.62 = 2.73 | 2.73 | ✅ |
| `portfolio.total_pnl` | market_pnl(2.73) + realized(0) = 2.73 | 2.73 | ✅ |
| `portfolio.total_pnl_pct` | 2.73 / 4000 × 100 = 0.07% | 0.07 | ✅ |
| `portfolio.commission_per_trade` | 1.1 (config.fees) | 1.1 | ✅ |
| `portfolio.total_commissions` | 2 × 1.1 = 2.2 | 2.2 | ✅ |
| `portfolio.realized_pnl` | 0 (no closed Atlas trades) | 0 | ✅ for Atlas positions |
| `risk.max_positions` | 15 (config v2.2) | 15 | ✅ |
| `risk.halted` | false | false | ✅ |
| `risk.exposure_pct` | (201.12+228.38+1424.86) / 4002.73 × 100 = 46.3% | 46.3 | ✅ Includes manual XOP entry value |
| `benchmark_ticker` | "SPY" (config.universe) | "SPY" | ✅ |
| `benchmark_return_pct` | (4002.27 − 4000) / 4000 × 100 = 0.06% | 0.06 | ✅ |
| `data_freshness.broker_connected` | true | true | ✅ |
| `data_freshness.plan_date` | "2026-02-27" | "2026-02-27" | ✅ (last active plan for SP500) |
| `plan.status` | "EXECUTED" | "EXECUTED" | ✅ |
| `plan.trade_date` | "2026-02-27" | "2026-02-27" | ✅ |

**Note on SP500 exposure_pct calculation:** The 46.3% includes all positions' entry values — Atlas (ON=$201.12, CHTR=$228.38) plus manual XOP ($142.486×10=$1,424.86). Total invested = $1,854.36 / $4,002.73 = 46.3%. This is by design (positions variable includes manual).

---

## 5. Open Positions — Individual Verification

### 5a. ASX Positions

**⚠️ CRITICAL FINDING #1: All 4 ASX positions show `current_price = entry_price`, P&L = 0.**  
The IBKR broker returned fill prices as current prices. As of 10:30am AEST, the live market prices have moved. Dashboard P&L is incorrect.

| Ticker | Entry Price | Current Price (Dashboard) | Shares | Dashboard P&L | Live Price | Actual P&L | Plan Entry | Verdict |
|--------|------------|--------------------------|--------|--------------|-----------|-----------|------------|---------|
| REH.AX | 15.7333 | 15.7333 | 36 | 0.00 | **15.59** | **−5.16 AUD** | 15.64 | ❌ Stale price |
| ADH.AX | 1.8367 | 1.8367 | 304 | 0.00 | **1.805** | **−9.64 AUD** | 1.87 | ❌ Stale price |
| ORA.AX | 2.1552 | 2.1552 | 262 | 0.00 | **2.11** | **−11.84 AUD** | 2.17 | ❌ Stale price |
| AGL.AX | 9.8558 | 9.8558 | 57 | 0.00 | **9.735** | **−6.89 AUD** | 9.83 | ❌ Stale price |
| **TOTAL** | — | — | — | **0.00** | — | **−33.52 AUD** | — | ❌ |

Live prices fetched at audit time via `dashboard.live_prices.fetch_prices()`.

**Entry prices vs plan prices:**
- REH.AX: fill 15.7333 vs plan 15.64 (+0.59% above plan — slippage at ASX open)
- ADH.AX: fill 1.8367 vs plan 1.87 (−1.78% below plan — better than expected)
- ORA.AX: fill 2.1552 vs plan 2.17 (−0.68% below plan — better than expected)
- AGL.AX: fill 9.8558 vs plan 9.83 (+0.26% above plan — minor slippage)

Fill prices differ from plan prices as expected for MOO orders (executes at opening auction price, not prior-day close estimate).

**Other position field checks (ASX):**

| Field | REH.AX | ADH.AX | ORA.AX | AGL.AX | Verdict |
|-------|--------|--------|--------|--------|---------|
| `stop_price` | 14.3943 | 1.7076 | 2.0056 | 9.3636 | ✅ Matches plan |
| `strategy` | trend_following | trend_following | trend_following | trend_following | ✅ |
| `entry_date` | 2026-03-03 | 2026-03-03 | 2026-03-03 | 2026-03-03 | ✅ |
| `days_held` | 0 | 0 | 0 | 0 | ✅ (entered today) |
| `is_atlas` | true | true | true | true | ✅ |
| `market` | "asx" | "asx" | "asx" | "asx" | ✅ |
| `sector` | "Unknown" | "Unknown" | "Unknown" | "Unknown" | ⚠️ See warning #10 |

### 5b. SP500 Positions

**Note on ON entry price:** The broker state file (`brokers/state/sp500.json`) records ON entry_price=68.16 (the planned price), but the dashboard shows 67.04. The dashboard is **correct** — verified by cash balance math: $4000 − (3×$67.04 + 1×$228.38) = $3,570.50 matches actual cash. The state file is stale and shows the planned price, not the actual MOO fill.

| Ticker | Entry Price | Current Price (Dashboard) | Shares | Dashboard P&L | Dashboard P&L% | Live Price | Verdict |
|--------|------------|--------------------------|--------|--------------|----------------|-----------|---------|
| ON | 67.04 | 66.41 | 3 | −1.89 | −0.94% | 66.48 | ✅ Math correct; live price close to dashboard (minor staleness ~+0.07) |
| CHTR | 228.38 | 233.00 | 1 | 4.62 | 2.02% | 232.80 | ✅ Math correct; live price close (−0.20 diff) |

**P&L verification:**
```
ON:   (66.41 − 67.04) × 3 = −1.89 ✅
CHTR: (233.00 − 228.38) × 1 = 4.62 ✅
ON pnl_pct:   (66.41 − 67.04) / 67.04 × 100 = −0.940% ✅
CHTR pnl_pct: (233.00 − 228.38) / 228.38 × 100 = 2.023% ≈ 2.02% ✅
```

| Field | ON | CHTR | Verdict |
|-------|-----|------|---------|
| `entry_date` | 2026-02-27 | 2026-02-27 | ✅ Matches plan |
| `days_held` | 4 | 4 | ✅ Feb 27 → Mar 3 = 4 calendar days |
| `stop_price` | 62.5013 | 211.8972 | ✅ Matches plan |
| `strategy` | trend_following | trend_following | ✅ |
| `is_atlas` | true | true | ✅ |
| `market` | "sp500" | "sp500" | ✅ |
| `sector` | "Unknown" | "Unknown" | ⚠️ State file has ON=Technology, CHTR=Communication Services — enrichment not applied |

---

## 6. Manual Positions

### XOP (SP500 manual)

**⚠️ CRITICAL FINDING #2: XOP position fields are internally inconsistent.**

The `entry_price`, `current_price`, `pnl`, and `pnl_pct` fields do not agree with each other:

| Field | Dashboard Value | Expected from other fields | Verdict |
|-------|----------------|---------------------------|---------|
| `entry_price` | 142.486 | — | baseline |
| `current_price` | 172.897 | Should yield pnl=(172.897−142.486)×10=**304.11** | ❌ |
| `shares` | 10 | — | ✅ |
| `pnl` | 178.99 | From current_price: **304.11** not 178.99 | ❌ |
| `pnl_pct` | 11.55% | From current_price: **(172.897−142.486)/142.486 = 21.34%** | ❌ |
| `pnl_pct` | 11.55% | From pnl=178.99: **178.99/(142.486×10) = 12.56%** | ❌ |

**Live XOP price at audit time: $159.56 USD**

The only consistent interpretation: `pnl` and `pnl_pct` were computed from a **stale price of ~$160.39** (178.99/10 + 142.486 ≈ 160.39), while `current_price` (172.897) is from an **even older cache**. The live price ($159.56) is closest to what the P&L implies.

Root cause: The Moomoo broker API returned the `unrealized_pnl` and `unrealized_pnl_pct` fields calculated from one snapshot timestamp, while `current_price` comes from a different (older cached) snapshot. The dashboard passes through these broker fields without checking internal consistency.

| Field | `days_held` | `stop_price` | `is_atlas` | `market` |
|-------|------------|-------------|-----------|---------|
| XOP | 0 (no entry_date) | 0 | false | "sp500" |
| Verdict | ⚠️ No entry date recorded | ✅ Correct (no stop for manual) | ✅ | ✅ |

---

## 7. Plan Section

### ASX Plan (`markets.asx.plan`)
| Field | Expected | Actual | Verdict |
|-------|----------|--------|---------|
| `trade_date` | "2026-03-03" | "2026-03-03" | ✅ |
| `status` | "APPROVED" | "APPROVED" | ✅ Matches `plans/plan_asx_2026-03-03.json` |
| `market_id` | "asx" | "asx" | ✅ |
| Entries count | 7 proposed | 7 shown | ✅ Matches plan file |
| `risk_summary.positions_after` | 7 | 7 | ✅ |
| `risk_summary.risk_pct_of_equity` | 7.26% | 7.26 | ✅ |

⚠️ **Plan shows 7 APPROVED entries but only 4 were executed** (see section 4a). The dashboard correctly shows the plan as approved, but the portfolio only reflects 4 fills.

### SP500 Plan (`markets.sp500.plan`)
| Field | Expected | Actual | Verdict |
|-------|----------|--------|---------|
| `trade_date` | "2026-02-27" | "2026-02-27" | ✅ |
| `status` | "EXECUTED" | "EXECUTED" | ✅ Matches `plans/plan_sp500_2026-02-27.json` |
| ON entry_price | 68.16 (plan) | 68.16 | ✅ Plan price shown (actual fill 67.04 — correct that plan shows planned price) |
| CHTR entry_price | 228.38 (plan) | 228.38 | ✅ |

### Top-Level Plan
| Field | Expected | Actual | Verdict |
|-------|----------|--------|---------|
| Shows which market's plan | Combined or most relevant | SP500 plan (EXECUTED 2/27) | ⚠️ See warning #9 |

---

## 8. Equity Curves

### ASX Equity Curve
| Date | Value | Expected | Verdict |
|------|-------|----------|---------|
| 2026-03-02 | 3999 | 3999 (no positions, IBKR account inception) | ✅ |
| 2026-03-03 | 3999.0 | 3999 (positions entered, P&L=0 due to stale prices) | ⚠️ Stale (see finding #1) |

Last point matches reported equity ✅. Only 2 data points (system started 2026-03-02).

### SP500 Equity Curve
| Date | Value | Expected | Verdict |
|------|-------|----------|---------|
| 2026-02-27 | 4000 | 4000 (starting equity, no positions yet) | ✅ |
| 2026-02-28 | 4004.57 | Confirmed in live_sp500.json equity_history | ✅ |
| 2026-03-02 | 4000 | Positions at entry value (no P&L change) | ✅ |
| 2026-03-03 | 4002.73 | Matches current portfolio equity | ✅ |

### Combined Top-Level Equity Curve
| Date | Value | Expected | Verdict |
|------|-------|----------|---------|
| 2026-02-27 | 9666.24 | 3999 + 4000×1.41681 = 9666.24 | ✅ |
| 2026-02-28 | 9672.71 | 3999 + 4004.57×1.41681 = 9672.71 | ✅ |
| 2026-03-02 | 9666.24 | 3999 + 4000×1.41681 = 9666.24 | ✅ |
| 2026-03-03 | 9670.11 | 3999 + 4002.73×1.41681 = 9670.11 | ✅ |

All equity curve points verified with FX-adjusted math. ✅

---

## 9. Risk Section

### Top-Level Risk

**⚠️ WARNING #4: Top-level `risk` block shows SP500 values only — not combined portfolio.**

The code explicitly picks `primary_data.get("risk", {})` where `primary = "sp500"`. This means:

| Field | Dashboard | SP500 Config | ASX Config | Combined (expected) | Verdict |
|-------|-----------|-------------|-----------|----------------------|---------|
| `exposure_pct` | 46.3% | 46.3% (SP500 only) | 56.3% | ~50.5% (combined atlas+manual, FX adj) | ⚠️ Shows SP500 only |
| `max_positions` | 15 | 15 | 7 | N/A (per-market) | ⚠️ Shows SP500 only |
| `risk_per_trade` | 0.005 | 0.005 | 0.005 (config uses 0.02 for ASX) | N/A | ⚠️ Note: ASX config max_risk_per_trade_pct=0.02, not 0.005 |
| `max_portfolio_risk` | 0.05 | 0.05 | Not specified | N/A | ⚠️ Shows SP500 only |
| `halted` | false | false | false | false | ✅ Both correct |

**Note on ASX risk_per_trade discrepancy:** ASX config has `max_risk_per_trade_pct = 0.02` but the top-level risk shows `risk_per_trade = 0.005` (from SP500 config). This is misleading.

### Per-Market Risk Sections
- ASX: `max_positions=7` ✅, `halted=false` ✅, `exposure_pct=56.3%` ✅, `risk_per_trade=0.005` ⚠️ (ASX config shows 0.02, not 0.005 — appears to be a field that isn't populated from the right config key)
- SP500: `max_positions=15` ✅, `halted=false` ✅, `exposure_pct=46.3%` ✅
- HK: `max_positions=10` ✅, `halted=false` ✅, `exposure_pct=0%` ✅

---

## 10. Strategy Summary

### ASX Strategy Summary
| Strategy | Positions | Unrealized P&L | Market Value | Verdict |
|---------|-----------|---------------|-------------|---------|
| trend_following | 4 | 0.0 | 2251.2 | ✅ positions correct; P&L 0 due to stale prices |

Market value verification: 36×15.7333 + 304×1.8367 + 262×2.1552 + 57×9.8558 = 566.4 + 558.36 + 564.66 + 561.78 = **2251.2 AUD** ✅

### SP500 Strategy Summary
| Strategy | Positions | Unrealized P&L | Market Value | Verdict |
|---------|-----------|---------------|-------------|---------|
| trend_following | 2 | 2.73 | 432.23 | ✅ positions, P&L correct |

Market value: 3×66.41 + 1×233.0 = 199.23 + 233.0 = **432.23 USD** ✅

### Top-Level Strategy Summary

**⚠️ CRITICAL FINDING #3: Market value mixes AUD and USD without FX conversion.**

```python
# Source code (generate_data.py line 1789):
combined_strats[key]["market_value"] += s["market_value"]  # No currency conversion!
```

| Field | Dashboard | Correct Value | Difference | Verdict |
|-------|-----------|--------------|------------|---------|
| `trend_following.positions` | 6 | 6 | — | ✅ |
| `trend_following.unrealized_pnl` | 2.73 | Should be 2.73 USD × 1.41681 = 3.87 AUD | **−1.14 AUD** | ❌ Currency not converted |
| `trend_following.market_value` | 2683.43 | 2251.2 AUD + 432.23 USD×1.41681 = **2863.59 AUD** | **−180.16 AUD** (−6.3%) | ❌ USD added without FX conversion |

The dashboard adds ASX values (AUD) directly to SP500 values (USD) without converting. This understates both the combined market value and unrealized P&L.

---

## 11. Manual Positions (Top-Level)

| Field | Dashboard | Correct | Verdict |
|-------|-----------|---------|---------|
| `num_open` | 1 | 1 (XOP only) | ✅ |
| `positions` | [XOP] | [XOP] | ✅ |
| `unrealized_pnl` | 178.99 | Inconsistent (see finding #2) | ❌ |
| `market_value` | 1728.97 | 172.897 × 10 = 1728.97 | ✅ (math correct but current_price stale) |

**Note:** The `market_value` (172.897 × 10 = 1728.97 USD) is shown in USD within the AUD-denominated combined dashboard without FX conversion. Correct AUD equivalent: 1728.97 × 1.41681 = **2450.07 AUD**.

---

## 12. Closed Trades

| Field | Dashboard | Source Files | Verdict |
|-------|-----------|-------------|---------|
| `closed_trades` | [] (empty) | `live_asx.json`: closed_trades=[], `live_sp500.json`: closed_trades=[] | ✅ No closed Atlas trades |

**⚠️ WARNING #5: ONDL trades missing from realized P&L.**

The `pending_orders` section for SP500 shows 2 filled ONDL orders on 2026-03-02:
- BUY 37 @ 26.95, filled @ 26.95
- SELL 37 @ 28.39, filled @ 28.45

Profit: (28.45 − 26.95) × 37 = **$55.50 USD realized profit**.

These trades do NOT appear in `closed_trades` (Atlas accounting) and are NOT in `realized_pnl`. This is because Atlas only tracks its own positions, not manual ONDL trades. However, this means the **actual Moomoo account balance differs from the Atlas-computed equity** of $4,002.73. The dashboard equity is underreporting the true account value.

---

## 13. Benchmark Data

### ASX Benchmark (IOZ.AX)
| Date | Value | Verdict |
|------|-------|---------|
| 2026-02-26 | 3999.0 | ✅ (starting equity reference) |
| 2026-02-27 | 4008.75 | ✅ |
| 2026-03-02 | 4009.83 | ✅ |
| 2026-03-03 | 3983.84 | ✅ |
| `benchmark_return_pct` | (3983.84 − 3999) / 3999 × 100 = −0.38% | ✅ |

### SP500 Benchmark (SPY)
| Date | Value | Verdict |
|------|-------|---------|
| 2026-02-27 | 4000.0 | ✅ |
| 2026-03-02 | 4002.27 | ✅ |
| 2026-03-03 | (missing) | ⚠️ **Warning #7: No 2026-03-03 data point** |
| `benchmark_return_pct` | (4002.27 − 4000) / 4000 × 100 = 0.057% ≈ 0.06% | ✅ |

The SP500 benchmark curve stops at 2026-03-02. Today's SPY data point (2026-03-03) is absent. This means the combined benchmark curve uses a stale SPY value for today's blended benchmark return.

### Combined Benchmark
| Date | Value | Verdict |
|------|-------|---------|
| 2026-02-26 | 9666.24 | ✅ |
| 2026-02-27 | 9675.99 | ✅ |
| 2026-03-02 | 9680.29 | ✅ |
| 2026-03-03 | 9654.30 | ⚠️ SP500 component uses stale 3/2 data |
| `benchmark_return_pct` | (9654.30 − 9666.24) / 9666.24 × 100 = −0.12% | ✅ Math correct but uses stale SP500 data |
| `benchmark_ticker` | "IOZ + SPY" | ✅ |

---

## 14. Research Section

### Queue Statistics
| Field | Dashboard | Verified (from `research/queue.json`) | Verdict |
|-------|-----------|--------------------------------------|---------|
| `queue.total` | 24 | 24 items in queue.json | ✅ |
| `queue.queued` | 0 | 0 active | ✅ |
| `queue.running` | 0 | 0 running | ✅ |
| `queue.completed` | 16 | passed(8)+failed(5)+partial(1)+promoted(2) = 16 | ✅ |
| Deferred | 8 (implicit) | 8 items with status="deferred" | ✅ |

### Research Statistics
| Field | Dashboard | Verified (from `research/journal.json`) | Verdict |
|-------|-----------|----------------------------------------|---------|
| `total_experiments` | 42 | 42 entries in journal.json | ✅ |
| `passed` | 11 | verdict="pass": 11 | ✅ |
| `failed` | 19 | verdict="fail": 19 | ✅ |
| `partial` | 9 | verdict="partial": 9 | ✅ |
| `promoted` | 3 | verdict="promoted": 3 | ✅ |
| `pass_rate_pct` | 26.2 | 11/42 × 100 = 26.19% | ✅ |

### Cumulative Impact
| Field | Dashboard | Verdict |
|-------|-----------|---------|
| `sharpe_delta` | 0.28 | ✅ Matches SMA-200 filter result (+0.28 Sharpe) |
| `promotions` | 3 | ✅ Three promoted experiments confirmed |

### Recent Results & Strategy Coverage
The 10 recent_results entries and 9 strategy_coverage entries were spot-checked against the experiment files in `research/experiments/`. All match.

### Daily Insight
The VIX regime scatter chart data (35 monthly data points, March 2024 – February 2026) appears consistent with historical VIX and SPY monthly return data.

---

## 15. Pending Orders

| Field | Dashboard | Verdict |
|-------|-----------|---------|
| ASX pending_orders | [] | ✅ No pending IBKR orders |
| SP500 pending_orders | [ONDL BUY, ONDL SELL] | ⚠️ See warning #5 — filled manual trades present |
| HK pending_orders | [] | ✅ |

---

## Summary of Issues Found

### ❌ Critical Issues

| # | Location | Issue | Impact | Fix |
|---|----------|-------|--------|-----|
| C1 | `markets.asx.portfolio.open_positions[*].current_price` | All 4 ASX positions show `current_price = entry_price`. Actual live prices diverge by −0.9% to −1.8%. Dashboard P&L = 0 when actual unrealized P&L = **−33.52 AUD** | P&L reporting incorrect for ASX. Users see 0 loss when actually −33.52 AUD | IBKR needs to provide live prices post-fill; consider refreshing prices after order fills using yfinance or IBKR market data |
| C2 | `markets.sp500.manual_positions.positions[0]` (XOP) | `entry_price=142.486`, `current_price=172.897`, `pnl=178.99`, `pnl_pct=11.55%` are internally inconsistent. Math shows: (172.897−142.486)×10 = **304.11** not 178.99; pnl_pct should be **21.34%** not 11.55%. Live price = 159.56 | Manual position P&L is wrong. Users see stale/incorrect values | Detect broker data inconsistency (pnl vs current_price); validate that pnl ≈ (current_price − entry_price) × shares before publishing |
| C3 | `strategy_summary[0].market_value` (top-level) | ASX AUD value (2251.2) added to SP500 USD value (432.23) without FX conversion → shows **2683.43** instead of correct **2863.59 AUD** (+180.16 AUD understated) | Combined portfolio market value understated by 6.3% | In `generate_data.py` merge loop, convert SP500 market_value using `to_aud()` before summing |

### ⚠️ Warning Issues

| # | Location | Issue | Impact | Fix |
|----|----------|-------|--------|-----|
| W1 | Top-level `risk` block | Shows SP500-only risk parameters (max_positions=15, risk_per_trade=0.005) not combined portfolio metrics | Misleading for multi-market portfolio view | Compute combined risk metrics or show per-market breakdown |
| W2 | `strategy_summary[0].unrealized_pnl` (top-level) | SP500 unrealized_pnl (2.73 USD) added directly to ASX (0.0 AUD) without FX conversion — shows 2.73 when should be **3.87 AUD** | Understates combined unrealized P&L | Same fix as C3 — apply `to_aud()` in merge loop |
| W3 | Top-level `plan` | Shows SP500 plan (EXECUTED 2/27) — the ASX plan (APPROVED today 3/3) is more relevant for current trading decisions | Operators see stale EXECUTED plan, not today's active APPROVED plan | Prefer APPROVED plan over EXECUTED; or show both markets' plans |
| W4 | SP500 `data_freshness.plan_date` | SP500 plan is from 2/27 (5 days ago). No new SP500 plan generated for today (3/3) | Normal — SP500 market closed until next entry signal | Low severity — expected |
| W5 | `closed_trades` / `realized_pnl` | ONDL manual trades (profit ~$55.50 USD, 2/37 shares × $1.50/share) appear in `pending_orders` but NOT in `realized_pnl`. Dashboard equity understates actual Moomoo account balance | Atlas equity ($4,002.73) ≠ actual broker equity (~$4,058+) | Document design choice clearly; or add an "actual_broker_equity" field from `broker_acct` |
| W6 | `brokers/state/sp500.json` | ON entry_price = 68.16 (planned), not actual fill 67.04. Dashboard correctly shows 67.04 (verified by cash math), but state file is wrong | State file drift — could corrupt paper fallback P&L calculations | Update live state file with actual fill prices when orders execute |
| W7 | `markets.sp500.benchmark_curve` | Missing 2026-03-03 data point — curve only goes to 3/2 | Today's combined benchmark return uses stale SP500 SPY data | Ensure SPY benchmark data fetched each day, even on days with no positions |
| W8 | `markets.sp500.portfolio.buying_power` | $3,673.54 USD > cash $3,570.50 (difference $103.04) — buying_power exceeds settled cash | Unexplained: could be margin, dividend credit, or Moomoo-specific buying power calculation | Investigate Moomoo buying_power API field definition |
| W9 | All position `sector` fields | All positions show `sector = "Unknown"`. State file has ON=Technology, CHTR=Communication Services. Sector enrichment not applied | Strategy sector concentration analysis cannot be performed | Fix sector enrichment pipeline in generate_data.py to read sector from broker data or state file |
| W10 | ASX `risk.risk_per_trade` | Shows 0.005 (SP500 value) but ASX config has `max_risk_per_trade_pct = 0.02` | Misleading — ASX uses 2% risk per trade, not 0.5% | Fix risk block population to read from market-specific config |

---

## Verified Correct Items (✅ Summary)

The following 34+ items were explicitly verified and found correct:

- Account equity, cash, buying_power AUD aggregation with FX conversion
- Portfolio starting_equity, total_pnl, total_pnl_pct, market_pnl, realized_pnl
- SP500 ON position: entry_price (67.04 actual fill), current_price, pnl (−1.89), pnl_pct (−0.94%), days_held (4)
- SP500 CHTR position: entry_price, current_price, pnl (4.62), pnl_pct (2.02%), days_held (4)
- SP500 cash balance verified by entry-price math ($3,570.50 = $4,000 − $201.12 − $228.38)
- SP500 commissions: 2 × $1.10 = $2.20
- ASX commissions: 4 × $6.00 = $24.00
- ASX fill prices vs. plan prices (minor expected slippage on MOO orders)
- ASX exposure_pct: 56.3% = 2251.2 / 3999.0
- SP500 exposure_pct: 46.3% (confirmed includes manual XOP entry value)
- HK market offline status (live_enabled=false), all zeros, correct config
- All equity curve points verified with FX math (all 4 date points)
- ASX benchmark_return_pct: −0.38%
- SP500 benchmark_return_pct: 0.06%
- Combined benchmark_return_pct: −0.12%
- All config_version fields match config files
- All broker assignments match config files
- All per-market max_positions from config
- All halted=false fields
- Research queue: total=24, completed=16, deferred=8
- Research statistics: 42 experiments, 11 pass, 19 fail, 9 partial, 3 promoted (all verified against journal.json)
- Pass rate: 26.2% = 11/42
- Cumulative sharpe_delta=0.28, promotions=3
- FX cross-check: 1/AUDUSD ≈ USDAUD internally consistent
- Plan dates and statuses (ASX APPROVED 3/3, SP500 EXECUTED 2/27)
- ASX stop prices match plan file
- SP500 stop prices match plan file
- ASX equity curve: only 2 points, mathematically correct
- data_freshness fields for all markets
- Manual positions: num_open=1, market_value=1728.97 (172.897×10) math correct
- Days held: 0 for ASX (entered today), 4 for SP500 (Feb 27 → Mar 3)
- Strategy summary per-market values correct before aggregation

---

## Recommended Fixes (Priority Order)

### Priority 1 — Fix Immediately

1. **C3: Strategy summary FX conversion** (`generate_data.py` ~line 1789)  
   ```python
   # CURRENT (broken):
   combined_strats[key]["market_value"] += s["market_value"]
   combined_strats[key]["unrealized_pnl"] += s["unrealized_pnl"]
   
   # FIX:
   combined_strats[key]["market_value"] += to_aud(s["market_value"], ccy)
   combined_strats[key]["unrealized_pnl"] += to_aud(s["unrealized_pnl"], ccy)
   ```

2. **C2: XOP consistency validation** — Add a post-broker-fetch validation:
   ```python
   for p in positions:
       ep, cp, sh = p.get("entry_price", 0), p.get("current_price", 0), p.get("shares", 0)
       expected_pnl = (cp - ep) * sh
       actual_pnl = p.get("unrealized_pnl", 0)
       if abs(actual_pnl - expected_pnl) / max(abs(expected_pnl), 1) > 0.10:
           logger.warning("P&L inconsistency for %s: broker=%s, calculated=%s", p["ticker"], actual_pnl, expected_pnl)
           p["unrealized_pnl"] = round(expected_pnl, 2)  # override with calculated
   ```

### Priority 2 — Fix This Week

3. **C1: ASX stale prices** — After IBKR fills execute, perform a yfinance price refresh before dashboard generation. Add a 5-minute wait post-MOO fill before running the dashboard.

4. **W9: Sector enrichment** — Pass sector from broker state data into position enrichment. IBKR and Moomoo both return sector information.

5. **W6: Live state file entry price** — Update `live_sp500.json` with actual fill price (67.04) when the MOO order result is received from Moomoo.

6. **W3: Top-level plan selection** — Prefer APPROVED plan over EXECUTED plan when choosing which to surface at top level:
   ```python
   # Pick the plan most relevant to current trading
   primary_plan = next(
       (md.get("plan") for md in market_data.values() if md.get("plan", {}).get("status") == "APPROVED"),
       primary_data.get("plan")
   )
   ```

### Priority 3 — Address When Convenient

7. **W1/W10: Top-level risk block** — Compute combined risk metrics rather than copying SP500 values verbatim. At minimum, fix `risk_per_trade` to not show SP500's 0.005 when ASX is using 0.02.

8. **W5: ONDL realized P&L gap** — Document explicitly that Atlas equity ≠ broker account balance (design choice). Consider adding `actual_broker_equity` field from `broker_acct.total_assets` for transparency.

9. **W7: SP500 benchmark curve gap** — Ensure SPY data is fetched even on non-plan days.

10. **W8: SP500 buying_power anomaly** — Investigate Moomoo's buying_power field; ensure it's correctly interpreted.

---

## Appendix: Sources Verified

| Source File | Purpose | Status |
|------------|---------|--------|
| `dashboard/data/dashboard-data.json` | Main audit target | Audited |
| `config/active/asx.json` | ASX config | Verified |
| `config/active/sp500.json` | SP500 config | Verified |
| `config/active/hk.json` | HK config | Verified |
| `brokers/state/asx.json` | ASX paper state | Verified |
| `brokers/state/sp500.json` | SP500 paper state (ON entry_price stale) | Verified — mismatch found |
| `brokers/state/hk.json` | HK paper state | Verified |
| `brokers/state/live_asx.json` | ASX live state | Verified |
| `brokers/state/live_sp500.json` | SP500 live state | Verified |
| `plans/plan_asx_2026-03-03.json` | Today's ASX plan | Verified |
| `plans/plan_sp500_2026-02-27.json` | Active SP500 plan | Verified |
| `research/queue.json` | Research queue | Verified |
| `research/journal.json` | Research journal | Verified |
| `dashboard/generate_data.py` | Generator code | Key sections reviewed |
| Yahoo Finance (live) | Price verification | REH.AX, ADH.AX, ORA.AX, AGL.AX, ON, CHTR, XOP fetched |

---

*End of audit report — 2026-03-03*
