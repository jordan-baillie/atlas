---
strategy_id: mean_reversion
type: strategy
status: active
total_experiments: 9
best_sharpe: 0.6424
tags:
  - strategy
  - "strategy/mean-reversion"
---

# Mean Reversion

> **Status:** `ACTIVE` | **Experiments:** 9 | **Promotions:** 0

## Overview

RSI(14) + z-score mean reversion on individual SP500 stocks. Core active strategy. Enters oversold reversals with ATR-based stop losses. Optimized via coordinate descent to Sharpe 1.04, CAGR 15.69% (v2.0).

## Aggregate Metrics

| Metric | Value |
|--------|-------|
| Best Sharpe | 0.64 |
| Worst Sharpe | -2.10 |
| Avg Sharpe | 0.16 |
| Total Experiments | 9 |
| Pass / Partial / Fail | 1 / 2 / 6 |
| Promotions | 0 |

## Experiments

| Experiment | Wave | Verdict | Sharpe | CAGR | Promoted |
|------------|------|---------|--------|------|----------|
| [[wave1_vol_filter]] | 1 | `pass` | N/A | N/A% |  |
| [[wave2_exit_mr]] | 2 | `fail` | N/A | N/A% |  |
| [[wave3_ibs_sweep]] | 3 | `fail` | 0.61 | N/A% |  |
| [[wave3_vol_sweep]] | 3 | `fail` | 0.61 | N/A% |  |
| [[wave3_rsi_period]] | 3 | `fail` | 0.61 | N/A% |  |
| [[wave3_hold_combined]] | 3 | `partial` | 0.64 | 30.86% |  |
| [[wave4_mr_hold5_oos]] | 4 | `fail` | N/A | N/A% |  |
| [[wave4_mr_strength_exit]] | 4 | `fail` | -2.10 | 0.37% |  |
| [[wave5_mr_profit_sweep]] | 5 | `partial` | 0.62 | N/A% |  |
