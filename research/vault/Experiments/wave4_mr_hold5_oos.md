---
experiment_id: wave4_mr_hold5_oos
wave: 4
strategy: mean_reversion
category: param_drift
market: sp500
verdict: fail
promoted: false
date: "2026-03-08"
tags:
  - experiment
  - "strategy/mean-reversion"
  - verdict/fail
  - wave/4
  - category/param_drift
  - market/sp500
---

# MR Hold-5 OOS Validation

> **Wave:** [[Wave 4]] | **Strategy:** [[Mean Reversion]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

Wave 3 found max_hold=5 beats max_hold=10 (Sharpe +0.035, CAGR +3.1pp, PF 4.55 vs 3.64). OOS validation needed before promotion. MR trades resolve quickly — 5-day hold captures most reversion, longer holds add noise.

## Results

*No metrics recorded for this experiment.*

## Verdict

**FAIL**

All 2 criteria failed: min_oos_sharpe_ratio_vs_is: metric not found; min_oos_profit_factor: metric not found

## Learnings

- OOS validation of max_hold=5 FAILED: zero OOS trades generated
- Walk-forward OOS window likely too short or parameter configuration prevented trade generation
- In-sample still looks good: Sharpe 0.87, PF 8.16, 53.6% WR — but can't validate out-of-sample
- max_hold=5 promotion BLOCKED until OOS can be properly executed
- POSSIBLE FIX: Extend OOS window or use different validation method (e.g., time-series cross-validation)

---

Strategy:: [[Mean Reversion]]
Wave:: [[Wave 4]]