# atr_stop_mult

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
| 2026-03-15 06:29 | stochastic_oversold | None → 2.0 | ❌ discard | +0.0000 | 0.3991 |
| 2026-03-15 10:03 | consecutive_down_days | None → 3.0 | ❌ discard | -0.1856 | 0.3069 |
| 2026-03-15 10:03 | consecutive_down_days | None → 2.0 | ❌ discard | +0.0000 | 0.4925 |
| 2026-03-15 10:03 | consecutive_down_days | None → 2.5 | ❌ discard | -0.0122 | 0.4803 |
| 2026-03-15 10:03 | consecutive_down_days | None → 1.5 | ❌ discard | +0.0054 | 0.4979 |
| 2026-03-15 10:07 | demark_sequential | 1.5 → 2.0 | ❌ discard | -0.1286 | -0.0040 |
| 2026-03-15 10:07 | demark_sequential | 1.5 → 2.5 | ❌ discard | -0.2195 | -0.0949 |
| 2026-03-15 10:07 | demark_sequential | 1.5 → 3.0 | ❌ discard | -0.3725 | -0.2479 |
| 2026-03-15 10:09 | donchian_breakout | 1.5 → 2.0 | ❌ discard | -2.2080 | -1.8431 |
| 2026-03-15 10:09 | donchian_breakout | 1.5 → 2.5 | ❌ discard | -1.1882 | -0.8233 |
| 2026-03-15 10:09 | donchian_breakout | 1.5 → 3.0 | ❌ discard | -1.3554 | -0.9905 |
| 2026-03-15 10:12 | stochastic_oversold | None → 2.5 | ❌ discard | -0.0056 | 0.3935 |
| 2026-03-15 10:12 | stochastic_oversold | None → 2.0 | ❌ discard | +0.0000 | 0.3991 |
| 2026-03-15 10:12 | stochastic_oversold | None → 1.5 | ❌ discard | -0.4507 | -0.0516 |
| 2026-03-15 10:12 | stochastic_oversold | None → 3.0 | ❌ discard | -0.0167 | 0.3824 |
| 2026-03-15 10:41 | williams_percent_r | 1.5 → 3.0 | ❌ discard | -3.6461 | -3.5200 |
| 2026-03-15 10:41 | williams_percent_r | 1.5 → 2.5 | ❌ discard | -2.6797 | -2.5536 |
| 2026-03-15 10:41 | williams_percent_r | 1.5 → 2.0 | ❌ discard | -1.8486 | -1.7225 |
| 2026-03-15 11:13 | lower_band_reversion | 2.5 → 3.0 | ❌ discard | -0.0293 | 0.3641 |
| 2026-03-15 11:13 | lower_band_reversion | 2.5 → 2.0 | ❌ discard | -0.0116 | 0.3818 |
| 2026-03-15 11:13 | lower_band_reversion | 2.5 → 1.5 | ❌ discard | -0.3731 | 0.0203 |
| 2026-03-15 11:18 | triple_rsi | 2.0 → 3.0 | ❌ discard | -0.2249 | -0.0261 |
| 2026-03-15 11:18 | triple_rsi | 2.0 → 1.5 | ❌ discard | -0.1776 | 0.0212 |
| 2026-03-15 11:18 | triple_rsi | 2.0 → 2.5 | ❌ discard | -0.1345 | 0.0643 |
| 2026-03-15 11:50 | keltner_reversion | 1.5 → 2.0 | ❌ discard | -0.1698 | -0.1514 |
| 2026-03-15 11:50 | keltner_reversion | 1.5 → 2.5 | ❌ discard | -4.3968 | -4.3784 |
| 2026-03-15 11:50 | keltner_reversion | 1.5 → 3.0 | ❌ discard | +0.0088 | 0.0272 |
| 2026-03-15 23:32 | short_term_mr | None → 2.0 | ❌ discard | -0.0823 | 0.4142 |
| 2026-03-15 23:32 | short_term_mr | None → 1.5 | ❌ discard | +0.0000 | 0.4965 |
| 2026-03-15 23:32 | short_term_mr | None → 2.5 | ❌ discard | -0.1148 | 0.3817 |
| 2026-03-16 00:14 | adx_trend_pullback | 1.5 → 3.0 | ❌ discard | -0.0458 | 0.3652 |
| 2026-03-16 00:14 | adx_trend_pullback | 1.5 → 2.5 | ❌ discard | -0.0284 | 0.3826 |
| 2026-03-16 00:14 | adx_trend_pullback | 1.5 → 2.0 | ❌ discard | -0.0097 | 0.4013 |
| 2026-03-16 00:56 | consecutive_down_days | None → 2.5 | ❌ discard | -0.0122 | 0.4803 |
| 2026-03-16 00:56 | consecutive_down_days | None → 1.5 | ❌ discard | +0.0054 | 0.4979 |
| 2026-03-16 00:56 | consecutive_down_days | None → 2.0 | ❌ discard | +0.0000 | 0.4925 |
| 2026-03-16 00:56 | consecutive_down_days | None → 3.0 | ❌ discard | -0.1856 | 0.3069 |
| 2026-03-16 00:59 | demark_sequential | 1.5 → 3.0 | ❌ discard | -0.3725 | -0.2479 |
| 2026-03-16 00:59 | demark_sequential | 1.5 → 2.5 | ❌ discard | -0.2195 | -0.0949 |
| 2026-03-16 00:59 | demark_sequential | 1.5 → 2.0 | ❌ discard | -0.1286 | -0.0040 |
| 2026-03-16 01:02 | donchian_breakout | 1.5 → 3.0 | ❌ discard | -1.3554 | -0.9905 |
| 2026-03-16 01:02 | donchian_breakout | 1.5 → 2.0 | ❌ discard | -2.2080 | -1.8431 |
| 2026-03-16 01:02 | donchian_breakout | 1.5 → 2.5 | ❌ discard | -1.1882 | -0.8233 |
| 2026-03-16 01:06 | stochastic_oversold | None → 3.0 | ❌ discard | -0.0167 | 0.3824 |
| 2026-03-16 01:06 | stochastic_oversold | None → 2.5 | ❌ discard | -0.0056 | 0.3935 |
| 2026-03-16 01:06 | stochastic_oversold | None → 2.0 | ❌ discard | +0.0000 | 0.3991 |
| 2026-03-16 01:06 | stochastic_oversold | None → 1.5 | ❌ discard | -0.4507 | -0.0516 |
| 2026-03-16 01:09 | williams_percent_r | 1.5 → 2.0 | ❌ discard | -1.8486 | -1.7225 |
| 2026-03-16 01:09 | williams_percent_r | 1.5 → 2.5 | ❌ discard | -2.6797 | -2.5536 |
| 2026-03-16 01:09 | williams_percent_r | 1.5 → 3.0 | ❌ discard | -3.6461 | -3.5200 |
