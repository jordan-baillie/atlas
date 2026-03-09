---
experiment_id: wave1_vix_filter
wave: 1
strategy: combined
category: filter
market: sp500
verdict: fail
promoted: false
date: "2026-03-01"
tags:
  - experiment
  - strategy/combined
  - verdict/fail
  - wave/1
  - category/filter
  - market/sp500
---

# VIX Filter

> **Wave:** [[Wave 1]] | **Strategy:** [[Combined Portfolio]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

Reducing exposure during high-VIX periods (VIX > 25-30) improves risk-adjusted returns by avoiding entries during market panic. Alternative: only allow mean_reversion entries during high VIX (buy the panic). Williams VIX Fix (already in helpers.py) can be used as stock-level proxy if index VIX data unavailable.

## Results

*No metrics recorded for this experiment.*

## Verdict

**FAIL**

*Criteria:* Sharpe improvement >= 0.03 without CAGR dropping > 2pp. Min 200 trades.

VIX filter HURTS performance across ALL variants. Baseline Sharpe 0.59 is best. VIX<20 drops Sharpe to 0.03 (catastrophic). VIX<25 drops to 0.47, VIX<30 to 0.51, VIX<35 to 0.50. Current strategies (especially MR) actually PROFIT from high-VIX entries. Blocking high-VIX entries removes the best MR trades.

## Delta vs Baseline

| Metric | Change |
|--------|--------|
| Sharpe | -0.09 |
| Note | best variant VIX<30 still -0.08 Sharpe |

## Learnings

- VIX filter is counterproductive for this portfolio mix
- Mean reversion thrives during high-VIX (panic) periods — blocking entries there destroys alpha
- All 4 VIX thresholds tested (20/25/30/35) degrade Sharpe
- KEY INSIGHT: MR buys oversold stocks during panic, which is the high-VIX regime
- VIX filter might work for trend-only portfolio but not MR-heavy one
- CLOSED: Do not re-test VIX filters on combined portfolio

---

Strategy:: [[Combined Portfolio]]
Wave:: [[Wave 1]]