# max_hold_days

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
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
| 2026-03-16 05:36 | adx_trend_pullback | 7 → 5 | ❌ discard | -0.0679 | 0.3942 |
| 2026-03-16 05:36 | adx_trend_pullback | 7 → 15 | ❌ discard | -0.0370 | 0.4251 |
| 2026-03-16 05:36 | adx_trend_pullback | 7 → 10 | ❌ discard | -0.0375 | 0.4246 |
| 2026-03-16 05:40 | consecutive_down_days | None → 7 | ❌ discard | -0.0017 | 0.4916 |
| 2026-03-16 05:40 | consecutive_down_days | None → 10 | ❌ discard | -0.0020 | 0.4913 |
| 2026-03-16 05:40 | consecutive_down_days | None → 3 | ❌ discard | +0.0002 | 0.4935 |
| 2026-03-16 05:40 | consecutive_down_days | None → 5 | ❌ discard | +0.0000 | 0.4933 |
| 2026-03-16 05:43 | demark_sequential | 7 → 10 | ❌ discard | -0.3113 | -0.2072 |
| 2026-03-16 05:43 | demark_sequential | 7 → 5 | ❌ discard | -0.4323 | -0.3282 |
| 2026-03-16 05:43 | demark_sequential | 7 → 15 | ❌ discard | -2.1501 | -2.0460 |
| 2026-03-16 05:45 | donchian_breakout | 30 → 20 | ❌ discard | -2.5246 | -2.2127 |
| 2026-03-16 05:45 | donchian_breakout | 30 → 15 | ❌ discard | -0.1079 | 0.2040 |
| 2026-03-16 05:45 | donchian_breakout | 30 → 10 | ❌ discard | -0.1795 | 0.1324 |
| 2026-03-16 06:15 | stochastic_oversold | None → 15 | ❌ discard | +0.0000 | 0.4012 |
| 2026-03-16 06:15 | stochastic_oversold | None → 7 | ❌ discard | +0.0004 | 0.4016 |
| 2026-03-16 06:15 | stochastic_oversold | None → 10 | ❌ discard | +0.0000 | 0.4012 |
| 2026-03-16 06:15 | stochastic_oversold | None → 5 | ❌ discard | +0.0001 | 0.4013 |
| 2026-03-16 06:18 | williams_percent_r | 7 → 10 | ❌ discard | -0.1899 | -0.0123 |
| 2026-03-16 06:18 | williams_percent_r | 7 → 15 | ❌ discard | -0.1264 | 0.0512 |
| 2026-03-16 06:18 | williams_percent_r | 7 → 5 | ❌ discard | -1.6665 | -1.4889 |
| 2026-03-16 06:22 | lower_band_reversion | None → 10 | ❌ discard | +0.0012 | 0.3965 |
| 2026-03-16 06:22 | lower_band_reversion | None → 7 | ❌ discard | +0.0000 | 0.3953 |
| 2026-03-16 06:22 | lower_band_reversion | None → 5 | ❌ discard | -0.0182 | 0.3771 |
| 2026-03-16 06:22 | lower_band_reversion | None → 3 | ❌ discard | -4.0901 | -3.6948 |
