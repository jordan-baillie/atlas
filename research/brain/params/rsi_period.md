# rsi_period

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
| 2026-03-13 02:50 | short_term_mr | None → 5 | ❌ discard | -0.1162 | 0.3917 |
| 2026-03-13 02:50 | short_term_mr | None → 3 | ❌ discard | -0.0939 | 0.4140 |
| 2026-03-13 02:50 | short_term_mr | None → 4 | ❌ discard | -0.1148 | 0.3931 |
| 2026-03-13 02:50 | short_term_mr | None → 2 | ❌ discard | +0.0000 | 0.5079 |
| 2026-03-13 05:32 | short_term_mr | None → 5 | ❌ discard | -0.1197 | 0.3940 |
| 2026-03-13 05:32 | short_term_mr | None → 4 | ❌ discard | -0.1169 | 0.3968 |
| 2026-03-13 05:32 | short_term_mr | None → 3 | ❌ discard | -0.0950 | 0.4187 |
| 2026-03-13 05:32 | short_term_mr | None → 2 | ❌ discard | +0.0000 | 0.5137 |
| 2026-03-13 23:01 | short_term_mr | None → 5 | ❌ discard | -0.1197 | 0.3940 |
| 2026-03-13 23:01 | short_term_mr | None → 4 | ❌ discard | -0.1169 | 0.3968 |
| 2026-03-13 23:01 | short_term_mr | None → 3 | ❌ discard | -0.0950 | 0.4187 |
| 2026-03-13 23:01 | short_term_mr | None → 2 | ❌ discard | +0.0000 | 0.5137 |
| 2026-03-14 02:00 | short_term_mr | None → 4 | ❌ discard | -0.1169 | 0.3968 |
| 2026-03-14 02:00 | short_term_mr | None → 5 | ❌ discard | -0.1197 | 0.3940 |
| 2026-03-14 02:00 | short_term_mr | None → 3 | ❌ discard | -0.0950 | 0.4187 |
| 2026-03-14 02:00 | short_term_mr | None → 2 | ❌ discard | +0.0000 | 0.5137 |
| 2026-03-14 07:03 | triple_rsi | None → 7 | ✅ kept | +4.3510 | -0.2234 |
| 2026-03-14 07:03 | triple_rsi | None → 5 | ❌ discard | +0.0000 | -4.5744 |
| 2026-03-14 07:03 | triple_rsi | None → 3 | ❌ discard | -0.0725 | -4.6469 |
| 2026-03-14 10:15 | triple_rsi | 7 → 5 | ❌ discard | -4.3510 | -4.5744 |
| 2026-03-14 10:15 | triple_rsi | 7 → 3 | ❌ discard | -4.4235 | -4.6469 |
| 2026-03-14 23:00 | short_term_mr | None → 5 | ❌ discard | -0.1197 | 0.3940 |
| 2026-03-14 23:00 | short_term_mr | None → 4 | ❌ discard | -0.1169 | 0.3968 |
| 2026-03-14 23:00 | short_term_mr | None → 3 | ❌ discard | -0.0950 | 0.4187 |
| 2026-03-14 23:00 | short_term_mr | None → 2 | ❌ discard | +0.0000 | 0.5137 |
| 2026-03-14 23:17 | triple_rsi | 7 → 5 | ❌ discard | -0.2446 | -0.1803 |
| 2026-03-14 23:17 | triple_rsi | 7 → 3 | ❌ discard | -0.3232 | -0.2589 |
| 2026-03-15 11:15 | triple_rsi | 7 → 5 | ❌ discard | -0.2204 | -0.0216 |
| 2026-03-15 11:15 | triple_rsi | 7 → 3 | ❌ discard | -0.2391 | -0.0403 |
| 2026-03-15 23:31 | short_term_mr | None → 5 | ❌ discard | -0.0444 | 0.4521 |
| 2026-03-15 23:31 | short_term_mr | None → 4 | ❌ discard | -0.0572 | 0.4393 |
| 2026-03-15 23:31 | short_term_mr | None → 3 | ❌ discard | -0.0308 | 0.4657 |
| 2026-03-15 23:31 | short_term_mr | None → 2 | ❌ discard | +0.0000 | 0.4965 |
| 2026-03-16 01:15 | triple_rsi | 7 → 3 | ❌ discard | -0.2391 | -0.0403 |
| 2026-03-16 01:15 | triple_rsi | 7 → 5 | ❌ discard | -0.2204 | -0.0216 |
| 2026-03-16 06:58 | triple_rsi | 7 → 5 | ✅ kept | +0.0431 | 0.2749 |
| 2026-03-16 06:58 | triple_rsi | 7 → 3 | ❌ discard | -0.2593 | -0.0275 |
| 2026-03-16 10:37 | triple_rsi | 5 → 7 | ❌ discard | -0.5493 | 0.4171 |
| 2026-03-16 10:37 | triple_rsi | 5 → 3 | ❌ discard | -0.7251 | 0.2413 |
| 2026-04-02 05:10 | connors_rsi2 | 4 → 3 | ✅ kept | +0.3000 | 0.4144 |
| 2026-04-02 05:10 | connors_rsi2 | 4 → 5 | ❌ discard | +0.1992 | 0.3136 |
| 2026-04-02 05:10 | connors_rsi2 | 4 → 2 | ❌ discard | -0.1780 | -0.0636 |
| 2026-04-02 05:41 | connors_rsi2 | 4 → 3 | ✅ kept | +0.3000 | 0.4144 |
| 2026-04-02 05:41 | connors_rsi2 | 4 → 5 | ❌ discard | +0.1992 | 0.3136 |
| 2026-04-02 05:41 | connors_rsi2 | 4 → 2 | ❌ discard | -0.1780 | -0.0636 |
| 2026-04-02 05:46 | connors_rsi2 | 4 → 3 | ✅ kept | +0.3000 | 0.4144 |
| 2026-04-02 05:51 | connors_rsi2 | 4 → 3 | ✅ kept | +0.1216 | 0.2411 |
| 2026-04-02 05:56 | connors_rsi2 | 4 → 3 | ✅ kept | +0.1216 | 0.2411 |
| 2026-04-02 06:02 | connors_rsi2 | 4 → 3 | ✅ kept | +0.1216 | 0.2411 |
| 2026-04-02 06:02 | connors_rsi2 | 4 → 5 | ❌ discard | +0.0846 | 0.2041 |
