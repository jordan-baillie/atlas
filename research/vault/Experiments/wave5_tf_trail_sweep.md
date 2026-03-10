---
experiment_id: wave5_tf_trail_sweep
wave: 5
strategy: trend_following
category: param_drift
market: sp500
verdict: partial
promoted: false
sharpe: 0.62
max_drawdown: 3.36
total_trades: 99
date: "2026-03-10"
tags:
  - experiment
  - "strategy/trend-following"
  - verdict/partial
  - wave/5
  - category/param_drift
  - market/sp500
---

# Tf Trail Sweep

> **Wave:** [[Wave 5]] | **Strategy:** [[Trend Following]] | **Verdict:** `PARTIAL` | **Promoted:** ❌

## Hypothesis

TF trailing stop (currently 2.5x ATR) determines how much room trends have to breathe. With SMA-200 filtering out downtrends, surviving trades are higher quality and may benefit from tighter or wider stops. Sweep 1.5-3.5x in combined mode.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | 0.62 |
| Max Drawdown | 3.36% |
| Total Trades | 99 |

## Verdict

**PARTIAL**

trailing_stop_atr_mult=3.5 slightly better than current 3.0 (Sharpe +0.0017). Very marginal — within MC noise. Wider stops (4.0, 4.5) degrade Sharpe. Confirms Wave 4 finding that tighter stops work slightly better, but 3.0→3.5 is negligible.

## Delta vs Baseline

| Metric | Change |
|--------|--------|
| Sharpe | 0.00 |

## Learnings

- TF trailing stop 3.5 marginally better than 3.0, wider stops worse — confirms Wave 4
- TF trailing stop ATR mult 3.5 marginally better than 3.0, but not statistically significant
- Wider stops (4.0+) clearly worse — confirmed pattern from Wave 4 chandelier exit test
- Current value (3.0) is adequate — no change worth the overfitting risk

---

Strategy:: [[Trend Following]]
Wave:: [[Wave 5]]