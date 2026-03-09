---
strategy_id: portfolio_filter
type: strategy
status: filter
total_experiments: 4
best_sharpe: -0.6446
tags:
  - strategy
  - "strategy/portfolio-filter"
---

# Portfolio Filter

> **Status:** `FILTER` | **Experiments:** 4 | **Promotions:** 2

## Overview

Portfolio-level filter experiments (VIX, volume, cross-market, turn-of-month). Tests portfolio-wide regime or entry filters.

## Aggregate Metrics

| Metric | Value |
|--------|-------|
| Best Sharpe | -0.64 |
| Worst Sharpe | -1.04 |
| Avg Sharpe | -0.84 |
| Total Experiments | 4 |
| Pass / Partial / Fail | 0 / 1 / 1 |
| Promotions | 2 |

## Experiments

| Experiment | Wave | Verdict | Sharpe | CAGR | Promoted |
|------------|------|---------|--------|------|----------|
| [[wave1_asx_reopt]] | 1 | `promoted` | N/A | N/A% | ✅ |
| [[wave1_sma200]] | 1 | `promoted` | N/A | N/A% | ✅ |
| [[wave2_tom_filter]] | 2 | `partial` | -0.64 | 2.87% |  |
| [[wave2_vol_combined]] | 2 | `fail` | -1.04 | 1.36% |  |
