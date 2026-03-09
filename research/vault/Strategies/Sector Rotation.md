---
strategy_id: sector_rotation
type: strategy
status: dormant
total_experiments: 3
best_sharpe: 0.43
tags:
  - strategy
  - "strategy/sector-rotation"
---

# Sector Rotation

> **Status:** `DORMANT` | **Experiments:** 3 | **Promotions:** 0

## Overview

Top-down momentum sector rotation. Selects strongest GICS sectors by momentum, buys strongest stocks within them. Passes solo (Sharpe 0.43) but degrades combined portfolio.

## Aggregate Metrics

| Metric | Value |
|--------|-------|
| Best Sharpe | 0.43 |
| Worst Sharpe | -0.11 |
| Avg Sharpe | 0.16 |
| Total Experiments | 3 |
| Pass / Partial / Fail | 1 / 1 / 1 |
| Promotions | 0 |

## Experiments

| Experiment | Wave | Verdict | Sharpe | CAGR | Promoted |
|------------|------|---------|--------|------|----------|
| [[wave1_sector_solo]] | 1 | `partial` | -0.11 | 3.25% |  |
| [[wave1_sector_opt]] | 1 | `pass` | 0.43 | 9.61% |  |
| [[wave1_sector_comb]] | 1 | `fail` | 0.55 | 11.06% |  |
