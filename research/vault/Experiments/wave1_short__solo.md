---
experiment_id: wave1_short__solo
wave: 1
strategy: short_term_mr
category: dormant
market: sp500
verdict: pass
promoted: false
sharpe: -0.4471
cagr: -1.67
max_drawdown: 18.22
win_rate: 58.56
total_trades: 946
profit_factor: 0.9609
total_pnl: -186.87
date: "2026-02-27"
tags:
  - experiment
  - "strategy/short-term-mr"
  - verdict/pass
  - wave/1
  - category/dormant
  - market/sp500
---

# Short Solo

> **Wave:** [[Wave 1]] | **Strategy:** [[Short Term MR]] | **Verdict:** `PASS` | **Promoted:** ❌

## Hypothesis

Short-term MR (RSI(2)/IBS) captures rapid 1-5 day reversals that the existing mean_reversion (RSI(14)/z-score) strategy misses. Different timeframe = different signals = diversification benefit. Connors research shows RSI(2)<10 has 70-75% win rate on SP500. Key risk: signal overlap with existing MR — need <30% overlap for value.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | -0.45 |
| Sortino | -0.63 |
| CAGR | -1.67% |
| Max Drawdown | 18.22% |
| Win Rate | 58.56% |
| Profit Factor | 0.96 |
| Total Trades | 946 |
| Total PnL | $-186.87 |
| Avg Trade | $-0.20 |
| Final Equity | $3872.37 |

## Strategy Breakdown

| Strategy | Trades | Total PnL | Win Rate |
|----------|--------|-----------|----------|
| short_term_mr | 946 | $-186.87 | 58.60% |

## Verdict

**PASS**

*Criteria:* Solo viability check: strategy generates 10+ trades, WR > 35%, PF > 0.7. Optimization will tune for profitability. Updated: removed positive_pnl requirement (untuned defaults rarely profitable).

All 3 criteria met: min_trades: 946.0000 >= 10; min_win_rate: 58.5600 >= 35.0; min_profit_factor: 0.9609 >= 0.7

## Learnings

- Short-term MR generates 946 trades — highest trade count of any dormant strategy tested
- 58.6% WR suggests signal quality, but PF 0.96 means losses slightly exceed wins
- Massive trade count (946) will create severe slot contention in combined portfolio at max_positions=10
- Viable for optimization — high trade count gives optimizer plenty of data to tune parameters

---

Strategy:: [[Short Term MR]]
Wave:: [[Wave 1]]