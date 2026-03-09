---
strategy_id: short_term_mr
type: strategy
status: dormant
total_experiments: 3
best_sharpe: 0.2711
tags:
  - strategy
  - "strategy/short-term-mr"
---

# Short Term MR

> **Status:** `DORMANT` | **Experiments:** 3 | **Promotions:** 0

## Overview

RSI(2)/IBS rapid 1-5 day reversion strategy (Connors-style). Passes solo tests (Sharpe 0.27, CAGR 7.6%, 63% WR) after optimization. Degrades combined portfolio due to position slot contention.

## Aggregate Metrics

| Metric | Value |
|--------|-------|
| Best Sharpe | 0.27 |
| Worst Sharpe | -0.45 |
| Avg Sharpe | -0.09 |
| Total Experiments | 3 |
| Pass / Partial / Fail | 2 / 0 / 1 |
| Promotions | 0 |

## Experiments

| Experiment | Wave | Verdict | Sharpe | CAGR | Promoted |
|------------|------|---------|--------|------|----------|
| [[wave1_short__solo]] | 1 | `pass` | -0.45 | -1.67% |  |
| [[wave1_short__opt]] | 1 | `pass` | 0.27 | 7.65% |  |
| [[wave1_short__comb]] | 1 | `fail` | 0.30 | 7.69% |  |
