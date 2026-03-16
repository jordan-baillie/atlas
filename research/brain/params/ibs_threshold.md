# ibs_threshold

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
| 2026-03-12 07:06 | consecutive_down_days | 1.0 → 0.2 | ❌ discard | -0.2241 | 0.3502 |
| 2026-03-12 07:06 | consecutive_down_days | 1.0 → 0.3 | ❌ discard | -0.2054 | 0.3689 |
| 2026-03-12 07:06 | consecutive_down_days | 1.0 → 0.5 | ❌ discard | -0.2063 | 0.3680 |
| 2026-03-12 13:57 | consecutive_down_days | 1.0 → 0.2 | ❌ discard | -0.2213 | 0.3528 |
| 2026-03-12 13:57 | consecutive_down_days | 1.0 → 0.3 | ❌ discard | -0.2447 | 0.3294 |
| 2026-03-12 13:57 | consecutive_down_days | 1.0 → 0.5 | ❌ discard | -0.2012 | 0.3729 |
| 2026-03-13 00:26 | consecutive_down_days | 1.0 → 0.2 | ❌ discard | -0.2213 | 0.3528 |
| 2026-03-13 00:26 | consecutive_down_days | 1.0 → 0.3 | ❌ discard | -0.2447 | 0.3294 |
| 2026-03-13 00:26 | consecutive_down_days | 1.0 → 0.5 | ❌ discard | -0.2012 | 0.3729 |
| 2026-03-13 03:27 | consecutive_down_days | 1.0 → 0.2 | ❌ discard | -0.2202 | 0.3700 |
| 2026-03-13 03:27 | consecutive_down_days | 1.0 → 0.3 | ❌ discard | -0.2088 | 0.3814 |
| 2026-03-13 03:27 | consecutive_down_days | 1.0 → 0.5 | ❌ discard | -0.1848 | 0.4054 |
| 2026-03-14 06:00 | lower_band_reversion | None → 0.5 | ✅ kept | +0.5505 | 0.3856 |
| 2026-03-14 06:00 | lower_band_reversion | None → 0.2 | ❌ discard | +0.3661 | 0.2012 |
| 2026-03-14 06:00 | lower_band_reversion | None → 0.3 | ❌ discard | +0.0000 | -0.1649 |
| 2026-03-14 10:12 | lower_band_reversion | 0.5 → 0.2 | ❌ discard | -0.0053 | 0.3913 |
| 2026-03-14 10:12 | lower_band_reversion | 0.5 → 0.3 | ❌ discard | -0.0021 | 0.3945 |
| 2026-03-14 23:14 | lower_band_reversion | 0.5 → 0.2 | ❌ discard | -0.0053 | 0.3913 |
| 2026-03-14 23:14 | lower_band_reversion | 0.5 → 0.3 | ❌ discard | -0.0021 | 0.3945 |
| 2026-03-15 10:02 | consecutive_down_days | 1.0 → 0.2 | ❌ discard | -0.1256 | 0.3669 |
| 2026-03-15 10:02 | consecutive_down_days | 1.0 → 0.3 | ❌ discard | -0.1217 | 0.3708 |
| 2026-03-15 10:02 | consecutive_down_days | 1.0 → 0.5 | ❌ discard | -0.0005 | 0.4920 |
| 2026-03-15 11:11 | lower_band_reversion | 0.5 → 0.2 | ❌ discard | +0.0027 | 0.3961 |
| 2026-03-15 11:11 | lower_band_reversion | 0.5 → 0.3 | ❌ discard | -3.4832 | -3.0898 |
| 2026-03-16 00:55 | consecutive_down_days | 1.0 → 0.2 | ❌ discard | -0.1256 | 0.3669 |
| 2026-03-16 00:55 | consecutive_down_days | 1.0 → 0.3 | ❌ discard | -0.1217 | 0.3708 |
| 2026-03-16 00:55 | consecutive_down_days | 1.0 → 0.5 | ❌ discard | -0.0005 | 0.4920 |
