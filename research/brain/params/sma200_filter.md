# sma200_filter

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
| 2026-03-12 10:31 | momentum_breakout | None → True | ❌ discard | +0.0000 | 0.7217 |
| 2026-03-12 10:31 | momentum_breakout | None → False | ❌ discard | +0.0000 | 0.7217 |
| 2026-03-12 13:17 | connors_rsi2 | False → True | ❌ discard | +0.0045 | 0.3948 |
| 2026-03-12 13:19 | momentum_breakout | None → True | ❌ discard | +0.0000 | 0.7217 |
| 2026-03-12 13:19 | momentum_breakout | None → False | ❌ discard | +0.0000 | 0.7217 |
| 2026-03-12 13:59 | consecutive_down_days | False → True | ❌ discard | -0.1857 | 0.3884 |
| 2026-03-12 14:50 | demark_sequential | None → True | ❌ discard | -4.7199 | -8.7106 |
| 2026-03-12 14:50 | demark_sequential | None → False | ❌ discard | +0.0000 | -3.9907 |
| 2026-03-12 23:47 | connors_rsi2 | False → True | ❌ discard | +0.0045 | 0.3948 |
| 2026-03-12 23:49 | momentum_breakout | None → False | ❌ discard | +0.0000 | 0.7217 |
| 2026-03-12 23:49 | momentum_breakout | None → True | ❌ discard | +0.0000 | 0.7217 |
| 2026-03-13 00:28 | consecutive_down_days | False → True | ❌ discard | -0.1857 | 0.3884 |
| 2026-03-13 02:47 | connors_rsi2 | False → True | ❌ discard | +0.0045 | 0.3948 |
| 2026-03-13 02:49 | momentum_breakout | None → False | ❌ discard | +0.0000 | 0.7217 |
| 2026-03-13 02:49 | momentum_breakout | None → True | ❌ discard | +0.0000 | 0.7217 |
| 2026-03-13 03:29 | consecutive_down_days | False → True | ❌ discard | -0.1843 | 0.4059 |
| 2026-03-13 05:31 | momentum_breakout | None → False | ❌ discard | +0.0000 | 0.7364 |
| 2026-03-13 05:31 | momentum_breakout | None → True | ❌ discard | +0.0000 | 0.7364 |
| 2026-03-13 06:27 | adx_trend_pullback | None → True | ❌ discard | +0.0000 | 0.5558 |
| 2026-03-13 06:27 | adx_trend_pullback | None → False | ❌ discard | -0.1226 | 0.4332 |
| 2026-03-13 07:20 | demark_sequential | None → True | ❌ discard | -5.5920 | -9.7056 |
| 2026-03-13 07:20 | demark_sequential | None → False | ❌ discard | +0.0000 | -4.1136 |
| 2026-03-13 10:38 | adx_trend_pullback | None → False | ❌ discard | +0.0004 | 0.3905 |
| 2026-03-13 10:38 | adx_trend_pullback | None → True | ❌ discard | +0.0000 | 0.3901 |
| 2026-03-13 11:34 | demark_sequential | None → True | ❌ discard | -2.6036 | -5.7595 |
| 2026-03-13 11:34 | demark_sequential | None → False | ❌ discard | +0.0000 | -3.1559 |
| 2026-03-13 13:08 | demark_sequential | None → True | ❌ discard | -2.6036 | -5.7595 |
| 2026-03-13 13:08 | demark_sequential | None → False | ❌ discard | +0.0000 | -3.1559 |
| 2026-03-13 13:58 | donchian_breakout | None → False | ❌ discard | +0.0072 | 0.4168 |
| 2026-03-13 13:58 | donchian_breakout | None → True | ❌ discard | +0.0000 | 0.4096 |
| 2026-03-13 14:28 | stochastic_oversold | None → True | ❌ discard | +0.0000 | 0.3987 |
| 2026-03-13 14:28 | stochastic_oversold | None → False | ❌ discard | +0.0040 | 0.4027 |
| 2026-03-13 23:10 | demark_sequential | None → True | ❌ discard | -2.6036 | -5.7595 |
| 2026-03-13 23:10 | demark_sequential | None → False | ❌ discard | +0.0000 | -3.1559 |
| 2026-03-13 23:37 | donchian_breakout | None → False | ❌ discard | +0.0048 | 0.5343 |
| 2026-03-13 23:37 | donchian_breakout | None → True | ❌ discard | +0.0000 | 0.5295 |
| 2026-03-14 02:10 | demark_sequential | None → True | ❌ discard | -2.6036 | -5.7595 |
| 2026-03-14 02:10 | demark_sequential | None → False | ❌ discard | +0.0000 | -3.1559 |
| 2026-03-14 02:37 | donchian_breakout | None → True | ❌ discard | +0.0000 | 0.5547 |
| 2026-03-14 02:37 | donchian_breakout | None → False | ❌ discard | +0.0055 | 0.5602 |
| 2026-03-14 03:13 | williams_percent_r | None → False | ✅ kept | +0.4820 | -1.1208 |
| 2026-03-14 03:13 | williams_percent_r | None → True | ❌ discard | +0.0000 | -1.6028 |
| 2026-03-14 05:01 | donchian_breakout | None → False | ❌ discard | +0.0055 | 0.5602 |
| 2026-03-14 05:01 | donchian_breakout | None → True | ❌ discard | +0.0000 | 0.5547 |
| 2026-03-14 05:32 | williams_percent_r | False → True | ❌ discard | -2.3366 | -2.1398 |
| 2026-03-14 06:29 | lower_band_reversion | None → True | ❌ discard | +0.0000 | 0.3966 |
| 2026-03-14 06:29 | lower_band_reversion | None → False | ❌ discard | -0.0010 | 0.3956 |
| 2026-03-14 10:02 | donchian_breakout | None → False | ❌ discard | +0.0055 | 0.5602 |
| 2026-03-14 10:02 | donchian_breakout | None → True | ❌ discard | +0.0000 | 0.5547 |
| 2026-03-14 10:10 | williams_percent_r | False → True | ❌ discard | -2.3366 | -2.1398 |
