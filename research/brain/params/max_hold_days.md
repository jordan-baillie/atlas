# max_hold_days

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
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
| 2026-03-16 10:54 | inside_bar_nr7 | None → 10 | ❌ discard | -2.8262 | -1.8430 |
| 2026-03-16 10:54 | inside_bar_nr7 | None → 7 | ❌ discard | -2.4728 | -1.4896 |
| 2026-03-16 10:54 | inside_bar_nr7 | None → 3 | ❌ discard | -0.6892 | 0.2940 |
| 2026-03-16 10:54 | inside_bar_nr7 | None → 5 | ❌ discard | -0.8797 | 0.1035 |
| 2026-03-16 10:58 | volume_climax | None → 3 | ❌ discard | -2.5759 | -1.6095 |
| 2026-03-16 10:58 | volume_climax | None → 5 | ❌ discard | -0.5660 | 0.4004 |
| 2026-03-16 10:58 | volume_climax | None → 7 | ❌ discard | -0.5652 | 0.4012 |
| 2026-03-16 10:58 | volume_climax | None → 10 | ❌ discard | -0.5651 | 0.4013 |
| 2026-03-16 11:02 | gap_and_go | None → 10 | ❌ discard | -1.8515 | -0.8849 |
| 2026-03-16 11:02 | gap_and_go | None → 7 | ❌ discard | -2.1522 | -1.1856 |
| 2026-03-16 11:02 | gap_and_go | None → 3 | ❌ discard | -2.3705 | -1.4039 |
| 2026-03-16 11:02 | gap_and_go | None → 5 | ❌ discard | -2.3360 | -1.3694 |
| 2026-03-16 11:41 | heikin_ashi_reversal | None → 10 | ❌ discard | -2.0810 | -1.1147 |
| 2026-03-16 11:41 | heikin_ashi_reversal | None → 5 | ❌ discard | -2.1293 | -1.1630 |
| 2026-03-16 11:41 | heikin_ashi_reversal | None → 15 | ❌ discard | -1.9525 | -0.9862 |
| 2026-03-16 11:41 | heikin_ashi_reversal | None → 7 | ❌ discard | -2.4603 | -1.4940 |
| 2026-03-16 11:54 | macd_divergence | None → 10 | ❌ discard | -0.8175 | 0.1489 |
| 2026-03-16 11:54 | macd_divergence | None → 15 | ❌ discard | -0.8055 | 0.1609 |
| 2026-03-16 11:54 | macd_divergence | None → 5 | ❌ discard | -1.9944 | -1.0280 |
| 2026-03-16 11:54 | macd_divergence | None → 7 | ❌ discard | -3.6276 | -2.6612 |
| 2026-03-16 12:01 | overnight_return | None → 3 | ❌ discard | -2.0404 | -1.1132 |
| 2026-03-16 12:01 | overnight_return | None → 2 | ❌ discard | -2.6003 | -1.6731 |
| 2026-03-16 12:01 | overnight_return | None → 5 | ❌ discard | -2.1997 | -1.2725 |
| 2026-03-16 12:01 | overnight_return | None → 1 | ❌ discard | -0.9384 | -0.0112 |
