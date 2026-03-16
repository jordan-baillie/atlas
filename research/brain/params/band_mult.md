# band_mult

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
| 2026-03-14 03:40 | lower_band_reversion | None → 2.0 | ✅ kept | +0.0359 | -0.1649 |
| 2026-03-14 03:40 | lower_band_reversion | None → 2.5 | ❌ discard | +0.0000 | -0.2008 |
| 2026-03-14 03:40 | lower_band_reversion | None → 1.5 | ❌ discard | -2.5009 | -2.7017 |
| 2026-03-14 03:40 | lower_band_reversion | None → 1.0 | ❌ discard | -2.2352 | -2.4360 |
| 2026-03-14 05:33 | lower_band_reversion | 2.0 → 2.5 | ❌ discard | -0.0359 | -0.2008 |
| 2026-03-14 05:33 | lower_band_reversion | 2.0 → 1.5 | ❌ discard | -2.5368 | -2.7017 |
| 2026-03-14 05:33 | lower_band_reversion | 2.0 → 1.0 | ❌ discard | -2.2711 | -2.4360 |
| 2026-03-14 10:11 | lower_band_reversion | 2.0 → 2.5 | ❌ discard | -0.0221 | 0.3745 |
| 2026-03-14 10:11 | lower_band_reversion | 2.0 → 1.5 | ❌ discard | +0.0003 | 0.3969 |
| 2026-03-14 10:11 | lower_band_reversion | 2.0 → 1.0 | ❌ discard | -0.1172 | 0.2794 |
| 2026-03-14 23:14 | lower_band_reversion | 2.0 → 2.5 | ❌ discard | -0.0221 | 0.3745 |
| 2026-03-14 23:14 | lower_band_reversion | 2.0 → 1.5 | ❌ discard | +0.0003 | 0.3969 |
| 2026-03-14 23:14 | lower_band_reversion | 2.0 → 1.0 | ❌ discard | -0.1172 | 0.2794 |
| 2026-03-15 11:10 | lower_band_reversion | 2.0 → 1.5 | ✅ kept | +0.6508 | 0.3934 |
| 2026-03-15 11:10 | lower_band_reversion | 2.0 → 2.5 | ❌ discard | +0.6304 | 0.3730 |
| 2026-03-15 11:10 | lower_band_reversion | 2.0 → 1.0 | ❌ discard | +0.5387 | 0.2813 |
| 2026-03-16 01:11 | lower_band_reversion | 1.5 → 2.5 | ❌ discard | -0.0204 | 0.3730 |
| 2026-03-16 01:11 | lower_band_reversion | 1.5 → 2.0 | ❌ discard | -0.6508 | -0.2574 |
| 2026-03-16 01:11 | lower_band_reversion | 1.5 → 1.0 | ❌ discard | -0.1121 | 0.2813 |
| 2026-03-16 06:20 | lower_band_reversion | 1.5 → 2.5 | ❌ discard | -0.0212 | 0.3741 |
| 2026-03-16 06:20 | lower_band_reversion | 1.5 → 2.0 | ❌ discard | -0.6248 | -0.2295 |
| 2026-03-16 06:20 | lower_band_reversion | 1.5 → 1.0 | ❌ discard | -0.0961 | 0.2992 |
