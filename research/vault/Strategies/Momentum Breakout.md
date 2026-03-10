---
strategy_id: momentum_breakout
type: strategy
status: dormant
total_experiments: 6
best_sharpe: 0.2995
tags:
  - strategy
  - "strategy/momentum-breakout"
---

# Momentum Breakout

> **Status:** `DORMANT` | **Experiments:** 6 | **Promotions:** 0

## Overview

Research strategy `momentum_breakout`. See experiments below.

## Aggregate Metrics

| Metric | Value |
|--------|-------|
| Best Sharpe | 0.30 |
| Worst Sharpe | -2.72 |
| Avg Sharpe | -1.57 |
| Total Experiments | 6 |
| Pass / Partial / Fail | 2 / 3 / 1 |
| Promotions | 0 |

## Best Parameters

- `param_grid`: {'lookback_days': [10, 15, 20, 30], 'atr_stop_mult': [2.0, 2.5, 3.0, 3.5], 'trailing_stop_atr_mult': [2.0, 2.5, 3.0, 3.5], 'max_hold_days': [10, 15, 20, 25], 'trend_ma_period': [50, 100, 150, 200]}

## Experiment History

| Experiment | Wave | Verdict | Sharpe | CAGR | Promoted |
|------------|------|---------|--------|------|----------|
| [[wave1_moment_solo]] | 1 | `pass` | -0.99 | -2.55% |  |
| [[wave1_moment_opt]] | 1 | `pass` | 0.30 | 8.05% |  |
| [[wave1_moment_comb]] | 1 | `fail` | -0.16 | 1.90% |  |
| [[20260310_181024_9b3400]] | ? | `partial` | -2.72 | -0.59% |  |
| [[20260310_183205_1eac79]] | ? | `partial` | -2.72 | -0.59% |  |
| [[20260310_185801_dd443f]] | ? | `partial` | -1.75 | 1.10% |  |

## Key Learnings

- Momentum breakout generates 342 trades with 48.5% WR — sufficient signal activity for viability
- Untuned default params produce negative Sharpe (-0.99) but meet relaxed solo criteria (trades>10, WR>35%, PF>0.7)
- Strategy is viable for optimization phase — signal exists even if untuned defaults are unprofitable
- All 5 params changed during optimization
- Shorter breakout lookback (10 vs 20) works better
- Tighter stops (2.0x vs 3.5x ATR) dramatically improve results
- Longer trend filter (150 vs 50 SMA) eliminates false breakouts
- Momentum breakout solo is modestly profitable after optimization (Sharpe 0.30, CAGR 8.0%)
- But adding it to the active portfolio (MR+TF+OG) HURTS performance dramatically
- Combined Sharpe drops from 0.59 to -0.16, DD increases from 6.6% to 16.5%
- The 460 breakout trades compete with MR/TF signals for the 10 max positions
- Breakout strategy may work better with a separate position allocation
