---
experiment_id: wave1_short__opt
wave: 1
strategy: short_term_mr
category: dormant
market: sp500
verdict: pass
promoted: false
sharpe: 0.2711
cagr: 7.65
max_drawdown: 10.06
win_rate: 63.13
total_trades: 697
profit_factor: 1.1739
total_pnl: 623.8
date: "2026-02-27"
tags:
  - experiment
  - "strategy/short-term-mr"
  - verdict/pass
  - wave/1
  - category/dormant
  - market/sp500
---

# Short Opt

> **Wave:** [[Wave 1]] | **Strategy:** [[Short Term MR]] | **Verdict:** `PASS` | **Promoted:** ❌

## Hypothesis

Coordinate descent on Short-Term Mean Reversion params will improve solo performance significantly.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | 0.27 |
| Sortino | 0.41 |
| CAGR | 7.65% |
| Max Drawdown | 10.06% |
| Win Rate | 63.13% |
| Profit Factor | 1.17 |
| Total Trades | 697 |
| Total PnL | $623.80 |
| Avg Trade | $0.89 |
| Final Equity | $4608.63 |

## Strategy Breakdown

| Strategy | Trades | Total PnL | Win Rate |
|----------|--------|-----------|----------|
| short_term_mr | 697 | $623.80 | 63.10% |

## Verdict

**PASS**

*Criteria:* Optimization should lift Sharpe by 0.1+ vs untuned solo, maintain 15+ trades.

All 3 criteria met: sharpe_improvement_vs_solo: 0.7182 >= 0.1; min_trades: 697.0000 >= 15; min_profit_factor: 1.1739 >= 1.1

## Learnings

- Optimization improved Sharpe from -0.45 to +0.27 — significant improvement
- Post-optimization: 697 trades, 63% WR, PF 1.17, CAGR 7.6%
- Trade count reduced 946→697 (26%) through optimization — still very high
- PF improvement 0.96→1.17 shows optimizer found genuine edge in parameter space

---

Strategy:: [[Short Term MR]]
Wave:: [[Wave 1]]