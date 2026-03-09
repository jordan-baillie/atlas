---
strategy_id: lower_band_reversion
type: strategy
status: dormant
total_experiments: 5
best_sharpe: 0.707
tags:
  - strategy
  - "strategy/lower-band-reversion"
---

# Lower Band Reversion

> **Status:** `DORMANT` | **Experiments:** 5 | **Promotions:** 0

## Overview

IBS-based lower band reversion (Quantitativo LBR). Published Sharpe 2.11 on SPY. On individual stocks: Sharpe -2.08. Classic ETF-to-stock adaptation failure.

## Aggregate Metrics

| Metric | Value |
|--------|-------|
| Best Sharpe | 0.71 |
| Worst Sharpe | -2.08 |
| Avg Sharpe | -1.11 |
| Total Experiments | 5 |
| Pass / Partial / Fail | 1 / 2 / 2 |
| Promotions | 0 |

## Experiments

| Experiment | Wave | Verdict | Sharpe | CAGR | Promoted |
|------------|------|---------|--------|------|----------|
| [[wave4_lbr_solo]] | 4 | `partial` | -2.08 | -2.60% |  |
| [[wave4_lbr_solo_relaxed]] | 4 | `partial` | -1.85 | -1.53% |  |
| [[wave4_lbr_band_sweep]] | 4 | `fail` | 0.71 | 148.60% |  |
| [[wave4_lbr_ibs_sweep]] | 4 | `fail` | -1.21 | 0.60% |  |
| [[wave4_lbr_no_sma200]] | 4 | `pass` | N/A | N/A% |  |
