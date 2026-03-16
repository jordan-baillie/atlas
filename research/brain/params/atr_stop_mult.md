# atr_stop_mult

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
| 2026-03-16 07:02 | triple_rsi | 2.0 → 1.5 | ❌ discard | -0.0134 | 0.2615 |
| 2026-03-16 07:02 | triple_rsi | 2.0 → 2.5 | ❌ discard | -0.1483 | 0.1266 |
| 2026-03-16 07:06 | keltner_reversion | 1.5 → 3.0 | ❌ discard | -0.0171 | -0.0089 |
| 2026-03-16 07:06 | keltner_reversion | 1.5 → 2.5 | ❌ discard | -0.3434 | -0.3352 |
| 2026-03-16 07:06 | keltner_reversion | 1.5 → 2.0 | ❌ discard | -0.1736 | -0.1654 |
| 2026-03-16 10:08 | adx_trend_pullback | 1.5 → 2.5 | ❌ discard | -0.4454 | 0.4739 |
| 2026-03-16 10:08 | adx_trend_pullback | 1.5 → 2.0 | ❌ discard | -0.4450 | 0.4743 |
| 2026-03-16 10:08 | adx_trend_pullback | 1.5 → 3.0 | ❌ discard | -0.4574 | 0.4619 |
| 2026-03-16 10:14 | demark_sequential | 1.5 → 2.5 | ❌ discard | -0.7810 | 0.1854 |
| 2026-03-16 10:14 | demark_sequential | 1.5 → 2.0 | ❌ discard | -0.7626 | 0.2038 |
| 2026-03-16 10:14 | demark_sequential | 1.5 → 3.0 | ❌ discard | -0.8908 | 0.0756 |
| 2026-03-16 10:19 | donchian_breakout | 1.5 → 3.0 | ❌ discard | -0.4683 | 0.4919 |
| 2026-03-16 10:19 | donchian_breakout | 1.5 → 2.5 | ❌ discard | -0.4536 | 0.5066 |
| 2026-03-16 10:19 | donchian_breakout | 1.5 → 2.0 | ❌ discard | -0.5105 | 0.4497 |
| 2026-03-16 10:25 | williams_percent_r | 1.5 → 3.0 | ❌ discard | -2.3399 | -1.3735 |
| 2026-03-16 10:25 | williams_percent_r | 1.5 → 2.5 | ❌ discard | -1.5472 | -0.5808 |
| 2026-03-16 10:25 | williams_percent_r | 1.5 → 2.0 | ❌ discard | -0.6629 | 0.3035 |
| 2026-03-16 10:33 | lower_band_reversion | 2.5 → 3.0 | ❌ discard | -2.1529 | -1.2073 |
| 2026-03-16 10:33 | lower_band_reversion | 2.5 → 2.0 | ❌ discard | -1.5682 | -0.6226 |
| 2026-03-16 10:33 | lower_band_reversion | 2.5 → 1.5 | ❌ discard | -1.7881 | -0.8425 |
| 2026-03-16 10:42 | triple_rsi | 2.0 → 3.0 | ❌ discard | -0.6434 | 0.3230 |
| 2026-03-16 10:42 | triple_rsi | 2.0 → 1.5 | ❌ discard | -0.5197 | 0.4467 |
| 2026-03-16 10:42 | triple_rsi | 2.0 → 2.5 | ❌ discard | -0.5717 | 0.3947 |
| 2026-03-16 10:48 | keltner_reversion | 1.5 → 2.0 | ❌ discard | -0.7589 | 0.2075 |
| 2026-03-16 10:48 | keltner_reversion | 1.5 → 3.0 | ❌ discard | -0.9277 | 0.0387 |
| 2026-03-16 10:48 | keltner_reversion | 1.5 → 2.5 | ❌ discard | -0.8454 | 0.1210 |
| 2026-03-16 10:53 | inside_bar_nr7 | None → 3.0 | ❌ discard | -0.7527 | 0.2305 |
| 2026-03-16 10:53 | inside_bar_nr7 | None → 2.0 | ❌ discard | -0.8797 | 0.1035 |
| 2026-03-16 10:53 | inside_bar_nr7 | None → 1.5 | ❌ discard | -0.7969 | 0.1863 |
| 2026-03-16 10:53 | inside_bar_nr7 | None → 2.5 | ❌ discard | -0.9307 | 0.0525 |
| 2026-03-16 10:58 | volume_climax | None → 2.5 | ❌ discard | -0.5707 | 0.3957 |
| 2026-03-16 10:58 | volume_climax | None → 1.5 | ❌ discard | -0.5623 | 0.4041 |
| 2026-03-16 10:58 | volume_climax | None → 3.0 | ❌ discard | -0.5754 | 0.3910 |
| 2026-03-16 10:58 | volume_climax | None → 2.0 | ❌ discard | -0.5660 | 0.4004 |
| 2026-03-16 11:02 | gap_and_go | None → 2.0 | ❌ discard | -2.3360 | -1.3694 |
| 2026-03-16 11:02 | gap_and_go | None → 2.5 | ❌ discard | -3.0960 | -2.1294 |
| 2026-03-16 11:02 | gap_and_go | None → 3.0 | ❌ discard | -3.7341 | -2.7675 |
| 2026-03-16 11:02 | gap_and_go | None → 1.5 | ❌ discard | -1.8590 | -0.8924 |
| 2026-03-16 11:35 | heikin_ashi_reversal | None → 2.5 | ❌ discard | -2.5108 | -1.5445 |
| 2026-03-16 11:35 | heikin_ashi_reversal | None → 3.0 | ❌ discard | -2.7405 | -1.7742 |
| 2026-03-16 11:35 | heikin_ashi_reversal | None → 1.5 | ❌ discard | -1.9256 | -0.9593 |
| 2026-03-16 11:35 | heikin_ashi_reversal | None → 2.0 | ❌ discard | -2.0810 | -1.1147 |
| 2026-03-16 11:53 | macd_divergence | None → 1.5 | ❌ discard | -0.7904 | 0.1760 |
| 2026-03-16 11:53 | macd_divergence | None → 2.5 | ❌ discard | -0.8472 | 0.1192 |
| 2026-03-16 11:53 | macd_divergence | None → 2.0 | ❌ discard | -0.8175 | 0.1489 |
| 2026-03-16 11:53 | macd_divergence | None → 3.0 | ❌ discard | -0.9045 | 0.0619 |
| 2026-03-16 12:00 | overnight_return | None → 3.0 | ❌ discard | -3.0495 | -2.1223 |
| 2026-03-16 12:00 | overnight_return | None → 1.5 | ❌ discard | -2.1106 | -1.1834 |
| 2026-03-16 12:00 | overnight_return | None → 2.5 | ❌ discard | -2.7894 | -1.8622 |
| 2026-03-16 12:00 | overnight_return | None → 2.0 | ❌ discard | -2.6003 | -1.6731 |
