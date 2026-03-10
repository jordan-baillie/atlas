---
experiment_id: wave5_cdd_solo
wave: 5
strategy: consecutive_down_days
category: new_strategy
market: sp500
verdict: fail
promoted: false
date: "2026-03-10"
tags:
  - experiment
  - "strategy/consecutive-down-days"
  - verdict/fail
  - wave/5
  - category/new_strategy
  - market/sp500
---

# Cdd Solo

> **Wave:** [[Wave 5]] | **Strategy:** [[Consecutive Down Days]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

Buying large-cap stocks after 3+ consecutive down closes in uptrends (above SMA-200) captures short-term reversal premium. Academic research (Quantpedia, Groot et al. 2012) shows this effect generates 30-50 bps/week net of costs on large caps (Sharpe ~1.09 in published results). Signal is fundamentally different from RSI-based MR — counts close-to-close direction rather than oscillator levels.

## Results

*No metrics recorded for this experiment.*

## Verdict

**FAIL**

Code error: ConsecutiveDownDays class missing check_exits() abstract method implementation. The strategy was coded but incomplete — needs the exit logic method before it can be tested.

## Learnings

- CDD strategy missing check_exits() abstract method — incomplete implementation
- Fix code before re-queuing
- CDD strategy implementation is incomplete — missing check_exits() abstract method
- Need to implement check_exits() before re-queuing CDD experiments
- Strategy base class requires check_exits() — all strategies must implement it

---

Strategy:: [[Consecutive Down Days]]
Wave:: [[Wave 5]]