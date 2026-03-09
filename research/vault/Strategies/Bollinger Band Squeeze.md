---
strategy_id: bb_squeeze
type: strategy
status: dormant
total_experiments: 2
best_sharpe: -0.38
tags:
  - strategy
  - "strategy/bb-squeeze"
---

# Bollinger Band Squeeze

> **Status:** `DORMANT` | **Experiments:** 2 | **Promotions:** 0

## Overview

Bollinger Band Squeeze (BB inside Keltner Channel). Identifies low-volatility regimes preceding explosive directional moves. Marginally viable after optimization; not currently in active portfolio.

## Aggregate Metrics

| Metric | Value |
|--------|-------|
| Best Sharpe | -0.38 |
| Worst Sharpe | -1.68 |
| Avg Sharpe | -1.03 |
| Total Experiments | 2 |
| Pass / Partial / Fail | 1 / 1 / 0 |
| Promotions | 0 |

## Experiments

| Experiment | Wave | Verdict | Sharpe | CAGR | Promoted |
|------------|------|---------|--------|------|----------|
| [[wave1_bb_squ_solo]] | 1 | `pass` | -1.68 | -12.27% |  |
| [[wave1_bb_squ_opt]] | 1 | `partial` | -0.38 | -0.37% |  |
