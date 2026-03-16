# ema_period

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
| 2026-03-14 11:58 | keltner_reversion | None → 10 | ❌ discard | -53.0878 | -66.5584 |
| 2026-03-14 11:58 | keltner_reversion | None → 15 | ❌ discard | -7.2365 | -20.7071 |
| 2026-03-14 11:58 | keltner_reversion | None → 20 | ❌ discard | +0.0000 | -13.4706 |
| 2026-03-15 00:26 | keltner_reversion | None → 10 | ✅ kept | +5.5845 | -0.4257 |
| 2026-03-15 00:26 | keltner_reversion | None → 15 | ❌ discard | +0.5630 | -5.4472 |
| 2026-03-15 00:26 | keltner_reversion | None → 20 | ❌ discard | +0.0000 | -6.0102 |
| 2026-03-15 11:20 | keltner_reversion | 10 → 15 | ❌ discard | +0.0023 | -0.0779 |
| 2026-03-15 11:20 | keltner_reversion | 10 → 20 | ❌ discard | -4.0227 | -4.1029 |
