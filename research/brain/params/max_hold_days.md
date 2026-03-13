# max_hold_days

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
| 2026-03-12 05:38 | momentum_breakout | 10 → 15 | ❌ discard | -0.0333 | 0.6214 |
| 2026-03-12 05:40 | short_term_mr | None → 7 | ❌ discard | +0.0017 | 0.4118 |
| 2026-03-12 05:40 | short_term_mr | None → 2 | ❌ discard | -0.1738 | 0.2363 |
| 2026-03-12 05:40 | short_term_mr | None → 5 | ❌ discard | +0.0000 | 0.4101 |
| 2026-03-12 05:40 | short_term_mr | None → 3 | ❌ discard | -0.1581 | 0.2520 |
| 2026-03-12 05:43 | bb_squeeze | 15 → 10 | ❌ discard | -0.0778 | 0.3985 |
| 2026-03-12 05:43 | bb_squeeze | 15 → 5 | ❌ discard | -0.2736 | 0.2027 |
| 2026-03-12 07:02 | adx_trend_pullback | None → 7 | ❌ discard | -0.1750 | 0.3295 |
| 2026-03-12 07:02 | adx_trend_pullback | None → 10 | ❌ discard | +0.0000 | 0.5045 |
| 2026-03-12 07:02 | adx_trend_pullback | None → 5 | ❌ discard | -0.0412 | 0.4633 |
| 2026-03-12 07:02 | adx_trend_pullback | None → 15 | ❌ discard | -0.3021 | 0.2024 |
| 2026-03-12 07:08 | consecutive_down_days | None → 3 | ❌ discard | -0.0007 | 0.5736 |
| 2026-03-12 07:08 | consecutive_down_days | None → 10 | ❌ discard | -0.0017 | 0.5726 |
| 2026-03-12 07:08 | consecutive_down_days | None → 7 | ❌ discard | -0.0015 | 0.5728 |
| 2026-03-12 07:08 | consecutive_down_days | None → 5 | ❌ discard | +0.0000 | 0.5743 |
| 2026-03-12 10:31 | momentum_breakout | 10 → 15 | ✅ kept | +0.0426 | 0.7217 |
| 2026-03-12 10:31 | momentum_breakout | 10 → 20 | ❌ discard | -0.2391 | 0.4400 |
| 2026-03-12 10:31 | momentum_breakout | 10 → 5 | ❌ discard | -0.0116 | 0.6675 |
| 2026-03-12 10:46 | short_term_mr | None → 7 | ❌ discard | -0.0019 | 0.5017 |
| 2026-03-12 10:46 | short_term_mr | None → 2 | ❌ discard | -0.0868 | 0.4168 |
| 2026-03-12 10:46 | short_term_mr | None → 3 | ❌ discard | -0.0904 | 0.4132 |
| 2026-03-12 10:46 | short_term_mr | None → 5 | ❌ discard | +0.0000 | 0.5036 |
| 2026-03-12 10:49 | bb_squeeze | 15 → 10 | ❌ discard | -0.0198 | 0.3903 |
| 2026-03-12 10:49 | bb_squeeze | 15 → 5 | ❌ discard | -0.1804 | 0.2297 |
| 2026-03-12 13:19 | momentum_breakout | 15 → 20 | ❌ discard | -0.2817 | 0.4400 |
| 2026-03-12 13:19 | momentum_breakout | 15 → 10 | ❌ discard | -0.0426 | 0.6791 |
| 2026-03-12 13:19 | momentum_breakout | 15 → 5 | ❌ discard | -0.0542 | 0.6675 |
| 2026-03-12 13:21 | short_term_mr | None → 7 | ❌ discard | -0.0012 | 0.5067 |
| 2026-03-12 13:21 | short_term_mr | None → 2 | ❌ discard | -0.0799 | 0.4280 |
| 2026-03-12 13:21 | short_term_mr | None → 3 | ❌ discard | -0.0848 | 0.4231 |
| 2026-03-12 13:21 | short_term_mr | None → 5 | ❌ discard | +0.0000 | 0.5079 |
| 2026-03-12 13:24 | bb_squeeze | 15 → 10 | ❌ discard | -0.0198 | 0.3903 |
| 2026-03-12 13:24 | bb_squeeze | 15 → 5 | ❌ discard | -0.1804 | 0.2297 |
| 2026-03-12 13:59 | consecutive_down_days | None → 3 | ❌ discard | -0.0010 | 0.5731 |
| 2026-03-12 13:59 | consecutive_down_days | None → 10 | ❌ discard | -0.0023 | 0.5718 |
| 2026-03-12 13:59 | consecutive_down_days | None → 7 | ❌ discard | -0.0015 | 0.5726 |
| 2026-03-12 13:59 | consecutive_down_days | None → 5 | ❌ discard | +0.0000 | 0.5741 |
| 2026-03-12 23:48 | momentum_breakout | 15 → 5 | ❌ discard | -0.0542 | 0.6675 |
| 2026-03-12 23:48 | momentum_breakout | 15 → 10 | ❌ discard | -0.0426 | 0.6791 |
| 2026-03-12 23:48 | momentum_breakout | 15 → 20 | ❌ discard | -0.2817 | 0.4400 |
| 2026-03-12 23:50 | short_term_mr | None → 7 | ❌ discard | -0.0012 | 0.5067 |
| 2026-03-12 23:50 | short_term_mr | None → 3 | ❌ discard | -0.0848 | 0.4231 |
| 2026-03-12 23:50 | short_term_mr | None → 2 | ❌ discard | -0.0799 | 0.4280 |
| 2026-03-12 23:50 | short_term_mr | None → 5 | ❌ discard | +0.0000 | 0.5079 |
| 2026-03-12 23:54 | bb_squeeze | 15 → 5 | ❌ discard | -0.1804 | 0.2297 |
| 2026-03-12 23:54 | bb_squeeze | 15 → 10 | ❌ discard | -0.0198 | 0.3903 |
| 2026-03-13 00:27 | consecutive_down_days | None → 5 | ❌ discard | +0.0000 | 0.5741 |
| 2026-03-13 00:27 | consecutive_down_days | None → 3 | ❌ discard | -0.0010 | 0.5731 |
| 2026-03-13 00:27 | consecutive_down_days | None → 10 | ❌ discard | -0.0023 | 0.5718 |
| 2026-03-13 00:27 | consecutive_down_days | None → 7 | ❌ discard | -0.0015 | 0.5726 |
