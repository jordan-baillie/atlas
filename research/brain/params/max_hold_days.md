# max_hold_days

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
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
| 2026-03-16 07:01 | triple_rsi | None → 10 | ❌ discard | +0.0000 | 0.2749 |
| 2026-03-16 07:01 | triple_rsi | None → 3 | ❌ discard | -2.8061 | -2.5312 |
| 2026-03-16 07:01 | triple_rsi | None → 7 | ❌ discard | -0.3471 | -0.0722 |
| 2026-03-16 07:01 | triple_rsi | None → 5 | ❌ discard | -2.8540 | -2.5791 |
| 2026-03-16 07:37 | keltner_reversion | None → 7 | ✅ kept | +0.0326 | 0.0408 |
| 2026-03-16 07:37 | keltner_reversion | None → 5 | ❌ discard | -0.0383 | -0.0301 |
| 2026-03-16 07:37 | keltner_reversion | None → 10 | ❌ discard | +0.0000 | 0.0082 |
| 2026-03-16 07:37 | keltner_reversion | None → 15 | ❌ discard | -0.0248 | -0.0166 |
| 2026-03-16 10:09 | adx_trend_pullback | 7 → 15 | ❌ discard | -0.4715 | 0.4478 |
| 2026-03-16 10:09 | adx_trend_pullback | 7 → 10 | ❌ discard | -0.4729 | 0.4464 |
| 2026-03-16 10:09 | adx_trend_pullback | 7 → 5 | ❌ discard | -0.5148 | 0.4045 |
| 2026-03-16 10:14 | demark_sequential | 7 → 10 | ❌ discard | -0.8932 | 0.0732 |
| 2026-03-16 10:14 | demark_sequential | 7 → 15 | ❌ discard | -0.8329 | 0.1335 |
| 2026-03-16 10:14 | demark_sequential | 7 → 5 | ❌ discard | -1.1079 | -0.1415 |
| 2026-03-16 10:19 | donchian_breakout | 30 → 10 | ❌ discard | -0.6809 | 0.2793 |
| 2026-03-16 10:19 | donchian_breakout | 30 → 20 | ❌ discard | -1.6164 | -0.6562 |
| 2026-03-16 10:19 | donchian_breakout | 30 → 15 | ❌ discard | -1.9993 | -1.0391 |
| 2026-03-16 10:26 | williams_percent_r | 7 → 5 | ❌ discard | -1.7913 | -0.8249 |
| 2026-03-16 10:26 | williams_percent_r | 7 → 15 | ❌ discard | -0.7264 | 0.2400 |
| 2026-03-16 10:26 | williams_percent_r | 7 → 10 | ❌ discard | -0.7102 | 0.2562 |
| 2026-03-16 10:32 | lower_band_reversion | None → 3 | ❌ discard | -0.8371 | 0.1085 |
| 2026-03-16 10:32 | lower_band_reversion | None → 10 | ❌ discard | -1.8453 | -0.8997 |
| 2026-03-16 10:32 | lower_band_reversion | None → 7 | ❌ discard | -1.8426 | -0.8970 |
| 2026-03-16 10:32 | lower_band_reversion | None → 5 | ❌ discard | -1.9404 | -0.9948 |
| 2026-03-16 10:41 | triple_rsi | None → 7 | ❌ discard | -0.7354 | 0.2310 |
| 2026-03-16 10:41 | triple_rsi | None → 5 | ❌ discard | -2.1697 | -1.2033 |
| 2026-03-16 10:41 | triple_rsi | None → 3 | ❌ discard | -2.1688 | -1.2024 |
| 2026-03-16 10:41 | triple_rsi | None → 10 | ❌ discard | -0.5229 | 0.4435 |
| 2026-03-16 10:49 | keltner_reversion | 7 → 5 | ❌ discard | -0.7685 | 0.1979 |
| 2026-03-16 10:49 | keltner_reversion | 7 → 10 | ❌ discard | -0.6391 | 0.3273 |
| 2026-03-16 10:49 | keltner_reversion | 7 → 15 | ❌ discard | -0.6924 | 0.2740 |
