# sma200_filter

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
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
| 2026-03-16 10:54 | inside_bar_nr7 | None → False | ❌ discard | -0.8204 | 0.1628 |
| 2026-03-16 10:54 | inside_bar_nr7 | None → True | ❌ discard | -0.8797 | 0.1035 |
| 2026-03-16 10:59 | volume_climax | None → True | ❌ discard | -0.5660 | 0.4004 |
| 2026-03-16 10:59 | volume_climax | None → False | ❌ discard | -0.5657 | 0.4007 |
| 2026-03-16 11:02 | gap_and_go | None → True | ❌ discard | -2.3360 | -1.3694 |
| 2026-03-16 11:02 | gap_and_go | None → False | ❌ discard | -2.2184 | -1.2518 |
| 2026-03-16 11:48 | heikin_ashi_reversal | None → True | ❌ discard | -2.0810 | -1.1147 |
| 2026-03-16 11:48 | heikin_ashi_reversal | None → False | ❌ discard | -2.4008 | -1.4345 |
| 2026-03-16 12:02 | overnight_return | None → True | ❌ discard | -2.6003 | -1.6731 |
| 2026-03-16 12:02 | overnight_return | None → False | ❌ discard | -2.4383 | -1.5111 |
| 2026-03-16 13:18 | consecutive_down_days | False → True | ❌ discard | -0.6808 | 0.2420 |
| 2026-03-16 13:23 | demark_sequential | True → False | ❌ discard | -0.8200 | 0.1571 |
| 2026-04-02 04:11 | momentum_breakout | None → False | ❌ discard | +0.0000 | 0.6308 |
| 2026-04-02 04:11 | momentum_breakout | None → True | ❌ discard | +0.0000 | 0.6308 |
| 2026-04-02 04:32 | momentum_breakout | None → False | ❌ discard | +0.0000 | 0.7402 |
| 2026-04-02 04:32 | momentum_breakout | None → True | ❌ discard | +0.0000 | 0.7402 |
| 2026-04-02 05:08 | momentum_breakout | None → True | ❌ discard | +0.0000 | 0.8275 |
| 2026-04-02 05:08 | momentum_breakout | None → False | ❌ discard | +0.0000 | 0.8275 |
| 2026-04-02 05:11 | connors_rsi2 | False → True | ❌ discard | -0.2796 | 0.1348 |
| 2026-04-02 05:42 | connors_rsi2 | False → True | ❌ discard | -0.2796 | 0.1348 |
| 2026-04-02 05:46 | connors_rsi2 | False → True | ❌ discard | -0.2796 | 0.1348 |
| 2026-04-02 05:52 | connors_rsi2 | False → True | ❌ discard | -0.1954 | 0.0457 |
| 2026-04-02 05:58 | connors_rsi2 | False → True | ❌ discard | -0.1954 | 0.0457 |
| 2026-04-02 06:03 | connors_rsi2 | False → True | ❌ discard | -0.1954 | 0.0457 |
