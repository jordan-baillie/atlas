---
experiment_id: wave1_bb_squ_opt
wave: 1
strategy: bb_squeeze
category: dormant
market: sp500
verdict: partial
promoted: false
sharpe: -0.38
cagr: -0.37
max_drawdown: 16.55
win_rate: 48.3
total_trades: 348
profit_factor: 1.04
date: "2026-02-28"
tags:
  - experiment
  - "strategy/bb-squeeze"
  - verdict/partial
  - wave/1
  - category/dormant
  - market/sp500
---

# BB Squeeze Optimization

> **Wave:** [[Wave 1]] | **Strategy:** [[Bollinger Band Squeeze]] | **Verdict:** `PARTIAL` | **Promoted:** ❌

## Hypothesis

Coordinate descent on BB Squeeze (Volatility Breakout) params will improve solo performance significantly.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | -0.38 |
| CAGR | -0.37% |
| Max Drawdown | 16.55% |
| Win Rate | 48.30% |
| Profit Factor | 1.04 |
| Total Trades | 348 |

## Verdict

**PARTIAL**

*Criteria:* Optimization should lift Sharpe by 0.1+ vs untuned solo, maintain 15+ trades.

2 pass, 1 fail: min_profit_factor: 1.0364 < 1.1

## Delta vs Baseline

| Metric | Change |
|--------|--------|
| Sharpe | 1.30 |
| Profit Factor | 0.30 |

## Learnings

- BB Squeeze improved dramatically: Sharpe -1.68 → -0.38, PF 0.74 → 1.04
- Best params: bb_period=25, bb_std=1.5 (both changed from defaults)
- But PF 1.04 still below 1.1 threshold, Sharpe still negative
- Near breakeven after optimization is not good enough for portfolio addition
- BB Squeeze on SP500 with current implementation is likely not viable
- PATTERN: All 3 dormant strategies tried so far are individually marginal after optimization

---

Strategy:: [[Bollinger Band Squeeze]]
Wave:: [[Wave 1]]