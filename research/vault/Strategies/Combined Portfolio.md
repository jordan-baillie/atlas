---
strategy_id: combined
type: strategy
status: filter
total_experiments: 3
best_sharpe: 0.7486
tags:
  - strategy
  - strategy/combined
---

# Combined Portfolio

> **Status:** `FILTER` | **Experiments:** 3 | **Promotions:** 0

## Overview

Combined portfolio (MR + TF + OG + additional strategies). Baseline: Sharpe 0.59 (v2.0). With SMA-200 filter: Sharpe 0.87 (v2.1).

## Aggregate Metrics

| Metric | Value |
|--------|-------|
| Best Sharpe | 0.75 |
| Worst Sharpe | 0.62 |
| Avg Sharpe | 0.68 |
| Total Experiments | 3 |
| Pass / Partial / Fail | 1 / 0 / 2 |
| Promotions | 0 |

## Experiments

| Experiment | Wave | Verdict | Sharpe | CAGR | Promoted |
|------------|------|---------|--------|------|----------|
| [[wave1_vix_filter]] | 1 | `fail` | N/A | N/A% |  |
| [[wave5_full_reopt]] | 5 | `pass` | 0.75 | 38.14% |  |
| [[wave5_pool_toggle]] | 5 | `fail` | 0.62 | 29.52% |  |
