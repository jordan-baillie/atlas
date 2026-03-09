---
experiment_id: wave1_moment_solo
wave: 1
strategy: momentum_breakout
category: dormant
market: sp500
verdict: pass
promoted: false
sharpe: -0.9931
cagr: -2.55
max_drawdown: 11.21
win_rate: 48.54
total_trades: 342
profit_factor: 0.9577
total_pnl: -72.15
date: "2026-02-27"
tags:
  - experiment
  - "strategy/momentum-breakout"
  - verdict/pass
  - wave/1
  - category/dormant
  - market/sp500
---

# Momentum Breakout Solo

> **Wave:** [[Wave 1]] | **Strategy:** [[Momentum Breakout]] | **Verdict:** `PASS` | **Promoted:** ❌

## Hypothesis

Momentum breakout captures trend initiation events that the existing trend_following strategy misses. TF waits for MA crossover (lagging); breakout enters at the point of N-day high breach (leading). Expects moderate trade count (30-80), higher avg win, lower win rate than MR.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | -0.99 |
| Sortino | -1.35 |
| CAGR | -2.55% |
| Max Drawdown | 11.21% |
| Win Rate | 48.54% |
| Profit Factor | 0.96 |
| Total Trades | 342 |
| Total PnL | $-72.15 |
| Avg Trade | $-0.21 |
| Final Equity | $3806.04 |

## Strategy Breakdown

| Strategy | Trades | Total PnL | Win Rate |
|----------|--------|-----------|----------|
| momentum_breakout | 342 | $-72.15 | 48.50% |

## Verdict

**PASS**

*Criteria:* Solo viability check: strategy generates 10+ trades, WR > 35%, PF > 0.7. Optimization will tune for profitability. Updated: removed positive_pnl requirement (untuned defaults rarely profitable).

All 3 criteria met: min_trades: 342.0000 >= 10; min_win_rate: 48.5400 >= 35.0; min_profit_factor: 0.9577 >= 0.7

## Learnings

- Momentum breakout generates 342 trades with 48.5% WR — sufficient signal activity for viability
- Untuned default params produce negative Sharpe (-0.99) but meet relaxed solo criteria (trades>10, WR>35%, PF>0.7)
- Strategy is viable for optimization phase — signal exists even if untuned defaults are unprofitable

---

Strategy:: [[Momentum Breakout]]
Wave:: [[Wave 1]]