---
wave: 1
status: complete
theme: "Dormant Strategy Activation & Portfolio Filters"
start_date: "2026-02-27"
experiment_count: 25
tags:
  - wave
  - wave/1
---

# Wave 1: Dormant Strategy Activation & Portfolio Filters

> **Status:** `COMPLETE` | **Experiments:** 25 | **Started:** 2026-02-27

## Theme Rationale

Activate all coded-but-disabled strategies on SP500 and test portfolio-wide filters (VIX, volume) to improve risk-adjusted returns. Systematic evaluation of what's already built before developing new strategies.

## Experiments

| Experiment | Verdict | Strategy | Sharpe | Promoted |
|------------|---------|----------|--------|----------|
| [[wave1_moment_solo]] | `pass` | Momentum Breakout | -0.99 |  |
| [[wave1_moment_opt]] | `pass` | Momentum Breakout | 0.30 |  |
| [[wave1_moment_comb]] | `fail` | Momentum Breakout | -0.16 |  |
| [[wave1_moment_oos]] | — | — | — | |
| [[wave1_short__solo]] | `pass` | Short Term MR | -0.45 |  |
| [[wave1_short__opt]] | `pass` | Short Term MR | 0.27 |  |
| [[wave1_short__comb]] | `fail` | Short Term MR | 0.30 |  |
| [[wave1_short__oos]] | — | — | — | |
| [[wave1_sector_solo]] | `partial` | Sector Rotation | -0.11 |  |
| [[wave1_sector_opt]] | `pass` | Sector Rotation | 0.43 |  |
| [[wave1_sector_comb]] | `fail` | Sector Rotation | 0.55 |  |
| [[wave1_sector_oos]] | — | — | — | |
| [[wave1_mtf_mo_solo]] | `fail` | MTF Momentum | N/A |  |
| [[wave1_mtf_mo_opt]] | — | — | — | |
| [[wave1_mtf_mo_comb]] | — | — | — | |
| [[wave1_mtf_mo_oos]] | — | — | — | |
| [[wave1_bb_squ_solo]] | `pass` | Bollinger Band Squeeze | -1.68 |  |
| [[wave1_bb_squ_opt]] | `partial` | Bollinger Band Squeeze | -0.38 |  |
| [[wave1_bb_squ_comb]] | — | — | — | |
| [[wave1_bb_squ_oos]] | — | — | — | |
| [[wave1_asx_reopt]] | `promoted` | Portfolio Filter | N/A | ✅ |
| [[wave1_vix_filter]] | `fail` | Combined Portfolio | N/A |  |
| [[wave1_vol_filter]] | `pass` | Mean Reversion | N/A |  |
| [[wave1_cross_mkt]] | `promoted` | SMA-200 Filter | 0.87 | ✅ |
| [[wave1_sma200]] | `promoted` | Portfolio Filter | N/A | ✅ |

## Key Findings

- momentum_breakout: profitable solo (Sharpe 0.30, CAGR 8.0%) but degrades portfolio combined (Sharpe 0.59→-0.16) due to 460 trades competing for 10 positions
- short_term_mr: profitable solo (Sharpe 0.27, CAGR 7.6%) but degrades portfolio combined (Sharpe 0.59→0.30) due to 697 trades competing for 10 positions
- Position allocation is the critical bottleneck — 10-position limit causes signal competition when adding strategies
- Solo test criteria relaxed: min_trades=10, WR>35%, PF>0.7 (viability check, not profitability check)
- Coord descent optimizer works for single-strategy parameter tuning
