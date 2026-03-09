---
experiment_id: wave4_lbr_solo_relaxed
wave: 4
strategy: lower_band_reversion
category: new_strategy
market: sp500
verdict: partial
promoted: false
sharpe: -1.851
cagr: -1.53
max_drawdown: 5.02
win_rate: 59.64
total_trades: 280
profit_factor: 0.8987
total_pnl: -112.32
date: "2026-03-08"
tags:
  - experiment
  - "strategy/lower-band-reversion"
  - verdict/partial
  - wave/4
  - category/new_strategy
  - market/sp500
---

# LBR Solo Relaxed

> **Wave:** [[Wave 4]] | **Strategy:** [[Lower Band Reversion]] | **Verdict:** `PARTIAL` | **Promoted:** ❌

## Hypothesis

Relaxing IBS threshold from 0.3 to 0.5 generates more trades on individual stocks (which have wider IBS distributions than SPY). Tests if the band signal alone carries enough edge without strict IBS filtering.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | -1.85 |
| Sortino | -2.53 |
| CAGR | -1.53% |
| Max Drawdown | 5.02% |
| Win Rate | 59.64% |
| Profit Factor | 0.90 |
| Total Trades | 280 |
| Total PnL | $-112.32 |
| Avg Trade | $-0.40 |
| Final Equity | $3883.07 |

## Strategy Breakdown

| Strategy | Trades | Total PnL | Win Rate |
|----------|--------|-----------|----------|
| lower_band_reversion | 280 | $-112.32 | 59.60% |

## Verdict

**PARTIAL**

2 pass, 1 fail: edge_p_value: 0.4853 >= 0.05 (edge not statistically significant)

---

Strategy:: [[Lower Band Reversion]]
Wave:: [[Wave 4]]