# momentum_breakout

> **Status:** active | **Best Sharpe:** 1.0101 | **Trades:** 490
> **Updated:** 2026-05-28 13:49

## Current Best Params

| Parameter | Value |
|-----------|-------|
| atr_period | 22 |
| atr_stop_mult | 0.81 |
| breakout_period | 10 |
| lookback_days | 22 |
| max_hold_days | 15 |
| profit_target_atr_mult | 2.2 |
| trend_ma_period | 30 |

## Current Metrics

| Metric | Value |
|--------|-------|
| Sharpe | 1.0101 |
| CAGR | 23.1% |
| Profit Factor | 1.36 |
| Max Drawdown | 23.1% |
| Total Trades | 490 |
| Win Rate | 43.1% |

## History

| Date | Sharpe | Trades | PF | CAGR | Change |
|------|--------|--------|----|------|--------|
| 2026-05-14 07:44 | 1.2042 | 597 | 1.60 | 24.3% | autoresearch_runner keep: trend_ma_period=20 |
| 2026-05-14 07:45 | 1.2645 | 640 | 1.60 | 25.5% | trend_ma_period: 27 → 40 |
| 2026-05-14 07:45 | 1.2645 | 640 | 1.60 | 25.5% | autoresearch_runner keep: trend_ma_period=40 |
| 2026-05-14 08:03 | 0.3998 | 183 | 1.75 | 9.0% | lookback_days: 15 → 11 |
| 2026-05-14 08:03 | 0.3998 | 183 | 1.75 | 9.0% | autoresearch_runner keep: lookback_days=11 |
| 2026-05-18 10:57 | 1.3174 | 569 | 1.70 | 25.4% | atr_stop_mult: 0.72 → 0.9 |
| 2026-05-18 10:57 | 1.3174 | 569 | 1.70 | 25.4% | autoresearch_runner keep: atr_stop_mult=0.9 |
| 2026-05-18 11:13 | 0.4717 | 212 | 2.55 | 17.9% | atr_stop_mult: 0.75 → 0.38 |
| 2026-05-18 11:13 | 0.4717 | 212 | 2.55 | 17.9% | autoresearch_runner keep: atr_stop_mult=0.38 |
| 2026-05-18 11:14 | 0.4840 | 205 | 2.45 | 17.6% | lookback_days: 11 → 10 |
| 2026-05-18 11:14 | 0.4840 | 205 | 2.45 | 17.6% | autoresearch_runner keep: lookback_days=10 |
| 2026-05-18 15:17 | 0.5559 | 200 | 1.96 | 14.8% | atr_stop_mult: 0.38 → 0.42 |
| 2026-05-18 15:17 | 0.5559 | 200 | 1.96 | 14.8% | autoresearch_runner keep: atr_stop_mult=0.42 |
| 2026-05-20 15:18 | 0.6552 | 215 | 2.12 | 16.9% | lookback_days: 10 → 8 |
| 2026-05-20 15:18 | 0.6552 | 215 | 2.12 | 16.9% | autoresearch_runner keep: lookback_days=8 |
| 2026-05-21 14:01 | 1.3756 | 568 | 1.77 | 26.1% | atr_stop_mult: 0.9 → 0.99 |
| 2026-05-21 14:01 | 1.3756 | 568 | 1.77 | 26.1% | autoresearch_runner keep: atr_stop_mult=0.99 |
| 2026-05-22 14:01 | 1.2172 | 545 | 1.66 | 23.8% | atr_stop_mult: 0.99 → 0.89 |
| 2026-05-22 14:01 | 1.2172 | 545 | 1.66 | 23.8% | autoresearch_runner keep: atr_stop_mult=0.89 |
| 2026-05-22 14:02 | 1.2775 | 489 | 1.71 | 22.5% | atr_stop_mult: 0.99 → 1.09 |
| 2026-05-22 14:02 | 1.2775 | 489 | 1.71 | 22.5% | autoresearch_runner keep: atr_stop_mult=1.09 |
| 2026-05-22 14:05 | 1.3047 | 501 | 1.74 | 22.9% | atr_period: 24 → 22 |
| 2026-05-22 14:05 | 1.3047 | 501 | 1.74 | 22.9% | autoresearch_runner keep: atr_period=22 |
| 2026-05-22 15:17 | 0.3956 | 240 | 2.17 | 15.7% | atr_stop_mult: 0.42 → 0.38 |
| 2026-05-22 15:17 | 0.3956 | 240 | 2.17 | 15.7% | autoresearch_runner keep: atr_stop_mult=0.38 |
| 2026-05-23 15:18 | 0.4735 | 235 | 2.69 | 21.3% | atr_stop_mult: 0.38 → 0.29 |
| 2026-05-23 15:18 | 0.4735 | 235 | 2.69 | 21.3% | autoresearch_runner keep: atr_stop_mult=0.29 |
| 2026-05-26 14:21 | 1.0188 | 390 | 1.49 | 21.4% | atr_period 20->22: re-test for keep (manual rules satisfied) |
| 2026-05-27 14:17 | 0.4938 | 379 | 1.32 | 12.3% | trend_ma_period 35->30: trend filter shorter, untested direction |
| 2026-05-28 13:49 | 1.0101 | 490 | 1.36 | 23.1% | profit_target_atr_mult: 0 -> 2.2 (manual override DSR: doubles Sharpe) |
