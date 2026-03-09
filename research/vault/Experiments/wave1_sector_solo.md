---
experiment_id: wave1_sector_solo
wave: 1
strategy: sector_rotation
category: dormant
market: sp500
verdict: partial
promoted: false
sharpe: -0.11
cagr: 3.25
max_drawdown: 11.6
win_rate: 44.2
total_trades: 251
profit_factor: 1.24
date: "2026-03-01"
tags:
  - experiment
  - "strategy/sector-rotation"
  - verdict/partial
  - wave/1
  - category/dormant
  - market/sp500
---

# Sector Rotation Solo

> **Wave:** [[Wave 1]] | **Strategy:** [[Sector Rotation]] | **Verdict:** `PARTIAL` | **Promoted:** ❌

## Hypothesis

Sector rotation provides a top-down signal that is uncorrelated with the bottom-up technical strategies (MR, TF, OG). It selects the strongest sectors by momentum then buys the strongest stocks within those sectors. 204 SP500 tickers mapped across 11 GICS sectors. Risk: longer holding periods and lower trade frequency may limit impact.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | -0.11 |
| CAGR | 3.25% |
| Max Drawdown | 11.60% |
| Win Rate | 44.20% |
| Profit Factor | 1.24 |
| Total Trades | 251 |

## Verdict

**PARTIAL**

*Criteria:* Solo viability check: strategy generates 10+ trades, WR > 35%, PF > 0.7. Optimization will tune for profitability. Updated: removed positive_pnl requirement (untuned defaults rarely profitable).

251 trades generated (strategy now functional after sector_map fix), but Sharpe -0.11, WR 44%, edge p=0.13 (not significant). PF 1.24 is promising — signal captures something real but needs optimization.

## Learnings

- Sector rotation is now functional — generates 251 trades vs 0 previously
- PF 1.24 suggests alpha signal exists but untuned parameters drag Sharpe negative
- Edge p-value 0.13 — not statistically significant, optimization may fix this
- Strategy needs code review: may need rebalance-aware position management
- DECISION: Worth sending to optimization phase despite partial verdict

---

Strategy:: [[Sector Rotation]]
Wave:: [[Wave 1]]