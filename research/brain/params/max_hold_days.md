# max_hold_days

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
| 2026-03-12 05:38 | momentum_breakout | 10 → 20 | ❌ discard | -0.2827 | 0.3720 |
| 2026-03-12 05:38 | momentum_breakout | 10 → 5 | ❌ discard | -0.0013 | 0.6534 |
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
