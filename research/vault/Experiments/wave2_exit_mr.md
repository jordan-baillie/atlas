---
experiment_id: wave2_exit_mr
wave: 2
strategy: mean_reversion
category: param_drift
market: sp500
verdict: fail
promoted: false
date: "2026-03-04"
tags:
  - experiment
  - "strategy/mean-reversion"
  - verdict/fail
  - wave/2
  - category/param_drift
  - market/sp500
---

# MR Exit

> **Wave:** [[Wave 2]] | **Strategy:** [[Mean Reversion]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

Research shows MR trades benefit from RSI-based exits over fixed MA exits. When RSI(2) recovers above 65-70, the oversold condition is resolved and holding longer adds risk without reward. Current MR uses SMA(20) reversion + ATR profit target; RSI recovery exit may capture the same edge with less exposure.

## Results

*No metrics recorded for this experiment.*

## Verdict

**FAIL**

*Criteria:* Sharpe improvement >= 0.05 OR PF improvement >= 0.1 vs current MR exit rules. Min 150 trades.

All 4 criteria failed: sharpe_improvement: metric not found; or_pf_improvement: metric not found; edge_p_value: 0.3212 >= 0.05 (edge not statistically significant); mc_fragile: True (p95 MC drawdown > 2× actual — trade sequence dependent)

## Learnings

- All max_hold_days values produce negative Sharpe in SOLO MR mode
- max_hold_days=10 is relatively best (-1.98), shorter holds are worse
- Current default (15) is slightly worse than 10 (-2.08 vs -1.98)
- NOTE: Solo MR on $4K has inherent negative Sharpe due to fee drag
- Relative ranking useful: 10 > 15 > 7 > 5 > 3 for max_hold_days
- NEEDS COMBINED TEST: solo param sweep is misleading at this equity level

---

Strategy:: [[Mean Reversion]]
Wave:: [[Wave 2]]