---
wave: 2
status: planned
theme: Enhanced Mean Reversion Alpha — Connors RSI(2) strategy + volume filter promotion + exit optimization
start_date: "2026-03-03"
experiment_count: 10
tags:
  - wave
  - wave/2
---

# Wave 2: Enhanced Mean Reversion Alpha — Connors RSI(2) strategy + volume filter promotion + exit optimization

> **Status:** `PLANNED` | **Experiments:** 10 | **Started:** 2026-03-03

## Theme Rationale

Wave 1 proved mean reversion is our strongest alpha source: SMA-200 filter promoted (Sharpe +0.28), volume 1.5x filter showed massive MR solo improvement (Sharpe -0.02→0.38). Wave 2 doubles down by: (A) Adding Connors RSI(2) — a new MR strategy with 34-year published track record, distinct entry/exit signals from existing MR (RSI-14/z-score), providing uncorrelated short-term alpha; (B) Promoting the proven 1.5x volume filter to the combined portfolio; (C) Optimizing exits across all 3 active strategies using research-backed approaches (RSI recovery, IBS exhaustion, Chandelier stops); (D) Testing Turn of Month calendar anomaly as an exploratory signal filter. Every experiment has a direct path to improving live P&L via higher Sharpe, CAGR, or lower drawdown.

## Experiments

| Experiment | Verdict | Strategy | Sharpe | Promoted |
|------------|---------|----------|--------|----------|
| [[wave2_vol_combined]] | `fail` | Portfolio Filter | -1.04 |  |
| [[wave2_rsi2_solo]] | `fail` | ConnorsRSI2 | -2.63 |  |
| [[wave2_rsi2_opt]] | — | — | — | |
| [[wave2_rsi2_combined]] | — | — | — | |
| [[wave2_rsi2_oos]] | — | — | — | |
| [[wave2_vol_promotion]] | — | — | — | |
| [[wave2_exit_mr]] | `fail` | Mean Reversion | N/A |  |
| [[wave2_exit_og]] | `fail` | Opening Gap | N/A |  |
| [[wave2_chandelier_tf]] | `fail` | Trend Following | N/A |  |
| [[wave2_tom_filter]] | `partial` | Portfolio Filter | -0.64 |  |
