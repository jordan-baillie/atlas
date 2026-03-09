---
strategy_id: momentum_breakout
type: strategy
status: dormant
total_experiments: 3
best_sharpe: 0.2995
tags:
  - strategy
  - "strategy/momentum-breakout"
---

# Momentum Breakout

> **Status:** `DORMANT` | **Experiments:** 3 | **Promotions:** 0

## Overview

N-day high breakout with trend MA alignment. Passes solo tests (Sharpe 0.30, CAGR 8.0%) after optimization. Degrades combined portfolio due to position slot contention.

## Aggregate Metrics

| Metric | Value |
|--------|-------|
| Best Sharpe | 0.30 |
| Worst Sharpe | -0.99 |
| Avg Sharpe | -0.35 |
| Total Experiments | 3 |
| Pass / Partial / Fail | 2 / 0 / 1 |
| Promotions | 0 |

## Experiments

| Experiment | Wave | Verdict | Sharpe | CAGR | Promoted |
|------------|------|---------|--------|------|----------|
| [[wave1_moment_solo]] | 1 | `pass` | -0.99 | -2.55% |  |
| [[wave1_moment_opt]] | 1 | `pass` | 0.30 | 8.05% |  |
| [[wave1_moment_comb]] | 1 | `fail` | -0.16 | 1.90% |  |
