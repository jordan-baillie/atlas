# max_hold_days

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
| 2026-03-15 10:09 | donchian_breakout | 30 → 15 | ❌ discard | -0.3418 | 0.0231 |
| 2026-03-15 10:09 | donchian_breakout | 30 → 10 | ❌ discard | -0.2453 | 0.1196 |
| 2026-03-15 10:13 | stochastic_oversold | None → 7 | ❌ discard | +0.0002 | 0.3993 |
| 2026-03-15 10:13 | stochastic_oversold | None → 5 | ❌ discard | +0.0017 | 0.4008 |
| 2026-03-15 10:13 | stochastic_oversold | None → 15 | ❌ discard | +0.0000 | 0.3991 |
| 2026-03-15 10:13 | stochastic_oversold | None → 10 | ❌ discard | +0.0000 | 0.3991 |
| 2026-03-15 10:42 | williams_percent_r | 7 → 15 | ❌ discard | -1.7531 | -1.6270 |
| 2026-03-15 10:42 | williams_percent_r | 7 → 10 | ❌ discard | -1.7422 | -1.6161 |
| 2026-03-15 10:42 | williams_percent_r | 7 → 5 | ❌ discard | -1.8086 | -1.6825 |
| 2026-03-15 11:12 | lower_band_reversion | None → 10 | ❌ discard | +0.0012 | 0.3946 |
| 2026-03-15 11:12 | lower_band_reversion | None → 5 | ❌ discard | -0.0180 | 0.3754 |
| 2026-03-15 11:12 | lower_band_reversion | None → 3 | ❌ discard | -4.1122 | -3.7188 |
| 2026-03-15 11:12 | lower_band_reversion | None → 7 | ❌ discard | +0.0000 | 0.3934 |
| 2026-03-15 11:17 | triple_rsi | None → 5 | ❌ discard | -0.3342 | -0.1354 |
| 2026-03-15 11:17 | triple_rsi | None → 7 | ❌ discard | -0.2914 | -0.0926 |
| 2026-03-15 11:17 | triple_rsi | None → 3 | ❌ discard | -0.3132 | -0.1144 |
| 2026-03-15 11:17 | triple_rsi | None → 10 | ❌ discard | +0.0000 | 0.1988 |
| 2026-03-15 11:51 | keltner_reversion | None → 5 | ❌ discard | -0.0539 | -0.0355 |
| 2026-03-15 11:51 | keltner_reversion | None → 10 | ❌ discard | +0.0000 | 0.0184 |
| 2026-03-15 11:51 | keltner_reversion | None → 15 | ❌ discard | -0.0177 | 0.0007 |
| 2026-03-15 11:51 | keltner_reversion | None → 7 | ❌ discard | -0.0002 | 0.0182 |
| 2026-03-15 23:32 | short_term_mr | None → 3 | ❌ discard | -0.0615 | 0.4350 |
| 2026-03-15 23:32 | short_term_mr | None → 7 | ❌ discard | +0.0015 | 0.4980 |
| 2026-03-15 23:32 | short_term_mr | None → 5 | ❌ discard | +0.0000 | 0.4965 |
| 2026-03-15 23:32 | short_term_mr | None → 2 | ❌ discard | -0.0626 | 0.4339 |
| 2026-03-16 00:51 | adx_trend_pullback | None → 7 | ✅ kept | +0.0313 | 0.4423 |
| 2026-03-16 00:51 | adx_trend_pullback | None → 5 | ❌ discard | -0.0189 | 0.3921 |
| 2026-03-16 00:51 | adx_trend_pullback | None → 10 | ❌ discard | +0.0000 | 0.4110 |
| 2026-03-16 00:51 | adx_trend_pullback | None → 15 | ❌ discard | -1.0565 | -0.6455 |
| 2026-03-16 00:57 | consecutive_down_days | None → 10 | ❌ discard | -0.0041 | 0.4884 |
| 2026-03-16 00:57 | consecutive_down_days | None → 7 | ❌ discard | -0.0017 | 0.4908 |
| 2026-03-16 00:57 | consecutive_down_days | None → 3 | ❌ discard | +0.0000 | 0.4925 |
| 2026-03-16 00:57 | consecutive_down_days | None → 5 | ❌ discard | +0.0000 | 0.4925 |
| 2026-03-16 01:00 | demark_sequential | 7 → 5 | ❌ discard | -0.4914 | -0.3668 |
| 2026-03-16 01:00 | demark_sequential | 7 → 10 | ❌ discard | -0.3288 | -0.2042 |
| 2026-03-16 01:00 | demark_sequential | 7 → 15 | ❌ discard | -0.2568 | -0.1322 |
| 2026-03-16 01:02 | donchian_breakout | 30 → 10 | ❌ discard | -0.2453 | 0.1196 |
| 2026-03-16 01:02 | donchian_breakout | 30 → 20 | ❌ discard | -0.0648 | 0.3001 |
| 2026-03-16 01:02 | donchian_breakout | 30 → 15 | ❌ discard | -0.3418 | 0.0231 |
| 2026-03-16 01:06 | stochastic_oversold | None → 5 | ❌ discard | +0.0017 | 0.4008 |
| 2026-03-16 01:06 | stochastic_oversold | None → 10 | ❌ discard | +0.0000 | 0.3991 |
| 2026-03-16 01:06 | stochastic_oversold | None → 7 | ❌ discard | +0.0002 | 0.3993 |
| 2026-03-16 01:06 | stochastic_oversold | None → 15 | ❌ discard | +0.0000 | 0.3991 |
| 2026-03-16 01:09 | williams_percent_r | 7 → 10 | ❌ discard | -1.7422 | -1.6161 |
| 2026-03-16 01:09 | williams_percent_r | 7 → 5 | ❌ discard | -1.8086 | -1.6825 |
| 2026-03-16 01:09 | williams_percent_r | 7 → 15 | ❌ discard | -1.7531 | -1.6270 |
| 2026-03-16 01:13 | lower_band_reversion | None → 3 | ❌ discard | -4.1122 | -3.7188 |
| 2026-03-16 01:13 | lower_band_reversion | None → 7 | ❌ discard | +0.0000 | 0.3934 |
| 2026-03-16 01:13 | lower_band_reversion | None → 5 | ❌ discard | -0.0180 | 0.3754 |
| 2026-03-16 01:13 | lower_band_reversion | None → 10 | ❌ discard | +0.0012 | 0.3946 |
