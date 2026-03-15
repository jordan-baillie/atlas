# atr_stop_mult

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
<<<<<<< Updated upstream
| 2026-03-14 10:05 | stochastic_oversold | None → 2.5 | ❌ discard | -0.0049 | 0.3938 |
=======
>>>>>>> Stashed changes
| 2026-03-14 10:05 | stochastic_oversold | None → 3.0 | ❌ discard | -0.0158 | 0.3829 |
| 2026-03-14 10:09 | williams_percent_r | 1.5 → 2.5 | ❌ discard | -0.3544 | -0.1576 |
| 2026-03-14 10:09 | williams_percent_r | 1.5 → 3.0 | ❌ discard | -0.4828 | -0.2860 |
| 2026-03-14 10:09 | williams_percent_r | 1.5 → 2.0 | ❌ discard | -0.1538 | 0.0430 |
| 2026-03-14 10:13 | lower_band_reversion | 2.5 → 2.0 | ❌ discard | -0.0110 | 0.3856 |
| 2026-03-14 10:13 | lower_band_reversion | 2.5 → 3.0 | ❌ discard | -0.0099 | 0.3867 |
| 2026-03-14 10:13 | lower_band_reversion | 2.5 → 1.5 | ❌ discard | -0.3859 | 0.0107 |
| 2026-03-14 23:02 | short_term_mr | None → 2.0 | ❌ discard | -0.0255 | 0.4882 |
| 2026-03-14 23:02 | short_term_mr | None → 2.5 | ❌ discard | -0.0182 | 0.4955 |
| 2026-03-14 23:02 | short_term_mr | None → 1.5 | ❌ discard | +0.0000 | 0.5137 |
| 2026-03-14 23:06 | adx_trend_pullback | 1.5 → 2.5 | ❌ discard | -0.0078 | 0.3823 |
| 2026-03-14 23:06 | adx_trend_pullback | 1.5 → 2.0 | ❌ discard | -2.1646 | -1.7745 |
| 2026-03-14 23:06 | adx_trend_pullback | 1.5 → 3.0 | ❌ discard | -0.0199 | 0.3702 |
| 2026-03-14 23:08 | donchian_breakout | 1.5 → 2.0 | ❌ discard | -0.1396 | 0.4070 |
| 2026-03-14 23:08 | donchian_breakout | 1.5 → 3.0 | ❌ discard | -0.4641 | 0.0825 |
| 2026-03-14 23:08 | donchian_breakout | 1.5 → 2.5 | ❌ discard | -0.2807 | 0.2659 |
| 2026-03-14 23:11 | williams_percent_r | 1.5 → 2.5 | ❌ discard | -0.3544 | -0.1576 |
| 2026-03-14 23:11 | williams_percent_r | 1.5 → 2.0 | ❌ discard | -0.1538 | 0.0430 |
| 2026-03-14 23:11 | williams_percent_r | 1.5 → 3.0 | ❌ discard | -0.4828 | -0.2860 |
| 2026-03-14 23:16 | lower_band_reversion | 2.5 → 1.5 | ❌ discard | -0.3859 | 0.0107 |
| 2026-03-14 23:16 | lower_band_reversion | 2.5 → 3.0 | ❌ discard | -0.0099 | 0.3867 |
| 2026-03-14 23:16 | lower_band_reversion | 2.5 → 2.0 | ❌ discard | -0.0110 | 0.3856 |
| 2026-03-14 23:52 | triple_rsi | None → 2.0 | ✅ kept | +0.1345 | 0.1988 |
| 2026-03-14 23:52 | triple_rsi | None → 2.5 | ❌ discard | +0.0000 | 0.0643 |
| 2026-03-14 23:52 | triple_rsi | None → 3.0 | ❌ discard | -0.0904 | -0.0261 |
| 2026-03-14 23:52 | triple_rsi | None → 1.5 | ❌ discard | -0.0431 | 0.0212 |
| 2026-03-15 00:59 | keltner_reversion | None → 1.5 | ✅ kept | +0.3455 | -0.0802 |
| 2026-03-15 00:59 | keltner_reversion | None → 2.0 | ❌ discard | +0.1672 | -0.2585 |
| 2026-03-15 00:59 | keltner_reversion | None → 3.0 | ❌ discard | -0.1688 | -0.5945 |
| 2026-03-15 00:59 | keltner_reversion | None → 2.5 | ❌ discard | +0.0000 | -0.4257 |
| 2026-03-15 02:02 | demark_sequential | 1.5 → 2.0 | ❌ discard | -0.5822 | -2.5319 |
| 2026-03-15 02:02 | demark_sequential | 1.5 → 2.5 | ❌ discard | -2.0643 | -4.0140 |
| 2026-03-15 02:02 | demark_sequential | 1.5 → 3.0 | ❌ discard | -3.1732 | -5.1229 |
| 2026-03-15 02:33 | donchian_breakout | 1.5 → 3.0 | ❌ discard | -1.3554 | -0.9905 |
| 2026-03-15 02:33 | donchian_breakout | 1.5 → 2.0 | ❌ discard | -2.2080 | -1.8431 |
| 2026-03-15 02:33 | donchian_breakout | 1.5 → 2.5 | ❌ discard | -1.1882 | -0.8233 |
| 2026-03-15 03:05 | stochastic_oversold | None → 3.0 | ❌ discard | -0.0167 | 0.3824 |
| 2026-03-15 03:05 | stochastic_oversold | None → 2.5 | ❌ discard | -0.0056 | 0.3935 |
| 2026-03-15 03:05 | stochastic_oversold | None → 2.0 | ❌ discard | +0.0000 | 0.3991 |
| 2026-03-15 03:05 | stochastic_oversold | None → 1.5 | ❌ discard | -0.4507 | -0.0516 |
| 2026-03-15 05:28 | demark_sequential | 1.5 → 2.0 | ❌ discard | -2.6441 | -2.9362 |
| 2026-03-15 05:28 | demark_sequential | 1.5 → 2.5 | ❌ discard | -3.5557 | -3.8478 |
| 2026-03-15 05:28 | demark_sequential | 1.5 → 3.0 | ❌ discard | -4.2484 | -4.5405 |
| 2026-03-15 06:25 | donchian_breakout | 1.5 → 2.5 | ❌ discard | -1.1882 | -0.8233 |
| 2026-03-15 06:25 | donchian_breakout | 1.5 → 2.0 | ❌ discard | -2.2080 | -1.8431 |
| 2026-03-15 06:25 | donchian_breakout | 1.5 → 3.0 | ❌ discard | -1.3554 | -0.9905 |
<<<<<<< Updated upstream
| 2026-03-15 07:00 | williams_percent_r | 1.5 → 2.5 | ❌ discard | -2.8061 | -2.8368 |
| 2026-03-15 07:00 | williams_percent_r | 1.5 → 2.0 | ❌ discard | -0.2158 | -0.2465 |
| 2026-03-15 07:00 | williams_percent_r | 1.5 → 3.0 | ❌ discard | -3.5710 | -3.6017 |
=======
| 2026-03-15 06:29 | stochastic_oversold | None → 2.5 | ❌ discard | -0.0056 | 0.3935 |
| 2026-03-15 06:29 | stochastic_oversold | None → 3.0 | ❌ discard | -0.0167 | 0.3824 |
| 2026-03-15 06:29 | stochastic_oversold | None → 1.5 | ❌ discard | -0.4507 | -0.0516 |
| 2026-03-15 06:29 | stochastic_oversold | None → 2.0 | ❌ discard | +0.0000 | 0.3991 |
>>>>>>> Stashed changes
