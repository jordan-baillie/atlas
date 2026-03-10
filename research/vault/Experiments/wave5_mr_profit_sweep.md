---
experiment_id: wave5_mr_profit_sweep
wave: 5
strategy: mean_reversion
category: param_drift
market: sp500
verdict: partial
promoted: false
sharpe: 0.6187
max_drawdown: 3.16
total_trades: 97
date: "2026-03-10"
tags:
  - experiment
  - "strategy/mean-reversion"
  - verdict/partial
  - wave/5
  - category/param_drift
  - market/sp500
---

# Mr Profit Sweep

> **Wave:** [[Wave 5]] | **Strategy:** [[Mean Reversion]] | **Verdict:** `PARTIAL` | **Promoted:** ❌

## Hypothesis

Current MR profit target (1.5x ATR) was set pre-SMA200. With the trend filter active, winners may run further (in uptrend). A higher profit target could capture more profit per trade. Sweeping 1.0-3.0x in combined mode to find optimal take-profit level.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | 0.62 |
| Max Drawdown | 3.16% |
| Total Trades | 97 |

## Verdict

**PARTIAL**

profit_target_atr_mult=2.5 marginally best (Sharpe +0.0004 over baseline 2.0). Improvement is noise-level — within Monte Carlo confidence interval. DD improved -0.65pp which is positive but not statistically meaningful. The parameter is not a meaningful lever at current portfolio composition.

## Delta vs Baseline

| Metric | Change |
|--------|--------|
| Sharpe | 0.00 |

## Learnings

- MR profit target has negligible impact on combined portfolio — noise-level
- MR profit target ATR mult has negligible impact on combined portfolio — not a tunable lever
- Sharpe improvement of 0.0004 is indistinguishable from noise
- Current value (2.0) is fine — no change needed

---

Strategy:: [[Mean Reversion]]
Wave:: [[Wave 5]]