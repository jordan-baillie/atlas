---
strategy_id: mtf_momentum
type: strategy
status: dormant
total_experiments: 7
best_sharpe: 0.0
tags:
  - strategy
  - "strategy/mtf-momentum"
---

# MTF Momentum

> **Status:** `DORMANT` | **Experiments:** 7 | **Promotions:** 0

## Overview

Research strategy `mtf_momentum`. See experiments below.

## Aggregate Metrics

| Metric | Value |
|--------|-------|
| Best Sharpe | 0.00 |
| Worst Sharpe | -2.34 |
| Avg Sharpe | -0.39 |
| Total Experiments | 7 |
| Pass / Partial / Fail | 0 / 1 / 6 |
| Promotions | 0 |

## Experiment History

| Experiment | Wave | Verdict | Sharpe | CAGR | Promoted |
|------------|------|---------|--------|------|----------|
| [[wave1_mtf_mo_solo]] | 1 | `fail` | N/A | N/A% |  |
| [[wave1_mtf_mo_solo]] | 1 | `fail` | N/A | N/A% |  |
| [[wave1_mtf_mo_solo]] | 1 | `fail` | N/A | N/A% |  |
| [[wave1_mtf_mo_solo]] | 1 | `fail` | N/A | N/A% |  |
| [[wave1_mtf_mo_solo]] | 1 | `fail` | N/A | N/A% |  |
| [[wave1_mtf_mo_solo]] | 1 | `fail` | N/A | N/A% |  |
| [[20260310_181024_d2d7b3]] | ? | `partial` | -2.34 | 0.19% |  |

## Key Learnings

- MTF Momentum has code bug: generate_signals() takes 3 positional arguments but 4 were given
- Strategy needs API signature fix before retesting
- Likely needs to accept the new market_breadth parameter added to generate_signals()
- calc_atr() call fixed (was passing df instead of df['high'],df['low'],df['close'])
- Second bug: Series comparison ambiguity in signal generation logic
- Strategy needs full code audit — multiple comparison operators likely use raw Series
- PATTERN: Dormant strategies have accumulated API drift bugs
- DO NOT re-queue until code is audited and unit-tested
- ROOT CAUSE: confidence=0.50 hardcoded in mtf_momentum.py, filtered by min_confidence=0.75 in config
- 3rd failure — first 2 were API signature bugs (fixed), this is the remaining blocker
- Strategy generates hundreds of signals but none pass confidence gate
- FIX: Add dynamic confidence scoring or per-strategy min_confidence override
- BLOCKED: Do not re-queue until confidence calculation is implemented
