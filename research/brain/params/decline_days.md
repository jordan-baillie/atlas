# decline_days

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
| 2026-03-14 11:21 | triple_rsi | None → 2 | ✅ kept | +0.2497 | 0.0643 |
| 2026-03-14 11:21 | triple_rsi | None → 4 | ❌ discard | -5.5120 | -5.6974 |
| 2026-03-14 11:21 | triple_rsi | None → 3 | ❌ discard | +0.0000 | -0.1854 |
| 2026-03-14 23:19 | triple_rsi | 2 → 3 | ❌ discard | -0.2497 | -0.1854 |
| 2026-03-14 23:19 | triple_rsi | 2 → 4 | ❌ discard | -5.7617 | -5.6974 |
| 2026-03-15 11:16 | triple_rsi | 2 → 4 | ❌ discard | -4.3830 | -4.1842 |
| 2026-03-15 11:16 | triple_rsi | 2 → 3 | ❌ discard | -0.3007 | -0.1019 |
| 2026-03-16 07:00 | triple_rsi | 2 → 4 | ❌ discard | -4.9397 | -4.6648 |
| 2026-03-16 07:00 | triple_rsi | 2 → 3 | ❌ discard | -3.6201 | -3.3452 |
