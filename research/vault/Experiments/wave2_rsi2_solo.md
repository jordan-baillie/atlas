---
experiment_id: wave2_rsi2_solo
wave: 2
strategy: connors_rsi2
category: new_strategy
market: sp500
verdict: fail
promoted: false
sharpe: -2.63
cagr: -2.06
max_drawdown: 6.86
win_rate: 57.03
total_trades: 249
profit_factor: 0.78
date: "2026-03-04"
tags:
  - experiment
  - "strategy/connors-rsi2"
  - verdict/fail
  - wave/2
  - category/new_strategy
  - market/sp500
---

# ConnorsRSI2 Solo

> **Wave:** [[Wave 2]] | **Strategy:** [[ConnorsRSI2]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

Connors RSI(2) with SMA-200 filter has proven 74%+ win rate across 34 years of backtesting (Connors & Alvarez 2008, confirmed by reddit/algotrading 2024). Entry: RSI(2)<10 + close>SMA(200). Exit: close>SMA(5). This is DISTINCT from our existing MR (RSI-14/z-score) and should generate uncorrelated signals on different stocks at different times.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | -2.63 |
| CAGR | -2.06% |
| Max Drawdown | 6.86% |
| Win Rate | 57.03% |
| Profit Factor | 0.78 |
| Total Trades | 249 |

## Verdict

**FAIL**

*Criteria:* Min 100 trades, Sharpe > 0, PF > 1.1, win rate > 55%. RSI(2) strategies typically have high win rates but need enough trades to be statistically meaningful.

2 pass, 3 fail: min_sharpe: -2.6257 < 0.0; min_profit_factor: 0.7837 < 1.1; edge_p_value: 0.2659 >= 0.05 (edge not statistically significant)

## Learnings

- ConnorsRSI2 generates 249 trades with 57% WR — signal quality decent
- But PF=0.78: losses larger than wins. ATR(3.0x) stop gives wide risk while SMA(5) exit captures small gains
- Edge not statistically significant (p=0.27)
- ALSO HAD CODE BUG: calc_position_size returns dict, code compared dict <= 0
- Fixed: pos_result["shares"] extraction. Does not change backtest outcome (only affected expensive stocks)
- HYPOTHESIS REJECTED: RSI(2) solo is unprofitable with current params on SP500 at $4K equity
- POSSIBLE RETRY: With tighter stop (1.5-2x ATR) and higher equity, risk-reward may improve

---

Strategy:: [[ConnorsRSI2]]
Wave:: [[Wave 2]]