# sma200_filter

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
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
| 2026-03-15 07:01 | williams_percent_r | False → True | ❌ discard | -0.1352 | -0.1659 |
| 2026-03-15 10:05 | consecutive_down_days | False → True | ❌ discard | -0.7537 | -0.2612 |
| 2026-03-15 10:08 | demark_sequential | True → False | ❌ discard | -0.2324 | -0.1078 |
| 2026-03-15 10:09 | donchian_breakout | None → False | ❌ discard | -0.1416 | 0.2233 |
| 2026-03-15 10:09 | donchian_breakout | None → True | ❌ discard | +0.0000 | 0.3649 |
| 2026-03-15 10:43 | williams_percent_r | False → True | ❌ discard | -2.0433 | -1.9172 |
| 2026-03-15 11:13 | lower_band_reversion | None → True | ❌ discard | +0.0000 | 0.3934 |
| 2026-03-15 11:13 | lower_band_reversion | None → False | ❌ discard | -0.0157 | 0.3777 |
| 2026-03-15 11:52 | keltner_reversion | None → True | ❌ discard | +0.0000 | 0.0184 |
| 2026-03-15 11:52 | keltner_reversion | None → False | ❌ discard | -0.1162 | -0.0978 |
| 2026-03-16 00:52 | adx_trend_pullback | None → True | ❌ discard | +0.0000 | 0.4423 |
| 2026-03-16 00:52 | adx_trend_pullback | None → False | ❌ discard | -0.0048 | 0.4375 |
| 2026-03-16 00:58 | consecutive_down_days | False → True | ❌ discard | -0.7537 | -0.2612 |
| 2026-03-16 01:01 | demark_sequential | True → False | ❌ discard | -0.2324 | -0.1078 |
| 2026-03-16 01:03 | donchian_breakout | None → True | ❌ discard | +0.0000 | 0.3649 |
| 2026-03-16 01:03 | donchian_breakout | None → False | ❌ discard | -0.1416 | 0.2233 |
| 2026-03-16 01:10 | williams_percent_r | False → True | ❌ discard | -2.0433 | -1.9172 |
| 2026-03-16 05:41 | consecutive_down_days | False → True | ❌ discard | -0.7340 | -0.2407 |
| 2026-03-16 05:44 | demark_sequential | True → False | ❌ discard | -0.2311 | -0.1270 |
| 2026-03-16 06:11 | donchian_breakout | None → False | ✅ kept | +0.0321 | 0.3440 |
| 2026-03-16 06:11 | donchian_breakout | None → True | ❌ discard | +0.0000 | 0.3119 |
| 2026-03-16 06:19 | williams_percent_r | False → True | ❌ discard | -1.9339 | -1.7563 |
| 2026-03-16 07:03 | triple_rsi | None → True | ❌ discard | +0.0000 | 0.2749 |
| 2026-03-16 07:03 | triple_rsi | None → False | ❌ discard | -2.6793 | -2.4044 |
| 2026-03-16 07:38 | keltner_reversion | None → True | ❌ discard | +0.0000 | 0.0408 |
| 2026-03-16 07:38 | keltner_reversion | None → False | ❌ discard | -0.0528 | -0.0120 |
| 2026-03-16 10:15 | demark_sequential | True → False | ❌ discard | -0.8169 | 0.1495 |
| 2026-03-16 10:20 | donchian_breakout | False → True | ❌ discard | -0.5054 | 0.4548 |
| 2026-03-16 10:27 | williams_percent_r | False → True | ❌ discard | -1.4780 | -0.5116 |
| 2026-03-16 10:50 | keltner_reversion | None → True | ❌ discard | -0.6891 | 0.2773 |
| 2026-03-16 10:50 | keltner_reversion | None → False | ❌ discard | -0.6783 | 0.2881 |
