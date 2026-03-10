---
strategy_id: trend_following
type: strategy
status: active
total_experiments: 2
best_sharpe: 0.62
tags:
  - strategy
  - "strategy/trend-following"
---

# Trend Following

> **Status:** `ACTIVE` | **Experiments:** 2 | **Promotions:** 0

## Overview

Fast/slow MA crossover trend following with pullback entries. Core active strategy. Enters higher-probability pullbacks within confirmed uptrends.

## Aggregate Metrics

| Metric | Value |
|--------|-------|
| Best Sharpe | 0.62 |
| Worst Sharpe | 0.62 |
| Avg Sharpe | 0.62 |
| Total Experiments | 2 |
| Pass / Partial / Fail | 0 / 1 / 1 |
| Promotions | 0 |

## Experiments

| Experiment | Wave | Verdict | Sharpe | CAGR | Promoted |
|------------|------|---------|--------|------|----------|
| [[wave2_chandelier_tf]] | 2 | `fail` | N/A | N/A% |  |
| [[wave5_tf_trail_sweep]] | 5 | `partial` | 0.62 | N/A% |  |
