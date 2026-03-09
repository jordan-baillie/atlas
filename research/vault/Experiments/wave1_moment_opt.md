---
experiment_id: wave1_moment_opt
wave: 1
strategy: momentum_breakout
category: dormant
market: sp500
verdict: pass
promoted: false
sharpe: 0.2995
cagr: 8.05
max_drawdown: 12.67
win_rate: 52.39
total_trades: 460
profit_factor: 1.2945
total_pnl: 997.01
date: "2026-02-27"
tags:
  - experiment
  - "strategy/momentum-breakout"
  - verdict/pass
  - wave/1
  - category/dormant
  - market/sp500
---

# Momentum Breakout Optimization

> **Wave:** [[Wave 1]] | **Strategy:** [[Momentum Breakout]] | **Verdict:** `PASS` | **Promoted:** ❌

## Hypothesis

Coordinate descent on Momentum Breakout params will improve solo performance significantly.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | 0.30 |
| Sortino | 0.37 |
| CAGR | 8.05% |
| Max Drawdown | 12.67% |
| Win Rate | 52.39% |
| Profit Factor | 1.29 |
| Total Trades | 460 |
| Total PnL | $997.01 |
| Avg Trade | $2.17 |
| Final Equity | $4641.55 |

## Strategy Breakdown

| Strategy | Trades | Total PnL | Win Rate |
|----------|--------|-----------|----------|
| momentum_breakout | 460 | $997.01 | 52.40% |

## Verdict

**PASS**

*Criteria:* Optimization should lift Sharpe by 0.1+ vs untuned solo, maintain 15+ trades.

All 3 criteria met: sharpe_improvement_vs_solo: 1.2926 >= 0.1; min_trades: 460.0000 >= 15; min_profit_factor: 1.2945 >= 1.1

## Delta vs Baseline

| Metric | Change |
|--------|--------|
| Sharpe Improvement | 1.29 |

## Learnings

- All 5 params changed during optimization
- Shorter breakout lookback (10 vs 20) works better
- Tighter stops (2.0x vs 3.5x ATR) dramatically improve results
- Longer trend filter (150 vs 50 SMA) eliminates false breakouts

---

Strategy:: [[Momentum Breakout]]
Wave:: [[Wave 1]]