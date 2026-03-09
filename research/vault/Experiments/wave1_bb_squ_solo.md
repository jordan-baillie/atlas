---
experiment_id: wave1_bb_squ_solo
wave: 1
strategy: bb_squeeze
category: dormant
market: sp500
verdict: pass
promoted: false
sharpe: -1.68
cagr: -12.27
max_drawdown: 27.35
win_rate: 45.0
total_trades: 322
profit_factor: 0.74
date: "2026-02-28"
tags:
  - experiment
  - "strategy/bb-squeeze"
  - verdict/pass
  - wave/1
  - category/dormant
  - market/sp500
---

# BB Squeeze Solo

> **Wave:** [[Wave 1]] | **Strategy:** [[Bollinger Band Squeeze]] | **Verdict:** `PASS` | **Promoted:** ❌

## Hypothesis

BB Squeeze (Bollinger Band inside Keltner Channel) identifies periods of low volatility that precede explosive moves. When the squeeze fires (BBs expand outside KCs) with positive momentum, it signals a high-probability directional move. This is a volatility regime strategy — fundamentally different from trend/MR/gap signals.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | -1.68 |
| CAGR | -12.27% |
| Max Drawdown | 27.35% |
| Win Rate | 45.00% |
| Profit Factor | 0.74 |
| Total Trades | 322 |

## Verdict

**PASS**

*Criteria:* Solo viability check: strategy generates 10+ trades, WR > 35%, PF > 0.7. Optimization will tune for profitability. Updated: removed positive_pnl requirement (untuned defaults rarely profitable).

All 3 criteria met: min_trades: 322.0000 >= 10; min_win_rate: 45.0300 >= 35.0; min_profit_factor: 0.7419 >= 0.7

## Learnings

- BB Squeeze is viable: 322 trades, 45% WR, PF 0.74 with default params
- Clearly unprofitable untuned (Sharpe -1.68, CAGR -12.3%) but signal generates enough trades
- Passed to optimization phase

---

Strategy:: [[Bollinger Band Squeeze]]
Wave:: [[Wave 1]]