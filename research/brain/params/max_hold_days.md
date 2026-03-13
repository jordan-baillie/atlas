# max_hold_days

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
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
| 2026-03-13 02:49 | momentum_breakout | 15 → 10 | ❌ discard | -0.0426 | 0.6791 |
| 2026-03-13 02:49 | momentum_breakout | 15 → 5 | ❌ discard | -0.0542 | 0.6675 |
| 2026-03-13 02:49 | momentum_breakout | 15 → 20 | ❌ discard | -0.2817 | 0.4400 |
| 2026-03-13 02:51 | short_term_mr | None → 5 | ❌ discard | +0.0000 | 0.5079 |
| 2026-03-13 02:51 | short_term_mr | None → 2 | ❌ discard | -0.0799 | 0.4280 |
| 2026-03-13 02:51 | short_term_mr | None → 3 | ❌ discard | -0.0848 | 0.4231 |
| 2026-03-13 02:51 | short_term_mr | None → 7 | ❌ discard | -0.0012 | 0.5067 |
| 2026-03-13 02:54 | bb_squeeze | 15 → 5 | ❌ discard | -0.1804 | 0.2297 |
| 2026-03-13 02:54 | bb_squeeze | 15 → 10 | ❌ discard | -0.0198 | 0.3903 |
| 2026-03-13 03:29 | consecutive_down_days | None → 10 | ❌ discard | -0.0017 | 0.5885 |
| 2026-03-13 03:29 | consecutive_down_days | None → 7 | ❌ discard | -0.0018 | 0.5884 |
| 2026-03-13 03:29 | consecutive_down_days | None → 5 | ❌ discard | +0.0000 | 0.5902 |
| 2026-03-13 03:29 | consecutive_down_days | None → 3 | ❌ discard | +0.0011 | 0.5913 |
| 2026-03-13 05:31 | momentum_breakout | 15 → 20 | ❌ discard | -0.2679 | 0.4685 |
| 2026-03-13 05:31 | momentum_breakout | 15 → 10 | ❌ discard | -0.0357 | 0.7007 |
| 2026-03-13 05:31 | momentum_breakout | 15 → 5 | ❌ discard | -0.0423 | 0.6941 |
| 2026-03-13 05:33 | short_term_mr | None → 3 | ❌ discard | -0.0592 | 0.4545 |
| 2026-03-13 05:33 | short_term_mr | None → 7 | ❌ discard | -0.0015 | 0.5122 |
| 2026-03-13 05:33 | short_term_mr | None → 5 | ❌ discard | +0.0000 | 0.5137 |
| 2026-03-13 05:33 | short_term_mr | None → 2 | ❌ discard | -0.0568 | 0.4569 |
