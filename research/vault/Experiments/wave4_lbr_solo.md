---
experiment_id: wave4_lbr_solo
wave: 4
strategy: lower_band_reversion
category: new_strategy
market: sp500
verdict: partial
promoted: false
sharpe: -2.0834
cagr: -2.6
max_drawdown: 6.77
win_rate: 58.15
total_trades: 270
profit_factor: 0.846
total_pnl: -174.35
date: "2026-03-07"
tags:
  - experiment
  - "strategy/lower-band-reversion"
  - verdict/partial
  - wave/4
  - category/new_strategy
  - market/sp500
---

# LBR Solo

> **Wave:** [[Wave 4]] | **Strategy:** [[Lower Band Reversion]] | **Verdict:** `PARTIAL` | **Promoted:** ❌

## Hypothesis

The Quantitativo IBS lower-band strategy (Sharpe 2.11 on SPY) can generate profitable signals when adapted to individual SP500 stocks. Published params: range_lookback=25, high_lookback=10, band_mult=2.5, IBS<0.3, exit on close>prev_high.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | -2.08 |
| Sortino | -2.78 |
| CAGR | -2.60% |
| Max Drawdown | 6.77% |
| Win Rate | 58.15% |
| Profit Factor | 0.85 |
| Total Trades | 270 |
| Total PnL | $-174.35 |
| Avg Trade | $-0.65 |
| Final Equity | $3801.69 |

## Strategy Breakdown

| Strategy | Trades | Total PnL | Win Rate |
|----------|--------|-----------|----------|
| lower_band_reversion | 270 | $-174.35 | 58.10% |

## Verdict

**PARTIAL**

2 pass, 1 fail: edge_p_value: 0.2529 >= 0.05 (edge not statistically significant)

## Learnings

- LBR with published SPY params on individual SP500 stocks: Sharpe -2.08, 270 trades, 58% WR, PF 0.85
- Win rate is decent (58%) but average loss exceeds average win — classic ETF-to-stock adaptation issue
- Edge not statistically significant (p=0.25)
- Published Sharpe 2.11 on SPY → -2.08 on individual stocks: dramatic degradation confirms ETF strategies don't transfer

---

Strategy:: [[Lower Band Reversion]]
Wave:: [[Wave 4]]