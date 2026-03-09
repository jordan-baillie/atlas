---
experiment_id: wave4_mr_strength_exit
wave: 4
strategy: mean_reversion
category: param_drift
market: sp500
verdict: fail
promoted: false
sharpe: -2.0995
cagr: 0.37
max_drawdown: 1.39
win_rate: 51.47
total_trades: 68
profit_factor: 1.0333
total_pnl: 10.26
date: "2026-03-08"
tags:
  - experiment
  - "strategy/mean-reversion"
  - verdict/fail
  - wave/4
  - category/param_drift
  - market/sp500
---

# MR Strength Exit

> **Wave:** [[Wave 4]] | **Strategy:** [[Mean Reversion]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

The LBR published exit rule (sell when close > yesterday high) captures the first sign of strength recovery. Testing this on existing MR strategy as an alternative to the current profit-target + mean-reversion exit. Expected: faster exits, higher win rate, possibly lower avg profit per trade.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | -2.10 |
| Sortino | -2.70 |
| CAGR | 0.37% |
| Max Drawdown | 1.39% |
| Win Rate | 51.47% |
| Profit Factor | 1.03 |
| Total Trades | 68 |
| Total PnL | $10.26 |
| Avg Trade | $0.15 |
| Final Equity | $4028.24 |

## Strategy Breakdown

| Strategy | Trades | Total PnL | Win Rate |
|----------|--------|-----------|----------|
| mean_reversion | 68 | $10.26 | 51.50% |

## Verdict

**FAIL**

All 2 criteria failed: min_sharpe_vs_baseline: metric not found; edge_p_value: 0.8794 >= 0.05 (edge not statistically significant)

## Learnings

- LBR-style strength exit (sell when close > yesterday's high) applied to MR: Sharpe -2.10, 68 trades
- Dramatic trade count reduction (101→68) — exit triggers too early, cutting profitable trades short
- PF barely above 1.0 (1.03) — essentially random after applying this exit rule
- Edge not significant (p=0.88) — the worst p-value of any MR experiment
- CONCLUSION: Simple 'strength exit' is inferior to max_hold_days for MR. Price-based exits add noise, not alpha.

---

Strategy:: [[Mean Reversion]]
Wave:: [[Wave 4]]