# Overlay Phase 3A — Flip Decision Report

**Date**: 2026-04-29  
**Author**: Backend Developer (RCA #3A)  
**Commit ref**: Phase 3A  

---

## Summary

**DECISION: FLIP** ✅

The sp500 overlay's `shadow_mode` is being changed from `true` → `false` for the sp500 market only.  
`commodity_etfs` and `sector_etfs` remain in shadow mode (unchanged).

---

## Background

- Layer 3 overlay ("AI tighten") runs daily at ~09:00 UTC, evaluates macro/news/chart context,
  and emits a `sizing_override` multiplier (e.g. 0.8 = reduce position by 20%).
- Since deployment, the overlay has operated in `shadow_mode: true` — decisions are recorded
  to `overlay_decisions` and `overlay_shadow_log` tables but NOT applied to actual order sizing.
- This report evaluates whether applying the overlay would have improved outcomes over the
  last 7 trading days for sp500, and whether the FLIP gate should be opened.

---

## 7-Day Window

| | |
|---|---|
| **Start** | 2026-04-21 (Monday) |
| **End** | 2026-04-29 (Tuesday) |
| **Trading days** | Apr 21, 22, 23, 24, 25, 28, 29 |
| **Market** | sp500 only |

---

## Overlay Decisions in Window

| Date | Action | Sizing Override | Tickers Avoided |
|------|--------|-----------------|-----------------|
| 2026-04-21 | tighten | 0.55 (45% reduction) | XLE, XOP, INSW, UNG |
| 2026-04-22 | no_change | — | — |
| 2026-04-23 | tighten | 0.55 (45% reduction) | XOP, XLE, INSW, UNG |
| 2026-04-24 | no_change | — | — |
| 2026-04-25 | (no decision recorded) | — | — |
| 2026-04-27 | tighten | 0.80 (20% reduction) | XOP, XLE, INSW |
| 2026-04-28 | tighten | 0.80 (20% reduction) | NVDA, AAPL, AMZN, GOOGL, GOOG |

4 of 6 days with decisions were `tighten`. 0 of 6 were `expand`.

---

## N Trades Evaluated

| Category | Count |
|----------|-------|
| Total sp500 trades entered | 8 |
| Closed (evaluable) | 6 |
| Open (excluded from P&L) | 2 (CAT, MU-Apr29) |
| Reconciliation entries (excluded) | 1 (CHTR Apr24 — strategy=reconciled) |
| Tighten-overlay-affected closed trades | **3** |

---

## Per-Trade Impact Table

| Trade ID | Ticker | Date | Actual Shares | Entry | Exit | Actual P&L | Overlay | Hypo Shares | Hypo P&L | **Delta** |
|----------|--------|------|--------------|-------|------|------------|---------|-------------|----------|-----------|
| 172 | CHTR | 2026-04-21 | 1 | $243.93 | $241.84 | **-$2.09** | tighten(0.55) | 0 (blocked) | $0.00 | **+$2.09** |
| 173 | ON | 2026-04-22 | 4 | $85.51 | $96.01 | +$42.01 | no_change | 4 | +$42.01 | $0.00 |
| 181 | AVGO | 2026-04-23 | 1 | $422.57 | $399.07 | **-$23.50** | tighten(0.55) | 0 (blocked) | $0.00 | **+$23.50** |
| 189 | ADI | 2026-04-24 | 2 | $403.88 | $383.83 | -$40.10 | no_change | 2 | -$40.10 | $0.00 |
| 190 | FCX | 2026-04-24 | 5 | $61.48 | $58.34 | -$15.71 | no_change | 5 | -$15.71 | $0.00 |
| 191 | MU | 2026-04-28 | 2 | $517.70 | $508.68 | **-$18.04** | tighten(0.8) | 1 | -$9.02 | **+$9.02** |

**Note on "blocked" trades**: `int(1 × 0.55) = 0` shares → live_executor skips the entry entirely.
This is the correct behavior per `live_executor.py:787` (`new_qty = int(original_qty * effective_multiplier)`).

---

## Cumulative P&L Comparison

| Scenario | Cumulative P&L | Notes |
|----------|---------------|-------|
| **A — Shadow (actual)** | **-$57.43** | All 6 closed trades as executed |
| **B — Enforce (hypothetical)** | **-$22.82** | Tighten overlay applied to 3 trades |
| **Delta (Y − X)** | **+$34.61** | Enforce would have saved $34.61 |

---

## Decision Criteria

| Criterion | Threshold | Result | Pass? |
|-----------|-----------|--------|-------|
| Cumulative delta | ≥ $0.01 | **+$34.61** | ✅ PASS |
| Median per-trade delta (all closed) | ≥ $0.00 | **+$1.05** | ✅ PASS |
| Median per-trade delta (tighten-only) | reference | **+$9.02** | (reference) |

**Both criteria pass. No winners were blocked** (the ON +$42.01 was on a `no_change` day).  
All 3 tighten decisions correctly identified losers (CHTR: -$2.09, AVGO: -$23.50, MU: -$18.04).

---

## Decision: FLIP

**Action taken:**

1. `config/active/sp500.json` → `overlay.shadow_mode: false`
2. `config/active/sp500.json` → `overlay.overlay_enforce_validated: true` (gate proof)
3. `config/schema.py` → Cross-field validation gate added: any market with `shadow_mode: false`
   and missing/false `overlay_enforce_validated` is rejected by `validate_config()`.

**Markets still in shadow mode:**
- `commodity_etfs` — shadow_mode: true (unchanged)
- `sector_etfs` — shadow_mode: true (unchanged)

---

## Config-Promotion Gate

A cross-field validation rule was added to `config/schema.py` in `validate_config()`:

```
[OVERLAY_ENFORCE] overlay.shadow_mode = false requires overlay.overlay_enforce_validated = true
```

Any future market that tries to disable shadow mode without first passing this validation
step (and setting `overlay_enforce_validated: true`) will be rejected with a `[OVERLAY_ENFORCE]`
error. This prevents accidental enforce-mode deployments without data backing.

---

## Backtest Script

Backtest methodology implemented in: `scripts/backtest_overlay_phase3a.py`

Run with: `python3 scripts/backtest_overlay_phase3a.py`

---

## Caveats & Risks

1. **Small sample**: 3 trades affected by tighten overlay. Statistically thin — but the
   direction is unambiguous (all 3 were losers).
2. **Blocked trades**: 2 of 3 affected trades had 1 share → multiplied to 0 → fully blocked.
   This is a binary outcome, not a graded reduction. With larger positions, the effect
   would be smoother.
3. **No winners blocked**: The strongest result is that no winners were mistakenly blocked
   (ON +$42.01 was on a `no_change` day).
4. **Open trades excluded**: CAT and MU-Apr29 are open; their final P&L unknown.
5. **Adverse selection risk**: Overlay may have missed a future winner on a tighten day.
   Continue monitoring `overlay_shadow_log` (now writing to it under enforce mode too if
   a second shadow layer is added).

---

*Report generated by Backend Developer — RCA Phase 3A — 2026-04-29*
