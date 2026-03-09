---
experiment_id: wave2_exit_og
wave: 2
strategy: opening_gap
category: param_drift
market: sp500
verdict: fail
promoted: false
total_trades: 9
date: "2026-03-04"
tags:
  - experiment
  - "strategy/opening-gap"
  - verdict/fail
  - wave/2
  - category/param_drift
  - market/sp500
---

# Opening Gap Exit

> **Wave:** [[Wave 2]] | **Strategy:** [[Opening Gap]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

Opening gap reversal trades should exit when IBS > 0.8-0.9 (close near high = buying exhaustion). This is more targeted than SMA(5) exit. Also test shorter hold periods since gap fills typically resolve in 1-3 days.

## Results

| Metric | Value |
|--------|-------|
| Total Trades | 9 |

## Verdict

**FAIL**

*Criteria:* Sharpe improvement >= 0.05 OR PF improvement >= 0.1 vs current OG exit. Min 80 trades after filtering.

All 2 criteria failed: sharpe_improvement: metric not found; or_pf_improvement: metric not found

## Learnings

- Only 9 trades across entire backtest — insufficient for ANY conclusion
- All max_hold_days variants produce essentially identical results (9 trades each)
- PATTERN: OG generates very few solo trades on SP500 with current filters
- SMA-200 filter + gap threshold + RSI < 25 + volume surge = very selective
- Need to relax one filter (remove RSI or lower gap threshold) for more trades before testing exits

---

Strategy:: [[Opening Gap]]
Wave:: [[Wave 2]]