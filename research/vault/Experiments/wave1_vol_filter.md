---
experiment_id: wave1_vol_filter
wave: 1
strategy: mean_reversion
category: filter
market: sp500
verdict: pass
promoted: false
date: "2026-03-01"
tags:
  - experiment
  - "strategy/mean-reversion"
  - verdict/pass
  - wave/1
  - category/filter
  - market/sp500
---

# Volume Filter

> **Wave:** [[Wave 1]] | **Strategy:** [[Mean Reversion]] | **Verdict:** `PASS` | **Promoted:** ❌

## Hypothesis

Only entering trades when daily volume exceeds the N-day average improves signal quality by filtering out low-liquidity, low-conviction price moves. Higher volume on entry day = more institutional participation = higher probability of follow-through.

## Results

*No metrics recorded for this experiment.*

## Verdict

**PASS**

*Criteria:* Win rate improvement >= 2pp OR Sharpe improvement >= 0.02. Min 200 trades to avoid over-filtering.

Volume filter 1.5x avg on Mean Reversion shows dramatic improvement: Sharpe -0.02 → 0.38 (+0.40!), PF 1.30 → 1.62, DD 5.24% → 4.03%, WR 59% → 60%. Trades drop from 332 to 235 (still healthy). 2x is too aggressive (only 115 trades). 1.5x is the sweet spot. Needs combined portfolio test to confirm portfolio-level impact.

## Delta vs Baseline

| Metric | Change |
|--------|--------|
| Sharpe | 0.40 |
| Profit Factor | 0.32 |
| Max Drawdown Pct | -1.21 |

## Learnings

- 1.5x avg volume filter on MR: Sharpe jumps from -0.02 to 0.38 (massive)
- Mechanism: higher volume entries = more institutional participation = better follow-through
- PF 1.30 → 1.62 (23% improvement), DD 5.24% → 4.03% (1.2pp reduction)
- Trade count 332 → 235 (29% reduction) — acceptable for the quality improvement
- 2x avg is too aggressive: only 115 trades, Sharpe drops to -0.30
- 0.5x/0.8x/1.0x show minimal improvement — 1.5x is the threshold where quality jumps
- NEXT: Test 1.5x volume filter on combined portfolio (all strategies)

---

Strategy:: [[Mean Reversion]]
Wave:: [[Wave 1]]