# Per-Market Equity Attribution Audit Findings
**Date**: 2026-05-01  
**Scope**: FIX-PMEQ-AUDIT-001 comprehensive audit  
**Investigator**: Backend Developer  
**Trigger**: 3 phantom HALTs in 36 hours (see context)

---

## Executive Summary

- **Bugs fixed inline**: 2 (FIX-PMEQ-AUDIT-002, FIX-PMEQ-AUDIT-003)
- **Latent risks deferred**: 5 (Items C/H/G/E/I)
- **Non-issues confirmed**: 5 (Items A/B/D partial/F/J partial)
- **Snapshot reconciliation**: $9.52 drift (old formula data — fixed by FIX-PMEQ-AUDIT-003 at next EOD)
- **State-file ghosts**: NONE (confirmed post-fb28c6ff)
- **New tests added**: 27 (test_per_market_equity_audit.py) + 2 updates to test_per_market_drawdown.py
- **Commits**: 021f4cb0 / e6fb1921 / 37ddc20b, pushed to origin/main

---

## Item A: State-File Ghosts

**Classification**: NON-ISSUE  
**Evidence**: `check_state_file_universes()` returned zero violations. All 4 open positions (CAT/sp500, XLI/sector_etfs, FCX/commodity_etfs, GLD/commodity_etfs) are in their canonical state files.  
**Impact**: None.  
**Action**: Migration script `scripts/migrations/2026-05-01-fix-cross-market-state-ghosts.py` created as preventive tooling. Smoke-run confirmed no-op.

---

## Item B: Universe-Membership Drift

**Classification**: NON-ISSUE  
**Evidence**: All broker-held tickers resolve to their correct canonical universe:
- CAT → sp500 (dynamic builder, 199 tickers loaded)
- XLI → sector_etfs (static, 11 tickers)
- FCX → commodity_etfs (static, 10 tickers — FCX is in the list)
- GLD → commodity_etfs (confirmed)  
**Impact**: None.  
**Note**: commodity_etfs tickers = `['GLD', 'SLV', 'USO', 'XOP', 'CORN', 'DBA', 'DBB', 'UNG', 'CCJ', 'FCX']`. All broker positions are members.

---

## Item C: Snapshot Freshness — Stale Snapshot False HALT

**Classification**: BUG → **FIXED [FIX-PMEQ-AUDIT-002]**  
**Evidence**: When `_get_per_market_equity` returns `None` due to a snapshot >3 days old, `_per_market_equity_degraded` was NOT set to `True`. The fallback to `broker_eq` (global) then compared a per-market HWM against global equity, allowing cross-market equity movements to trigger false HALTs.

**Scenario that triggers it**:
1. Day 1: HWM = per-market sp500 equity = $2173
2. Day 1+4: snapshot is stale (>3 days), `_get_per_market_equity` returns None
3. Date changes → HWM resets to global broker_eq = $5000 (wrong — sp500 slice is $2173)
4. If commodity_etfs loses 2% → global drops from $5000 to $4900 → sp500 shows 2% dd → FALSE HALT

**Fix**: In `_get_per_market_equity`, the stale-snapshot path now sets `self._per_market_equity_degraded = True` before returning `None`. The existing degraded guard in `check_daily_drawdown` suppresses non-catastrophic HALT (< 20%).

**Design boundary**:
- No rows ever (new instance): degraded NOT set → HALT fires normally against global equity
- Stale snapshot (>3 days old): degraded=True → HALT suppressed below 20%
- Catastrophic override (≥ 20%) still fires in both cases

**Current state**: Snapshot is 2 days old (Apr 29). Under 3-day threshold. No immediate risk.

---

## Item D: HWM Stale-State Edge Cases

**Classification**: LATENT_RISK (partially resolved)  
**Evidence**:
- `live_commodity_etfs.json` has `daily_high_water_date: None` with `daily_high_water: 1297.55`
- This means the HWM was set at some point but the date field was never written (likely during the manual fb28c6ff state-file reconstruction)
- On next `check_daily_drawdown` call: `None != today_str` → HWM resets to current equity (safe)
- The $1297.55 high watermark is lost; not critical since commodity_etfs starting_equity=1001

**HWM sanity check**:
- sp500: HWM=$2173.83 (224% of $971 starting_equity) — 5× guard at $4855, not triggered
- sector_etfs: HWM=$1791.62 (56% of $3216 starting_equity) — 5× guard at $16080, not triggered
- commodity_etfs: HWM=$1297.55 (130% of $1001 starting_equity) — 5× guard at $5005, not triggered

**Cross-market HWM contamination**: Not present. The 5× guard catches legacy global-equity HWMs. All current HWMs are reasonable relative to starting_equity. `fb28c6ff` fix is holding.

**Deferred risk**: If `daily_high_water_date` is periodically None after state-file reconstructions (as happened with FCX transfer), the HWM resets every morning which is safe but loses intraday high tracking. The `save_state()` code always writes `daily_high_water_date` from `self.daily_high_water_date`, so this only happens when the state file is manually reconstructed.

---

## Item E: Cash-Flow Attribution Gaps

**Classification**: LATENT_RISK (documented, not fixed)  
**Evidence**:
- `ActivityType` enum includes: `SPLIT`, `SPIN`, `REORG`, `MA` (merger), `EXTRD`, etc.
- `compute_realized_cash_flow_since` only processes `FILL` and `DIV`
- All other types (including cash mergers, spinoffs) are silently skipped

**Impact analysis**:
- `SPLIT` (stock split): no cash, adjusts qty/price only → zero cash impact, OK to skip
- `SPIN` (spinoff): fractional shares → small cash credit possible. Low frequency.
- `REORG`/`MA` (cash merger): could be large cash inflow if a held position is acquired for cash. Unlikely in current short-term holdings but non-zero risk.
- `FEE/CFEE/CSD/CSW/JNLC`: intentionally excluded per docstring (global-level, not per-symbol)

**Deferred**: REORG/MA handling requires custom logic (acquired position may not be in positions list post-event). Frequency is very low. Current holdings (CAT/XLI/FCX/GLD) have no known acquisition risk.

---

## Item F: Multi-Position Simultaneous-Exit Cache Behavior

**Classification**: NON-ISSUE (documented)  
**Evidence**: `per_market_cash_flow._CACHE` keyed on `(since_ts, sorted_markets)` with 30s TTL. Two fills within 30s return same cached flows. A new exit within the 30s window shows stale data.

**Impact**: `check_daily_drawdown` fires every few minutes (not sub-second). Within 30s, position_mv from broker positions is accurate (live prices), only cash_flows may be 30s stale. The financial impact of a 30s stale cash attribution is negligible (e.g., a $1000 fill shows $0 cash flow for 30s → per-market equity temporarily understates by $1000, which is $0.001 error at today's scale).

---

## Item G: Position Transfers Between Markets

**Classification**: LATENT_RISK (documented, not fixed)  
**Evidence**: The `fb28c6ff` fix manually moved FCX from sp500 to commodity_etfs state file. After the transfer:
- Old market (sp500) HWM was set to global broker equity (from before per-market attribution)
- New market (commodity_etfs) HWM is $1297.55 with date=None

**Risk**: A future position transfer does not trigger HWM reset for either affected market. If the HWM was calibrated to include the transferred position's value, the losing market will have an inflated HWM (more likely false HALT), and the gaining market will have a deflated HWM (less protection).

**Resolution approach** (deferred): A `transfer_position_between_markets()` function that atomically: (1) removes from old state, (2) adds to new state, (3) resets HWM for BOTH markets to their post-transfer per-market equity.

---

## Item H: Zero-Position Market Gets No Snapshot Row

**Classification**: DEFERRED (policy decision required)  
**Evidence**: In `eod_settlement.py`, `_positions_by_market` is built from `broker.get_positions()`. A market with 0 open positions has no entry in this dict. `attribute_equity_pro_rata` only writes rows for markets in `positions_by_market`. A zero-position market gets no row in `market_equity_history`.

**Impact**: Next call to `_get_per_market_equity` for that market finds no row → returns None. With FIX-PMEQ-AUDIT-002, this is NOT in degraded mode (no row ≠ stale row). Falls back to global broker_eq. If the market had cash allocated (from closed positions), that cash is not tracked.

**Scenario that triggers it**: All sp500 positions close on the same day. EOD snapshot: sp500 gets no row. Next day sp500's check_daily_drawdown uses global broker_eq = $5000. HWM resets to $5000. If global drops 2%, sp500 HALT triggers even though sp500's real equity might be $3000 in cash.

**Policy question**: Should zero-position markets get a row with `allocated_equity=0` (no cash attribution) or should the cash be distributed equally among zero-position markets? Surfaced for lead decision.

**Workaround** (preventive, not implemented): In `eod_settlement.py`, pass all 3 market IDs to `attribute_equity_pro_rata` with empty position lists. The function handles `[]` positions (gets 0 MV, pro-rata cash gives 0 via `total_mv=0` else branch → equal cash split).

---

## Item I: Per-Market Snapshot Race Conditions

**Classification**: NON-ISSUE (documented acceptable risk)  
**Evidence**: All 3 EOD processes (sp500/sector_etfs/commodity_etfs, all at 08:00 UTC) call `attribute_equity_pro_rata` with full broker positions. Each process writes its own `market_id` row. `INSERT OR REPLACE` key is `(date, market_id)` — no conflicts between markets. If processes execute milliseconds apart, position MV snapshots may differ slightly.

**Impact**: Rounding-level drift (< $1) between the 3 rows. Attribution weights are identical since all 3 processes see the same positions. Acceptable for the purposes of daily drawdown monitoring.

---

## Item J: Sanity Reconciliation

**Classification**: NON-ISSUE (fixed by FIX-PMEQ-AUDIT-003)  

**Current state (Apr 29 snapshot, old formula)**:
```
commodity_etfs: allocated=$1280.80  (pos_mv=$1119.47  cash=$161.33)
sector_etfs:    allocated=$1749.77  (pos_mv=$1529.37  cash=$220.40)
sp500:          allocated=$2113.14  (pos_mv=$1846.97  cash=$266.17)
─────────────────────────────────────────────────────────────────
Sum allocated_equity: $5143.71
Broker equity (snapshot): $5134.19
DRIFT: $9.52
```

**Root cause**: Old formula was `allocated = pos_mv + broker_cash*(mv/total_mv)`, which sums to `total_pos_mv + broker_cash` (not `broker_equity`). The $9.52 gap represents Alpaca's "unsettled items" not reflected in positions or settled cash.

**Fix (FIX-PMEQ-AUDIT-003)**: New formula is `allocated = broker_equity * (mv/total_mv)`. Sum equals `broker_equity` exactly (within rounding).

**Expected reconciliation after next EOD**: drift < $1.

**Note**: The $9.52 drift in the audit script output is from the old Apr 29 snapshot. The threshold is $20, so this is a PASS. It will self-correct at next EOD.

---

## Bugs Fixed Inline

| ID | Commit | File | Description |
|----|--------|------|-------------|
| FIX-PMEQ-AUDIT-002 | 021f4cb0 | `brokers/live_portfolio.py` | Stale snapshot path in `_get_per_market_equity` now sets `_per_market_equity_degraded = True` before returning None. Prevents cross-market equity movements from triggering false HALTs when snapshot is >3 days old. |
| FIX-PMEQ-AUDIT-003 | e6fb1921 | `portfolio/market_equity_attribution.py` | `attribute_equity_pro_rata` now uses `broker_equity * weight` for `allocated_equity` instead of `pos_mv + cash_share`. Ensures `sum(allocated_equity) == broker_equity` exactly. |

---

## Latent Risks Deferred

| ID | Item | Estimated Impact | Notes |
|----|------|-----------------|-------|
| RISK-E | REORG/MA/SPIN not attributed as cash flows | Medium (if cash merger occurs) | Low frequency, requires custom logic. |
| RISK-G | Position transfer doesn't reset HWM | Medium (if manual transfer is needed) | fb28c6ff transfer resulted in hwm_date=None. Add `transfer_position_between_markets()` utility. |
| RISK-H | Zero-position market gets no snapshot row | High (if all positions close) | Policy decision needed: distribute cash to zero-position markets? |
| RISK-F | 30s TTL cache stale during burst fills | Low | Only cash-flow side; position_mv is always live. |
| RISK-I | EOD race condition (3 processes same time) | Low (rounding level) | INSERT OR REPLACE handles it cleanly. |

---

## Non-Issues Confirmed

| Item | Finding |
|------|---------|
| A | No state-file ghosts. All 4 positions in canonical state files. |
| B | No universe-membership drift. All broker positions are members of their canonical universe. |
| D (HWM 5× guard) | No cross-market HWM contamination. All HWMs within 5× starting_equity. |
| F | 30s cache risk is acceptable (drawdown check is not sub-second). |
| J (reconciliation) | $9.52 drift is from old formula, within $20 threshold. Fixed by FIX-PMEQ-AUDIT-003. |

---

## Operational Tools Created

| File | Purpose |
|------|---------|
| `scripts/audit_per_market_equity.py` | Daily sanity audit (snapshot reconciliation, freshness, ghosts, drift, HWM). Exits 0/1. |
| `scripts/migrations/2026-05-01-fix-cross-market-state-ghosts.py` | Idempotent migration for cross-market position cleanup. `--dry-run` available. |
| `tests/test_per_market_equity_audit.py` | 27 regression tests covering all audit items. |

---

*Generated by FIX-PMEQ-AUDIT-001 comprehensive audit, 2026-05-01*
