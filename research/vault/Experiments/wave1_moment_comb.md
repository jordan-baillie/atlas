---
experiment_id: wave1_moment_comb
wave: 1
strategy: momentum_breakout
category: dormant
market: sp500
verdict: fail
promoted: false
sharpe: -0.16
cagr: 1.9
max_drawdown: 16.49
date: "2026-02-27"
tags:
  - experiment
  - "strategy/momentum-breakout"
  - verdict/fail
  - wave/1
  - category/dormant
  - market/sp500
---

# Momentum Breakout Combined

> **Wave:** [[Wave 1]] | **Strategy:** [[Momentum Breakout]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

Adding optimized Momentum Breakout to the active strategy set (MR+TF+OG) improves the portfolio.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | -0.16 |
| CAGR | 1.90% |
| Max Drawdown | 16.49% |

**Combined vs Baseline:**

| Metric | Baseline | Combined | Delta |
|--------|----------|----------|-------|
| Sharpe | 0.59 | -0.16 | -0.75 |
| CAGR | 10.05% | 1.90% | -8.16% |
| Max DD | 6.56% | 16.49% | — |

## Verdict

**FAIL**

*Criteria:* Combined portfolio must maintain Sharpe >= 1.04, DD <= 6.4%, strategy contributes 10+ trades. OG currently drags portfolio solo but helps via diversification — breakout may do similar or better.

2 pass, 2 fail: min_combined_sharpe: -0.1623 < 1.04; max_combined_dd: 16.4879 > 6.39 — Adding momentum_breakout degrades the portfolio (Sharpe -0.75, CAGR -8.2pp). REJECT for portfolio inclusion.

## Delta vs Baseline

| Metric | Change |
|--------|--------|
| Sharpe | -0.75 |
| Cagr Pct | -8.16 |
| Max Drawdown Pct | 9.92 |

## Learnings

- Momentum breakout solo is modestly profitable after optimization (Sharpe 0.30, CAGR 8.0%)
- But adding it to the active portfolio (MR+TF+OG) HURTS performance dramatically
- Combined Sharpe drops from 0.59 to -0.16, DD increases from 6.6% to 16.5%
- The 460 breakout trades compete with MR/TF signals for the 10 max positions
- Breakout strategy may work better with a separate position allocation

---

Strategy:: [[Momentum Breakout]]
Wave:: [[Wave 1]]