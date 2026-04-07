# max_hold_days

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
| 2026-03-16 13:09 | adx_trend_pullback | 7 → 15 | ❌ discard | -0.5038 | 0.4479 |
| 2026-03-16 13:09 | adx_trend_pullback | 7 → 5 | ❌ discard | -0.5461 | 0.4056 |
| 2026-03-16 13:09 | adx_trend_pullback | 7 → 10 | ❌ discard | -0.5044 | 0.4473 |
| 2026-03-16 13:17 | consecutive_down_days | None → 3 | ❌ discard | -0.4283 | 0.4945 |
| 2026-03-16 13:17 | consecutive_down_days | None → 5 | ❌ discard | -0.4305 | 0.4923 |
| 2026-03-16 13:17 | consecutive_down_days | None → 7 | ❌ discard | -0.4313 | 0.4915 |
| 2026-03-16 13:17 | consecutive_down_days | None → 10 | ❌ discard | -0.4322 | 0.4906 |
| 2026-03-16 13:22 | demark_sequential | 7 → 5 | ❌ discard | -1.1006 | -0.1235 |
| 2026-03-16 13:22 | demark_sequential | 7 → 10 | ❌ discard | -0.8957 | 0.0814 |
| 2026-03-16 13:22 | demark_sequential | 7 → 15 | ❌ discard | -0.8346 | 0.1425 |
| 2026-04-02 04:11 | momentum_breakout | 15 → 5 | ❌ discard | +0.0000 | 0.6308 |
| 2026-04-02 04:11 | momentum_breakout | 15 → 20 | ❌ discard | +0.0000 | 0.6308 |
| 2026-04-02 04:11 | momentum_breakout | 15 → 10 | ❌ discard | +0.0000 | 0.6308 |
| 2026-04-02 04:11 | momentum_breakout | 15 → 11 | ❌ discard | +0.0000 | 0.6308 |
| 2026-04-02 04:11 | momentum_breakout | 15 → 18 | ❌ discard | +0.0000 | 0.6308 |
| 2026-04-02 04:11 | momentum_breakout | 15 → 14 | ❌ discard | +0.0000 | 0.6308 |
| 2026-04-02 04:32 | momentum_breakout | 15 → 10 | ❌ discard | +0.0000 | 0.7402 |
| 2026-04-02 04:32 | momentum_breakout | 15 → 5 | ❌ discard | +0.0000 | 0.7402 |
| 2026-04-02 04:32 | momentum_breakout | 15 → 11 | ❌ discard | +0.0000 | 0.7402 |
| 2026-04-02 04:32 | momentum_breakout | 15 → 20 | ❌ discard | +0.0000 | 0.7402 |
| 2026-04-02 04:32 | momentum_breakout | 15 → 12 | ❌ discard | +0.0000 | 0.7402 |
| 2026-04-02 04:32 | momentum_breakout | 15 → 13 | ❌ discard | +0.0000 | 0.7402 |
| 2026-04-02 05:08 | momentum_breakout | 15 → 20 | ❌ discard | +0.0000 | 0.8275 |
| 2026-04-02 05:08 | momentum_breakout | 15 → 10 | ❌ discard | +0.0000 | 0.8275 |
| 2026-04-02 05:08 | momentum_breakout | 15 → 9 | ❌ discard | +0.0000 | 0.8275 |
| 2026-04-02 05:08 | momentum_breakout | 15 → 5 | ❌ discard | +0.0000 | 0.8275 |
| 2026-04-02 05:08 | momentum_breakout | 15 → 12 | ❌ discard | +0.0000 | 0.8275 |
| 2026-04-02 05:08 | momentum_breakout | 15 → 17 | ❌ discard | +0.0000 | 0.8275 |
| 2026-04-02 05:13 | connors_rsi2 | 10 → 5 | ✅ kept | +0.0557 | 0.5735 |
| 2026-04-02 05:13 | connors_rsi2 | 10 → 3 | ❌ discard | -0.0010 | 0.5168 |
| 2026-04-02 05:13 | connors_rsi2 | 10 → 9 | ❌ discard | +0.0000 | 0.5178 |
| 2026-04-02 05:13 | connors_rsi2 | 10 → 7 | ❌ discard | -0.0037 | 0.5141 |
| 2026-04-02 05:13 | connors_rsi2 | 10 → 11 | ❌ discard | +0.0000 | 0.5178 |
| 2026-04-02 05:13 | connors_rsi2 | 10 → 12 | ❌ discard | -0.0102 | 0.5076 |
| 2026-04-02 05:45 | connors_rsi2 | 10 → 5 | ✅ kept | +0.0557 | 0.5735 |
| 2026-04-02 05:45 | connors_rsi2 | 10 → 7 | ❌ discard | -0.0037 | 0.5141 |
| 2026-04-02 05:45 | connors_rsi2 | 10 → 8 | ❌ discard | +0.0076 | 0.5254 |
| 2026-04-02 05:45 | connors_rsi2 | 10 → 3 | ❌ discard | -0.0010 | 0.5168 |
| 2026-04-02 05:45 | connors_rsi2 | 10 → 9 | ❌ discard | +0.0000 | 0.5178 |
| 2026-04-02 05:45 | connors_rsi2 | 10 → 11 | ❌ discard | +0.0000 | 0.5178 |
| 2026-04-02 05:49 | connors_rsi2 | 10 → 5 | ✅ kept | +0.0557 | 0.5735 |
| 2026-04-02 05:49 | connors_rsi2 | 10 → 7 | ❌ discard | -0.0037 | 0.5141 |
| 2026-04-02 05:49 | connors_rsi2 | 10 → 8 | ❌ discard | +0.0076 | 0.5254 |
| 2026-04-02 05:49 | connors_rsi2 | 10 → 3 | ❌ discard | -0.0010 | 0.5168 |
| 2026-04-02 05:49 | connors_rsi2 | 10 → 9 | ❌ discard | +0.0000 | 0.5178 |
| 2026-04-02 05:49 | connors_rsi2 | 10 → 11 | ❌ discard | +0.0000 | 0.5178 |
| 2026-04-02 05:55 | connors_rsi2 | 10 → 5 | ✅ kept | +0.0147 | 0.2987 |
| 2026-04-02 05:55 | connors_rsi2 | 10 → 8 | ❌ discard | -0.0499 | 0.2341 |
| 2026-04-02 06:00 | connors_rsi2 | 10 → 5 | ✅ kept | +0.0147 | 0.2987 |
| 2026-04-02 06:06 | connors_rsi2 | 10 → 5 | ✅ kept | +0.0147 | 0.2987 |
