# Leverage Gate Audit — 2026-04-27

## Executive Summary

**Verdict: Hybrid — the 1.75× state is INTENTIONAL and within the configured 2.0× cap,
but there was NO runtime invariant that prevented a future order from breaching the cap.**

A leverage gate has now been added to `brokers/live_executor.py::_execute_entry()`.

---

## Live State at Audit Time

| Metric | Value |
|--------|-------|
| Equity | $5,435.94 |
| Cash | −$4,062.64 (margin) |
| Buying power remaining | $1,373.30 |
| Total position market value | $9,498.58 |
| **Effective leverage** | **1.747×** |
| Configured cap (sp500) | 2.0× |
| Configured cap (sector_etfs) | 1.0× |
| Configured cap (commodity_etfs) | 1.0× |

12 positions across 3 universes:
- sp500 (5): ADI, AMD, AVGO, CAT, FCX
- commodity_etfs (4): CCJ, GLD, SLV, UNG
- sector_etfs (3): XLI, XLK, XLY

### Is 1.75× intentional?

**Yes** — the sp500 config `risk.leverage: 2.0` explicitly allows up to 2.0× equity.
Alpaca tracks `buying_power = $1,373.30` which is the remaining margin headroom before the
2.0× cap is hit. The existing `check_risk_limits()` in `live_portfolio.py` checks
`cost > available_buying_power` at plan-generation time to prevent individual signals from
exceeding available buying power.

The 1.75× level is the natural accumulation of 12 positions across 3 universes, each sized
at 0.5% risk-per-trade. The cross-universe leverage (sector_etfs + commodity_etfs both have
`leverage=1.0` in their own configs) is managed by Alpaca's unified margin account — the
broker's buying_power is the aggregate constraint.

---

## Where Sizing Happens

### 1. Signal generation (strategy layer)

Every strategy calls `utils/helpers.py::calc_position_size(equity, risk_pct, entry, stop)`:

```
risk_budget = equity × max_risk_per_trade_pct (0.5%)
shares = floor(risk_budget / risk_per_share)
cap: shares × entry ≤ equity  ← caps at 1:1 equity per single position
```

`equity` here is `LivePortfolio.equity()` — Atlas-internal equity computed as:
`starting_equity − sum(entry_costs) + realized_pnl`

This is NOT the broker's real-time equity and does NOT account for leverage or
other positions' market values.

### 2. Plan generation gating (plan.py → live_portfolio.py)

`plan.py::generate_plan()` calls `portfolio.check_risk_limits(signal)` for each signal.
`check_risk_limits()` in `live_portfolio.py` checks:

```python
effective_eq = self.equity() * self.leverage          # e.g. 5011 × 2.0 = 10022
max_risk = effective_eq × max_risk_per_trade           # inflated risk budget
if risk_amount > max_risk * 1.1: reject

available_bp = self.buying_power or (self.cash * self.leverage)
if cost > available_bp: reject                         # per-signal buying power check
```

**Gaps at this layer:**
1. Checked at plan-generation time (≤2 hours before execution)
2. Checks each signal independently — does NOT subtract already-proposed entries' costs
3. No check for `(current_mv + proposed_mv) / equity > leverage`

### 3. Execution (live_executor.py → _execute_entry)

Before this fix, `_execute_entry()` only called `preflight_check_order()` which checks:
- Single order value vs `live_safety.max_order_value` (safety cap per order)
- Daily order count limit
- qty > 0 and price > 0

**No leverage check existed here.**

---

## What Was Added

### `brokers/live_executor.py` — Pre-submit leverage gate (~line 801)

Inserted **after** the bid-ask spread capture and **before** `self._broker.place_order()`.

```python
# ── Pre-submit leverage gate ──────────────────────────────────────────────
try:
    _lever_cap = self.config.get("risk", {}).get("leverage", 1.0)
    _lev_acct = self._broker.get_account_info()
    if _lev_acct and _lev_acct.equity > 0:
        _lev_positions = self._broker.get_positions()
        _cur_mv = sum(p.market_value for p in _lev_positions if p.market_value > 0)
        _prosp_mv = qty * _order_price
        _prosp_leverage = (_cur_mv + _prosp_mv) / _lev_acct.equity
        if _prosp_leverage > _lever_cap * 1.05:   # 5% slack for price moves
            # log, journal, telegram, RETURN blocked result
except Exception as _lev_exc:
    logger.warning("Leverage gate check failed (non-blocking, proceeding): %s", _lev_exc)
```

**Key properties:**
- **Uses live broker state** — fetches real-time positions + equity at order-submit time
- **Aggregate check** — sums all current positions' market values (not per-signal)
- **5% slack** — allows up to `cap × 1.05` to prevent false blocks from price noise
- **Non-fatal** — if broker API fails, logs warning and lets the order proceed
- **Zero-equity guard** — skips check if equity = 0 (avoids division by zero)
- **Only fires for live orders** — does not affect dry_run path (dry_run returns early before this block)

### `tests/test_buying_power_gate.py` — 7 regression tests

| Test | Scenario |
|------|----------|
| `test_refuses_order_that_exceeds_leverage_cap` | 1.8× existing + $3000 order → 2.4× → BLOCKED |
| `test_approves_order_at_exactly_configured_cap` | 1.8× + $1000 → 2.0× = cap → APPROVED |
| `test_approves_order_within_slack_band` | 1.8× + $400 → 1.88× < 2.1× → APPROVED |
| `test_gate_is_non_fatal_on_broker_error` | broker raises → gate logs warning → order proceeds |
| `test_skips_gate_when_equity_is_zero` | equity=0 → no division by zero |
| `test_current_live_state_is_within_cap` | 1.747× + small order → 1.84× → APPROVED |
| `test_current_live_state_large_order_blocked` | 1.747× + $2500 → 2.21× → BLOCKED |

All 7 pass.

---

## Runtime Invariant

Going forward, any new entry order submitted through `_execute_entry()` will be refused if:

```
(sum(p.market_value for all broker positions) + qty × order_price) / account.equity
> risk.leverage × 1.05
```

This invariant runs at the moment of order submission using live broker state, making it
robust against plan staleness, price drift between plan generation and execution, and
cross-universe leverage accumulation.

---

## Secondary Gap (Not Fixed — Flagged)

`live_portfolio.check_risk_limits()` (called at plan generation in `plan.py`) checks each
signal's `cost > available_buying_power` independently without subtracting already-proposed
entries' costs. This could allow N signals each costing $X to be proposed even though
combined they exceed buying_power.

In practice, the execution-time leverage gate above catches this as a safety net.
Fixing `check_risk_limits()` would be belt-and-suspenders at the plan layer.

---

## Files Touched

| File | Change |
|------|--------|
| `brokers/live_executor.py` | Added 65-line pre-submit leverage gate block inside `_execute_entry()` |
| `tests/test_buying_power_gate.py` | New file: 7 regression tests |
| `reports/leverage_audit_2026-04-27.md` | This document |
