---
experiment_id: wave4_lbr_ibs_sweep
wave: 4
strategy: lower_band_reversion
category: new_strategy
market: sp500
verdict: fail
promoted: false
sharpe: -1.2085
cagr: 0.6
max_drawdown: 4.5
win_rate: 62.41
total_trades: 282
profit_factor: 1.0655
total_pnl: 70.28
date: "2026-03-08"
tags:
  - experiment
  - "strategy/lower-band-reversion"
  - verdict/fail
  - wave/4
  - category/new_strategy
  - market/sp500
---

# LBR IBS Sweep

> **Wave:** [[Wave 4]] | **Strategy:** [[Lower Band Reversion]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

IBS threshold controls signal quality vs quantity. Published used 0.3 for SPY. Individual stocks have different IBS distributions. Testing 0.1-0.6 to find optimal threshold that maximizes risk-adjusted returns.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | -1.21 |
| Sortino | -1.67 |
| CAGR | 0.60% |
| Max Drawdown | 4.50% |
| Win Rate | 62.41% |
| Profit Factor | 1.07 |
| Total Trades | 282 |
| Total PnL | $70.28 |
| Avg Trade | $0.25 |
| Final Equity | $4046.32 |

## Strategy Breakdown

| Strategy | Trades | Total PnL | Win Rate |
|----------|--------|-----------|----------|
| lower_band_reversion | 282 | $70.28 | 62.40% |

## Verdict

**FAIL**

All 1 criteria failed: edge_p_value: 0.7527 >= 0.05 (edge not statistically significant)

## Learnings

- IBS threshold sweep (0.1-0.6): no value produces significant edge (all p>0.25)
- Best Sharpe at IBS=0.4 (-1.21) with highest WR (62.4%) but PF only 1.07
- Higher IBS thresholds increase trade count but don't improve edge quality
- Published IBS=0.3 is not optimal for stocks — but no IBS value works
- CONCLUSION: IBS parameter cannot rescue LBR on individual stocks

---

Strategy:: [[Lower Band Reversion]]
Wave:: [[Wave 4]]