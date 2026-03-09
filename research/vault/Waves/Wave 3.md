---
wave: 3
status: planned
theme: "New strategy: Triple RSI + MR alpha stacking — high-conviction signals and entry filter optimization"
start_date: "2026-03-04"
experiment_count: 10
tags:
  - wave
  - wave/3
---

# Wave 3: New strategy: Triple RSI + MR alpha stacking — high-conviction signals and entry filter optimization

> **Status:** `PLANNED` | **Experiments:** 10 | **Started:** 2026-03-04

## Theme Rationale

Wave 3 targets three compounding profit improvements:

1. VOLUME FILTER PROMOTION (known alpha, unpromoted): Wave 1 proved 1.5x volume on MR solo:
   Sharpe -0.02→0.38, PF 1.30→1.62. Wave 2 combined test FAILED due to infrastructure bug
   (nested params in filter_test). This wave uses full-dict param sweep to bypass the bug.

2. IBS ENTRY FILTER (web research, new): Alvarez Quant Trading and Pagonidis (2013) showed
   IBS < 0.25 gives 58% improvement in avg gain per trade for RSI-based MR strategies.
   Our MR already has ibs_max parameter at 1.0 (disabled). Simply lowering it could
   capture significant alpha with zero code changes.

3. NEW TRIPLE RSI STRATEGY (web research, implemented): QuantifiedStrategies.com published
   a Triple RSI strategy with 90% win rate, PF 4.0, 1.2% avg gain on SPY. Adapted for
   individual SP500 stocks with SMA-200 filter and volume confirmation. Uses RSI(5) with
   3-day consecutive decline requirement — a DIFFERENT signal from our RSI(14)+zscore MR.
   Low trade count (~30-80/year) means minimal position contention with existing strategies.

Why this theme beats alternatives:
- NOT "dormant strategy activation" — lesson learned: all 4 dormant strategies failed
  combined test due to position contention. Triple RSI avoids this with low trade volume.
- NOT "exit optimization" — Wave 2 solo exit sweeps all showed negative Sharpe at $4K equity.
  Combined-mode param sweeps in Phase 4 address this properly.
- NOT "allocation pools" — infrastructure change deferred until proven strategies exist
  that NEED pools. Triple RSI + filter stacking may add enough alpha without pools.

## Experiments

| Experiment | Verdict | Strategy | Sharpe | Promoted |
|------------|---------|----------|--------|----------|
| [[wave3_ibs_sweep]] | `fail` | Mean Reversion | 0.61 |  |
| [[wave3_vol_sweep]] | `fail` | Mean Reversion | 0.61 |  |
| [[wave3_trsi_solo]] | `fail` | Triple RSI | -2.12 |  |
| [[wave3_trsi_opt]] | — | — | — | |
| [[wave3_trsi_comb]] | — | — | — | |
| [[wave3_stacked_mr]] | — | — | — | |
| [[wave3_full_reopt]] | — | — | — | |
| [[wave3_oos_val]] | — | — | — | |
| [[wave3_rsi_period]] | `fail` | Mean Reversion | 0.61 |  |
| [[wave3_hold_combined]] | `partial` | Mean Reversion | 0.64 |  |
