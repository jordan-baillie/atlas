---
experiment_id: wave2_chandelier_tf
wave: 2
strategy: trend_following
category: param_drift
market: sp500
verdict: fail
promoted: false
total_trades: 77
date: "2026-03-04"
tags:
  - experiment
  - "strategy/trend-following"
  - verdict/fail
  - wave/2
  - category/param_drift
  - market/sp500
---

# Chandelier TF

> **Wave:** [[Wave 2]] | **Strategy:** [[Trend Following]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

Chandelier exit (highest-high minus N×ATR) adapts to volatility better than fixed trailing stop ATR multiplier. It locks in profits during strong trends while giving room during pullbacks. Testing different ATR multipliers (2.5-4.0) against current trailing_stop_atr_mult=2.5.

## Results

| Metric | Value |
|--------|-------|
| Total Trades | 77 |

## Verdict

**FAIL**

*Criteria:* Sharpe improvement >= 0.05 vs current TF exit. Chandelier should capture more trend profit with adaptive stops.

All 2 criteria failed: sharpe_improvement: metric not found; edge_p_value: 0.6224 >= 0.05 (edge not statistically significant)

## Learnings

- Tighter stops (2.0x ATR) slightly better than wider (3.5-4.0x)
- Difference is marginal: -1.09 vs -1.17 Sharpe (all negative in solo mode)
- Current default (2.5x) is near-optimal based on this sweep
- 77 trades across all variants — consistent trade count means stop width only affects P&L per trade
- SAME PATTERN: Solo strategy on $4K equity shows negative Sharpe due to fee drag

---

Strategy:: [[Trend Following]]
Wave:: [[Wave 2]]