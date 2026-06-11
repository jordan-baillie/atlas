# opening_gap

> **Status:** active | **Best Sharpe:** 0.4926 | **Trades:** 714
> **Updated:** 2026-06-07 13:16

## Current Best Params

| Parameter | Value |
|-----------|-------|
| atr_period | 28 |
| atr_stop_mult | 1.0 |
| gap_threshold | -0.0 |
| ibs_confirm | 0.6 |
| ibs_exit_threshold | 0.8 |
| max_hold_days | 12 |
| rsi14_max | 44 |
| sma200_filter | False |
| sma_exit_period | 7 |
| vol_surge_threshold | 0.68 |

## Current Metrics

| Metric | Value |
|--------|-------|
| Sharpe | 0.4926 |
| CAGR | 10.2% |
| Profit Factor | 1.30 |
| Max Drawdown | 12.4% |
| Total Trades | 714 |
| Win Rate | 63.0% |

## History

| Date | Sharpe | Trades | PF | CAGR | Change |
|------|--------|--------|----|------|--------|
| Date | Sharpe | Trades | PF | CAGR | Change |
| Date | Sharpe | Trades | PF | CAGR | Change |
| Date | Sharpe | Trades | PF | CAGR | Change |
| Date | Sharpe | Trades | PF | CAGR | Change |
| Date | Sharpe | Trades | PF | CAGR | Change |
| Date | Sharpe | Trades | PF | CAGR | Change |
| Date | Sharpe | Trades | PF | CAGR | Change |
| Date | Sharpe | Trades | PF | CAGR | Change |
| 2026-03-12 02:55 | 0.3396 | 257 | 2.59 | 11.5% | migrated from best/opening_gap.json |
| 2026-04-22 13:15 | 0.0880 | 1089 | 1.08 | 4.7% | gap_threshold: -0.008 → -0.0 |
| 2026-04-22 13:15 | 0.0880 | 1089 | 1.08 | 4.7% | autoresearch_runner keep: gap_threshold=-0.0 |
| 2026-04-22 13:52 | 0.1162 | 1069 | 1.09 | 5.3% | rsi14_max: 35 → 44 |
| 2026-04-22 13:52 | 0.1162 | 1069 | 1.09 | 5.3% | autoresearch_runner keep: rsi14_max=44 |
| 2026-04-23 13:15 | 0.1162 | 1069 | 1.09 | 5.3% | ibs_confirm: 0.7 → 0.77 |
| 2026-04-23 13:15 | 0.1162 | 1069 | 1.09 | 5.3% | autoresearch_runner keep: ibs_confirm=0.77 |
| 2026-04-24 13:17 | 0.0989 | 1071 | 1.09 | 5.0% | ibs_confirm: 0.77 → 0.58 |
| 2026-04-24 13:17 | 0.0989 | 1071 | 1.09 | 5.0% | autoresearch_runner keep: ibs_confirm=0.58 |
| 2026-04-25 13:23 | 0.0989 | 1071 | 1.09 | 5.0% | vol_surge_threshold: 1.5 → 1.35 |
| 2026-04-25 13:23 | 0.0989 | 1071 | 1.09 | 5.0% | autoresearch_runner keep: vol_surge_threshold=1.35 |
| 2026-04-26 13:27 | 0.0989 | 1071 | 1.09 | 5.0% | vol_surge_threshold: 1.35 → 0.68 |
| 2026-04-26 13:27 | 0.0989 | 1071 | 1.09 | 5.0% | autoresearch_runner keep: vol_surge_threshold=0.68 |
| 2026-04-27 13:17 | 0.0989 | 1071 | 1.09 | 5.0% | ibs_confirm: 0.58 → 0.52 |
| 2026-04-27 13:17 | 0.0989 | 1071 | 1.09 | 5.0% | autoresearch_runner keep: ibs_confirm=0.52 |
| 2026-04-28 13:44 | 0.6016 | 1106 | 1.26 | 18.7% | atr_period: 25 → 28 |
| 2026-04-28 13:44 | 0.6016 | 1106 | 1.26 | 18.7% | autoresearch_runner keep: atr_period=28 |
| 2026-06-04 13:15 | 0.2375 | 693 | 1.19 | 7.1% | max_hold_days 10->15 longer hold |
| 2026-06-04 13:17 | 0.2375 | 693 | 1.19 | 7.1% | max_hold_days 15->20 even longer hold |
| 2026-06-04 13:18 | 0.2634 | 691 | 1.20 | 7.3% | max_hold_days 15->12 (bracket the optimum) |
| 2026-06-07 13:13 | 0.4926 | 714 | 1.30 | 10.2% | ibs_confirm 0.52->0.60 tighter confirm (untried) |
| 2026-06-07 13:16 | 0.4926 | 714 | 1.30 | 10.2% | ibs_confirm 0.60 (re-apply prior win as new base) |
