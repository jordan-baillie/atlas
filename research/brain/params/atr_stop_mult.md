# atr_stop_mult

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
| 2026-03-12 10:18 | momentum_breakout | 1.5 → 2.0 | ❌ discard | -0.0645 | 0.6146 |
| 2026-03-12 10:18 | momentum_breakout | 1.5 → 2.5 | ❌ discard | -0.0690 | 0.6101 |
| 2026-03-12 10:18 | momentum_breakout | 1.5 → 3.0 | ❌ discard | -0.2189 | 0.4602 |
| 2026-03-12 10:46 | short_term_mr | None → 1.5 | ❌ discard | +0.0000 | 0.5036 |
| 2026-03-12 10:46 | short_term_mr | None → 2.0 | ❌ discard | -0.0284 | 0.4752 |
| 2026-03-12 10:46 | short_term_mr | None → 2.5 | ❌ discard | -0.0386 | 0.4650 |
| 2026-03-12 10:49 | bb_squeeze | 2.0 → 3.0 | ❌ discard | -0.0774 | 0.3327 |
| 2026-03-12 10:49 | bb_squeeze | 2.0 → 1.5 | ❌ discard | -0.2768 | 0.1333 |
| 2026-03-12 10:49 | bb_squeeze | 2.0 → 2.5 | ❌ discard | -0.0461 | 0.3640 |
| 2026-03-12 11:38 | adx_trend_pullback | 1.5 → 2.0 | ❌ discard | -0.1098 | 0.3049 |
| 2026-03-12 11:38 | adx_trend_pullback | 1.5 → 2.5 | ❌ discard | -0.0104 | 0.4043 |
| 2026-03-12 11:38 | adx_trend_pullback | 1.5 → 3.0 | ❌ discard | -0.0346 | 0.3801 |
| 2026-03-12 13:15 | trend_following | 1.5 → 2.5 | ❌ discard | -0.2517 | 0.3698 |
| 2026-03-12 13:15 | trend_following | 1.5 → 2.0 | ❌ discard | -0.1988 | 0.4227 |
| 2026-03-12 13:15 | trend_following | 1.5 → 3.0 | ❌ discard | -0.2855 | 0.3360 |
| 2026-03-12 13:19 | momentum_breakout | 1.5 → 3.0 | ❌ discard | -0.1822 | 0.5395 |
| 2026-03-12 13:19 | momentum_breakout | 1.5 → 2.5 | ❌ discard | -0.2167 | 0.5050 |
| 2026-03-12 13:19 | momentum_breakout | 1.5 → 2.0 | ❌ discard | -0.2042 | 0.5175 |
| 2026-03-12 13:21 | short_term_mr | None → 1.5 | ❌ discard | +0.0000 | 0.5079 |
| 2026-03-12 13:21 | short_term_mr | None → 2.5 | ❌ discard | -0.0405 | 0.4674 |
| 2026-03-12 13:21 | short_term_mr | None → 2.0 | ❌ discard | -0.0284 | 0.4795 |
| 2026-03-12 13:24 | bb_squeeze | 2.0 → 1.5 | ❌ discard | -0.2768 | 0.1333 |
| 2026-03-12 13:24 | bb_squeeze | 2.0 → 2.5 | ❌ discard | -0.0461 | 0.3640 |
| 2026-03-12 13:24 | bb_squeeze | 2.0 → 3.0 | ❌ discard | -0.0774 | 0.3327 |
| 2026-03-12 13:29 | adx_trend_pullback | 1.5 → 2.5 | ❌ discard | -0.0104 | 0.4043 |
| 2026-03-12 13:29 | adx_trend_pullback | 1.5 → 3.0 | ❌ discard | -0.0346 | 0.3801 |
| 2026-03-12 13:29 | adx_trend_pullback | 1.5 → 2.0 | ❌ discard | -0.1098 | 0.3049 |
| 2026-03-12 13:58 | consecutive_down_days | None → 1.5 | ❌ discard | -0.0029 | 0.5712 |
| 2026-03-12 13:58 | consecutive_down_days | None → 2.5 | ❌ discard | -0.0129 | 0.5612 |
| 2026-03-12 13:58 | consecutive_down_days | None → 3.0 | ❌ discard | -0.0189 | 0.5552 |
| 2026-03-12 13:58 | consecutive_down_days | None → 2.0 | ❌ discard | +0.0000 | 0.5741 |
| 2026-03-12 23:44 | trend_following | 1.5 → 2.0 | ❌ discard | -0.1988 | 0.4227 |
| 2026-03-12 23:44 | trend_following | 1.5 → 3.0 | ❌ discard | -0.2855 | 0.3360 |
| 2026-03-12 23:44 | trend_following | 1.5 → 2.5 | ❌ discard | -0.2517 | 0.3698 |
| 2026-03-12 23:48 | momentum_breakout | 1.5 → 2.5 | ❌ discard | -0.2167 | 0.5050 |
| 2026-03-12 23:48 | momentum_breakout | 1.5 → 3.0 | ❌ discard | -0.1822 | 0.5395 |
| 2026-03-12 23:48 | momentum_breakout | 1.5 → 2.0 | ❌ discard | -0.2042 | 0.5175 |
| 2026-03-12 23:51 | short_term_mr | None → 2.5 | ❌ discard | -0.0405 | 0.4674 |
| 2026-03-12 23:51 | short_term_mr | None → 2.0 | ❌ discard | -0.0284 | 0.4795 |
| 2026-03-12 23:51 | short_term_mr | None → 1.5 | ❌ discard | +0.0000 | 0.5079 |
| 2026-03-12 23:53 | bb_squeeze | 2.0 → 2.5 | ❌ discard | -0.0461 | 0.3640 |
| 2026-03-12 23:53 | bb_squeeze | 2.0 → 1.5 | ❌ discard | -0.2768 | 0.1333 |
| 2026-03-12 23:53 | bb_squeeze | 2.0 → 3.0 | ❌ discard | -0.0774 | 0.3327 |
| 2026-03-12 23:59 | adx_trend_pullback | 1.5 → 2.0 | ❌ discard | -0.1098 | 0.3049 |
| 2026-03-12 23:59 | adx_trend_pullback | 1.5 → 2.5 | ❌ discard | -0.0104 | 0.4043 |
| 2026-03-12 23:59 | adx_trend_pullback | 1.5 → 3.0 | ❌ discard | -0.0346 | 0.3801 |
| 2026-03-13 00:26 | consecutive_down_days | None → 3.0 | ❌ discard | -0.0189 | 0.5552 |
| 2026-03-13 00:26 | consecutive_down_days | None → 1.5 | ❌ discard | -0.0029 | 0.5712 |
| 2026-03-13 00:26 | consecutive_down_days | None → 2.0 | ❌ discard | +0.0000 | 0.5741 |
| 2026-03-13 00:26 | consecutive_down_days | None → 2.5 | ❌ discard | -0.0129 | 0.5612 |
