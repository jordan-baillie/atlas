---
experiment_id: wave5_pool_toggle
wave: 5
strategy: combined
category: portfolio
market: sp500
verdict: fail
promoted: false
sharpe: 0.6183
cagr: 29.52
max_drawdown: 3.81
total_trades: 101
date: "2026-03-10"
tags:
  - experiment
  - strategy/combined
  - verdict/fail
  - wave/5
  - category/portfolio
  - market/sp500
---

# Pool Toggle

> **Wave:** [[Wave 5]] | **Strategy:** [[Combined Portfolio]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

Enabling allocation pools (TF:5, MR:5, OG:3) with current 3 active strategies should produce results within 5% of no-pools baseline. Pool totals (13) are under max_positions (15), so no crowding. This validates the pool system works correctly before adding strategies.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | 0.62 |
| CAGR | 29.52% |
| Max Drawdown | 3.81% |
| Total Trades | 101 |

## Verdict

**FAIL**

Allocation pools produced identical results to baseline (pools_off). With only 3 active strategies (MR/TF/OG) and max_positions=15, there's no position contention to resolve — pools are only useful when 4+ strategies compete for limited slots. Edge p-value 0.33 — no statistical significance. MC fragile.

## Delta vs Baseline

| Metric | Change |
|--------|--------|
| Sharpe | 0.00 |
| Cagr Pct | 0.00 |

## Learnings

- Pools are no-op with 3 strategies + 15 max positions — no contention
- Pools only matter when 4+ strategies compete for limited slots
- Allocation pools are a no-op with only 3 strategies and max_positions=15 — no contention to resolve
- Pools will matter when dormant strategies are activated (4+ strategies competing for 15 slots)
- Don't test infrastructure features without the prerequisite condition (more strategies than slots)

---

Strategy:: [[Combined Portfolio]]
Wave:: [[Wave 5]]