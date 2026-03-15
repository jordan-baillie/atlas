# sma200_filter

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
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
| 2026-03-14 11:57 | triple_rsi | None → True | ❌ discard | +0.0000 | 0.2065 |
| 2026-03-14 11:57 | triple_rsi | None → False | ❌ discard | -3.2077 | -3.0012 |
| 2026-03-14 23:09 | donchian_breakout | None → False | ❌ discard | +0.0083 | 0.5549 |
| 2026-03-14 23:09 | donchian_breakout | None → True | ❌ discard | +0.0000 | 0.5466 |
| 2026-03-14 23:12 | williams_percent_r | False → True | ❌ discard | -2.2991 | -2.1023 |
| 2026-03-14 23:53 | triple_rsi | None → True | ❌ discard | +0.0000 | 0.1988 |
| 2026-03-14 23:53 | triple_rsi | None → False | ❌ discard | -3.5851 | -3.3863 |
| 2026-03-15 02:32 | demark_sequential | None → True | ❌ discard | -0.1668 | -1.9547 |
| 2026-03-15 02:32 | demark_sequential | None → False | ❌ discard | +0.0000 | -1.7879 |
| 2026-03-15 02:34 | donchian_breakout | None → True | ❌ discard | +0.0000 | 0.3649 |
| 2026-03-15 02:34 | donchian_breakout | None → False | ❌ discard | -0.1416 | 0.2233 |
| 2026-03-15 03:06 | stochastic_oversold | None → True | ❌ discard | +0.0000 | 0.3991 |
| 2026-03-15 03:06 | stochastic_oversold | None → False | ❌ discard | +0.0007 | 0.3998 |
| 2026-03-15 06:24 | demark_sequential | None → True | ✅ kept | +0.2324 | 0.1246 |
| 2026-03-15 06:24 | demark_sequential | None → False | ❌ discard | +0.0000 | -0.1078 |
| 2026-03-15 06:26 | donchian_breakout | None → False | ❌ discard | -0.1416 | 0.2233 |
| 2026-03-15 06:26 | donchian_breakout | None → True | ❌ discard | +0.0000 | 0.3649 |
