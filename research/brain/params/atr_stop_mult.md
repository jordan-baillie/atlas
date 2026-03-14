# atr_stop_mult

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
| 2026-03-13 23:40 | stochastic_oversold | None → 1.5 | ❌ discard | -0.4926 | -0.0939 |
| 2026-03-13 23:40 | stochastic_oversold | None → 3.0 | ❌ discard | -0.0158 | 0.3829 |
| 2026-03-13 23:40 | stochastic_oversold | None → 2.5 | ❌ discard | -0.0049 | 0.3938 |
| 2026-03-13 23:40 | stochastic_oversold | None → 2.0 | ❌ discard | +0.0000 | 0.3987 |
| 2026-03-14 00:11 | williams_percent_r | None → 1.5 | ✅ kept | +0.4687 | -2.0282 |
| 2026-03-14 00:11 | williams_percent_r | None → 3.0 | ❌ discard | -1.6426 | -4.1395 |
| 2026-03-14 00:11 | williams_percent_r | None → 2.5 | ❌ discard | -0.9652 | -3.4621 |
| 2026-03-14 00:11 | williams_percent_r | None → 2.0 | ❌ discard | +0.0000 | -2.4969 |
| 2026-03-14 02:02 | short_term_mr | None → 2.5 | ❌ discard | -0.0182 | 0.4955 |
| 2026-03-14 02:02 | short_term_mr | None → 2.0 | ❌ discard | -0.0255 | 0.4882 |
| 2026-03-14 02:02 | short_term_mr | None → 1.5 | ❌ discard | +0.0000 | 0.5137 |
| 2026-03-14 02:06 | adx_trend_pullback | 1.5 → 2.5 | ❌ discard | -0.0082 | 0.3819 |
| 2026-03-14 02:06 | adx_trend_pullback | 1.5 → 3.0 | ❌ discard | -0.0198 | 0.3703 |
| 2026-03-14 02:06 | adx_trend_pullback | 1.5 → 2.0 | ❌ discard | -2.1646 | -1.7745 |
| 2026-03-14 02:09 | demark_sequential | 1.5 → 2.5 | ❌ discard | -2.7393 | -5.8952 |
| 2026-03-14 02:09 | demark_sequential | 1.5 → 2.0 | ❌ discard | -1.1608 | -4.3167 |
| 2026-03-14 02:09 | demark_sequential | 1.5 → 3.0 | ❌ discard | -4.4076 | -7.5635 |
| 2026-03-14 02:36 | donchian_breakout | 1.5 → 2.5 | ❌ discard | -0.2891 | 0.2656 |
| 2026-03-14 02:36 | donchian_breakout | 1.5 → 2.0 | ❌ discard | -0.1464 | 0.4083 |
| 2026-03-14 02:36 | donchian_breakout | 1.5 → 3.0 | ❌ discard | -0.4722 | 0.0825 |
| 2026-03-14 02:40 | stochastic_oversold | None → 1.5 | ❌ discard | -0.4926 | -0.0939 |
| 2026-03-14 02:40 | stochastic_oversold | None → 2.5 | ❌ discard | -0.0049 | 0.3938 |
| 2026-03-14 02:40 | stochastic_oversold | None → 3.0 | ❌ discard | -0.0158 | 0.3829 |
| 2026-03-14 02:40 | stochastic_oversold | None → 2.0 | ❌ discard | +0.0000 | 0.3987 |
| 2026-03-14 02:43 | williams_percent_r | 1.5 → 3.0 | ❌ discard | -2.9005 | -4.5033 |
| 2026-03-14 02:43 | williams_percent_r | 1.5 → 2.5 | ❌ discard | -1.8159 | -3.4187 |
| 2026-03-14 02:43 | williams_percent_r | 1.5 → 2.0 | ❌ discard | -0.5793 | -2.1821 |
| 2026-03-14 05:01 | donchian_breakout | 1.5 → 2.0 | ❌ discard | -0.1464 | 0.4083 |
| 2026-03-14 05:01 | donchian_breakout | 1.5 → 3.0 | ❌ discard | -0.4722 | 0.0825 |
| 2026-03-14 05:01 | donchian_breakout | 1.5 → 2.5 | ❌ discard | -0.2891 | 0.2656 |
| 2026-03-14 05:31 | williams_percent_r | 1.5 → 3.0 | ❌ discard | -0.4828 | -0.2860 |
| 2026-03-14 05:31 | williams_percent_r | 1.5 → 2.5 | ❌ discard | -0.3544 | -0.1576 |
| 2026-03-14 05:31 | williams_percent_r | 1.5 → 2.0 | ❌ discard | -0.1538 | 0.0430 |
| 2026-03-14 06:28 | lower_band_reversion | None → 2.5 | ✅ kept | +0.0110 | 0.3966 |
| 2026-03-14 06:28 | lower_band_reversion | None → 2.0 | ❌ discard | +0.0000 | 0.3856 |
| 2026-03-14 06:28 | lower_band_reversion | None → 3.0 | ❌ discard | +0.0011 | 0.3867 |
| 2026-03-14 06:28 | lower_band_reversion | None → 1.5 | ❌ discard | -0.3749 | 0.0107 |
| 2026-03-14 10:01 | donchian_breakout | 1.5 → 2.5 | ❌ discard | -0.2891 | 0.2656 |
| 2026-03-14 10:01 | donchian_breakout | 1.5 → 3.0 | ❌ discard | -0.4722 | 0.0825 |
| 2026-03-14 10:01 | donchian_breakout | 1.5 → 2.0 | ❌ discard | -0.1464 | 0.4083 |
| 2026-03-14 10:05 | stochastic_oversold | None → 1.5 | ❌ discard | -0.4926 | -0.0939 |
| 2026-03-14 10:05 | stochastic_oversold | None → 2.0 | ❌ discard | +0.0000 | 0.3987 |
| 2026-03-14 10:05 | stochastic_oversold | None → 2.5 | ❌ discard | -0.0049 | 0.3938 |
| 2026-03-14 10:05 | stochastic_oversold | None → 3.0 | ❌ discard | -0.0158 | 0.3829 |
| 2026-03-14 10:09 | williams_percent_r | 1.5 → 2.5 | ❌ discard | -0.3544 | -0.1576 |
| 2026-03-14 10:09 | williams_percent_r | 1.5 → 3.0 | ❌ discard | -0.4828 | -0.2860 |
| 2026-03-14 10:09 | williams_percent_r | 1.5 → 2.0 | ❌ discard | -0.1538 | 0.0430 |
| 2026-03-14 10:13 | lower_band_reversion | 2.5 → 2.0 | ❌ discard | -0.0110 | 0.3856 |
| 2026-03-14 10:13 | lower_band_reversion | 2.5 → 3.0 | ❌ discard | -0.0099 | 0.3867 |
| 2026-03-14 10:13 | lower_band_reversion | 2.5 → 1.5 | ❌ discard | -0.3859 | 0.0107 |
