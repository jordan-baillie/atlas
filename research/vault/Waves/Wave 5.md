---
wave: 5
status: planned
theme: Full Portfolio Reoptimization + Consecutive Down Days — Maximize Returns and Add Uncorrelated Alpha
start_date: "2026-03-08"
experiment_count: 10
tags:
  - wave
  - wave/5
---

# Wave 5: Full Portfolio Reoptimization + Consecutive Down Days — Maximize Returns and Add Uncorrelated Alpha

> **Status:** `PLANNED` | **Experiments:** 10 | **Started:** 2026-03-08

## Theme Rationale

Two tracks targeting direct profit improvement:

TRACK A (Reoptimization, P2): The SMA-200 filter was promoted in v2.1 and fundamentally 
changed the trade mix from 443 to 270 trades. ALL strategy parameters (MR/TF/OG) were 
optimized WITHOUT SMA-200 active. They are almost certainly suboptimal now. ASX 
reoptimization in Wave 1 yielded +0.17 Sharpe improvement from a similar post-filter 
reopt — this is the highest-expected-value action available.

TRACK B (Param Sweeps, P3): Fine-tune individual strategy exit/entry parameters in 
COMBINED mode (not solo — solo is misleading at $4K equity due to fee drag). Sweeping 
profit targets, trailing stops, and gap thresholds that haven't been optimized post-SMA200.

TRACK C (Allocation Pools, P2): The pool system was built (Task #52) but never tested. 
It is the ONLY mechanism that can unlock dormant strategies — confirmed 4 times across 
waves 1-4. A single filter_test validates pools don't degrade the current portfolio.

TRACK D (Consecutive Down Days, P3→P2): A genuinely new strategy based on academic 
short-term reversal research. Unlike ConnorsRSI2 and LBR (which failed because they were 
ETF strategies adapted to stocks), CDD is designed for individual large-cap stocks 
per Connors (2008) and Quantpedia research (Sharpe 1.09 on large-caps). Signal source 
(consecutive red candles) is fundamentally different from RSI-based mean reversion, 
providing uncorrelated alpha.

## Experiments

| Experiment | Verdict | Strategy | Sharpe | Promoted |
|------------|---------|----------|--------|----------|
| [[wave5_full_reopt]] | `pass` | Combined Portfolio | 0.75 |  |
| [[wave5_reopt_oos]] | — | — | — | |
| [[wave5_mr_profit_sweep]] | `partial` | Mean Reversion | 0.62 |  |
| [[wave5_tf_trail_sweep]] | `partial` | Trend Following | 0.62 |  |
| [[wave5_og_gap_sweep]] | `partial` | Opening Gap | 0.62 |  |
| [[wave5_pool_toggle]] | `fail` | Combined Portfolio | 0.62 |  |
| [[wave5_cdd_solo]] | `fail` | Consecutive Down Days | N/A |  |
| [[wave5_cdd_opt]] | — | — | — | |
| [[wave5_cdd_combined]] | — | — | — | |
| [[wave5_cdd_oos]] | — | — | — | |
