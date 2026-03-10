---
strategy_id: bb_squeeze
type: strategy
status: dormant
total_experiments: 6
best_sharpe: 0.3981
tags:
  - strategy
  - "strategy/bb-squeeze"
---

# Bollinger Band Squeeze

> **Status:** `DORMANT` | **Experiments:** 6 | **Promotions:** 0

## Overview

Research strategy `bb_squeeze`. See experiments below.

## Aggregate Metrics

| Metric | Value |
|--------|-------|
| Best Sharpe | 0.40 |
| Worst Sharpe | -1.68 |
| Avg Sharpe | -0.79 |
| Total Experiments | 6 |
| Pass / Partial / Fail | 2 / 4 / 0 |
| Promotions | 0 |

## Experiment History

| Experiment | Wave | Verdict | Sharpe | CAGR | Promoted |
|------------|------|---------|--------|------|----------|
| [[wave1_bb_squ_solo]] | 1 | `pass` | -1.68 | -12.27% |  |
| [[wave1_bb_squ_opt]] | 1 | `partial` | -0.38 | -0.37% |  |
| [[wave1_bb_squ_solo]] | 1 | `pass` | -1.68 | -12.27% |  |
| [[wave1_bb_squ_opt]] | 1 | `partial` | -0.38 | -0.37% |  |
| [[20260310_181024_f7508b]] | ? | `partial` | -1.05 | 0.82% |  |
| [[20260310_185801_5ee089]] | ? | `partial` | 0.40 | 41.65% |  |

## Key Learnings

- BB Squeeze is viable: 322 trades, 45% WR, PF 0.74 with default params
- Clearly unprofitable untuned (Sharpe -1.68, CAGR -12.3%) but signal generates enough trades
- Passed to optimization phase
- BB Squeeze improved dramatically: Sharpe -1.68 → -0.38, PF 0.74 → 1.04
- Best params: bb_period=25, bb_std=1.5 (both changed from defaults)
- But PF 1.04 still below 1.1 threshold, Sharpe still negative
- Near breakeven after optimization is not good enough for portfolio addition
- BB Squeeze on SP500 with current implementation is likely not viable
- PATTERN: All 3 dormant strategies tried so far are individually marginal after optimization
