# max_hold_days

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
| 2026-03-14 10:06 | stochastic_oversold | None → 5 | ❌ discard | +0.0019 | 0.4006 |
| 2026-03-14 10:09 | williams_percent_r | 7 → 10 | ❌ discard | -1.6889 | -1.4921 |
| 2026-03-14 10:09 | williams_percent_r | 7 → 15 | ❌ discard | -1.3423 | -1.1455 |
| 2026-03-14 10:09 | williams_percent_r | 7 → 5 | ❌ discard | -0.1369 | 0.0599 |
| 2026-03-14 10:13 | lower_band_reversion | None → 5 | ❌ discard | -0.0220 | 0.3746 |
| 2026-03-14 10:13 | lower_band_reversion | None → 10 | ❌ discard | +0.0037 | 0.4003 |
| 2026-03-14 10:13 | lower_band_reversion | None → 7 | ❌ discard | +0.0000 | 0.3966 |
| 2026-03-14 10:13 | lower_band_reversion | None → 3 | ❌ discard | -3.6271 | -3.2305 |
| 2026-03-14 11:22 | triple_rsi | None → 3 | ❌ discard | -0.4146 | -0.3503 |
| 2026-03-14 11:22 | triple_rsi | None → 10 | ❌ discard | +0.0000 | 0.0643 |
| 2026-03-14 11:22 | triple_rsi | None → 5 | ❌ discard | -0.4337 | -0.3694 |
| 2026-03-14 11:22 | triple_rsi | None → 7 | ❌ discard | -4.3817 | -4.3174 |
| 2026-03-14 23:01 | short_term_mr | None → 3 | ❌ discard | -0.0592 | 0.4545 |
| 2026-03-14 23:01 | short_term_mr | None → 7 | ❌ discard | -0.0015 | 0.5122 |
| 2026-03-14 23:01 | short_term_mr | None → 2 | ❌ discard | -0.0568 | 0.4569 |
| 2026-03-14 23:01 | short_term_mr | None → 5 | ❌ discard | +0.0000 | 0.5137 |
| 2026-03-14 23:07 | adx_trend_pullback | None → 5 | ❌ discard | +0.0039 | 0.3940 |
| 2026-03-14 23:07 | adx_trend_pullback | None → 15 | ❌ discard | -0.0056 | 0.3845 |
| 2026-03-14 23:07 | adx_trend_pullback | None → 10 | ❌ discard | +0.0000 | 0.3901 |
| 2026-03-14 23:07 | adx_trend_pullback | None → 7 | ❌ discard | +0.0016 | 0.3917 |
| 2026-03-14 23:09 | donchian_breakout | 30 → 10 | ❌ discard | -0.4285 | 0.1181 |
| 2026-03-14 23:09 | donchian_breakout | 30 → 20 | ❌ discard | -0.3910 | 0.1556 |
| 2026-03-14 23:09 | donchian_breakout | 30 → 15 | ❌ discard | -0.4671 | 0.0795 |
| 2026-03-14 23:12 | williams_percent_r | 7 → 10 | ❌ discard | -1.7271 | -1.5303 |
| 2026-03-14 23:12 | williams_percent_r | 7 → 5 | ❌ discard | -0.1568 | 0.0400 |
| 2026-03-14 23:12 | williams_percent_r | 7 → 15 | ❌ discard | -1.6629 | -1.4661 |
| 2026-03-14 23:15 | lower_band_reversion | None → 5 | ❌ discard | -0.0220 | 0.3746 |
| 2026-03-14 23:15 | lower_band_reversion | None → 10 | ❌ discard | +0.0037 | 0.4003 |
| 2026-03-14 23:15 | lower_band_reversion | None → 3 | ❌ discard | -3.6271 | -3.2305 |
| 2026-03-14 23:15 | lower_band_reversion | None → 7 | ❌ discard | +0.0000 | 0.3966 |
| 2026-03-14 23:20 | triple_rsi | None → 7 | ❌ discard | -4.3817 | -4.3174 |
| 2026-03-14 23:20 | triple_rsi | None → 10 | ❌ discard | +0.0000 | 0.0643 |
| 2026-03-14 23:20 | triple_rsi | None → 5 | ❌ discard | -0.4346 | -0.3703 |
| 2026-03-14 23:20 | triple_rsi | None → 3 | ❌ discard | -0.4146 | -0.3503 |
| 2026-03-15 02:31 | demark_sequential | 15 → 10 | ✅ kept | +0.1618 | -1.7879 |
| 2026-03-15 02:31 | demark_sequential | 15 → 7 | ❌ discard | -0.2439 | -2.1936 |
| 2026-03-15 02:31 | demark_sequential | 15 → 5 | ❌ discard | -0.2795 | -2.2292 |
| 2026-03-15 02:33 | donchian_breakout | 30 → 10 | ❌ discard | -0.2453 | 0.1196 |
| 2026-03-15 02:33 | donchian_breakout | 30 → 20 | ❌ discard | -0.0648 | 0.3001 |
| 2026-03-15 02:33 | donchian_breakout | 30 → 15 | ❌ discard | -0.3418 | 0.0231 |
| 2026-03-15 03:06 | stochastic_oversold | None → 15 | ❌ discard | +0.0000 | 0.3991 |
| 2026-03-15 03:06 | stochastic_oversold | None → 5 | ❌ discard | +0.0017 | 0.4008 |
| 2026-03-15 03:06 | stochastic_oversold | None → 7 | ❌ discard | +0.0002 | 0.3993 |
| 2026-03-15 03:06 | stochastic_oversold | None → 10 | ❌ discard | +0.0000 | 0.3991 |
| 2026-03-15 05:56 | demark_sequential | 10 → 7 | ✅ kept | +0.1843 | -0.1078 |
| 2026-03-15 05:56 | demark_sequential | 10 → 15 | ❌ discard | -1.6583 | -1.9504 |
| 2026-03-15 05:56 | demark_sequential | 10 → 5 | ❌ discard | -2.2739 | -2.5660 |
| 2026-03-15 06:26 | donchian_breakout | 30 → 15 | ❌ discard | -0.3418 | 0.0231 |
| 2026-03-15 06:26 | donchian_breakout | 30 → 20 | ❌ discard | -0.0648 | 0.3001 |
| 2026-03-15 06:26 | donchian_breakout | 30 → 10 | ❌ discard | -0.2453 | 0.1196 |
