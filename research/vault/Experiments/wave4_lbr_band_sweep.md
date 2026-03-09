---
experiment_id: wave4_lbr_band_sweep
wave: 4
strategy: lower_band_reversion
category: new_strategy
market: sp500
verdict: fail
promoted: false
sharpe: 0.707
cagr: 148.6
max_drawdown: 3.44
win_rate: 61.04
total_trades: 249
profit_factor: 4.3847
total_pnl: 19009.09
date: "2026-03-08"
tags:
  - experiment
  - "strategy/lower-band-reversion"
  - verdict/fail
  - wave/4
  - category/new_strategy
  - market/sp500
---

# LBR Band Sweep

> **Wave:** [[Wave 4]] | **Strategy:** [[Lower Band Reversion]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

Band multiplier controls selectivity: wider band = fewer but deeper dips. Published used 2.5x. Testing 1.5-4.0x to find optimal trade-off between trade frequency and signal quality on individual stocks.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | 0.71 |
| Sortino | 81.12 |
| CAGR | 148.60% |
| Max Drawdown | 3.44% |
| Win Rate | 61.04% |
| Profit Factor | 4.38 |
| Total Trades | 249 |
| Total PnL | $19009.09 |
| Avg Trade | $76.34 |
| Final Equity | $23139.48 |

## Strategy Breakdown

| Strategy | Trades | Total PnL | Win Rate |
|----------|--------|-----------|----------|
| lower_band_reversion | 249 | $19009.09 | 61.00% |

## Verdict

**FAIL**

All 2 criteria failed: edge_p_value: 0.3204 >= 0.05 (edge not statistically significant); mc_fragile: True (p95 MC drawdown > 2× actual — trade sequence dependent)

## Learnings

- Band multiplier sweep (1.5x-4.0x): band_mult=3.5 produces anomalously high PF (4.38) with Sharpe 0.71
- Likely overfitting: $19K PnL on 249 trades smells like a few lucky outsized wins
- Monte Carlo test confirms fragility: p95 MC drawdown > 2× actual (trade-sequence dependent)
- All other band values produce negative Sharpe — no robust parameter exists
- Edge not significant at any band level (p=0.01-0.55 range, but MC fragile flags invalidate the low p-values)
- CONCLUSION: LBR band parameter cannot rescue the strategy on individual stocks

---

Strategy:: [[Lower Band Reversion]]
Wave:: [[Wave 4]]