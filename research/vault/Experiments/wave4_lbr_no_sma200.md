---
experiment_id: wave4_lbr_no_sma200
wave: 4
strategy: lower_band_reversion
category: new_strategy
market: sp500
verdict: pass
promoted: false
date: "2026-03-08"
tags:
  - experiment
  - "strategy/lower-band-reversion"
  - verdict/pass
  - wave/4
  - category/new_strategy
  - market/sp500
---

# LBR No SMA-200

> **Wave:** [[Wave 4]] | **Strategy:** [[Lower Band Reversion]] | **Verdict:** `PASS` | **Promoted:** ❌

## Hypothesis

SMA-200 filter was a clear win for existing MR (+0.28 Sharpe). But LBR specifically targets extreme dips — which often occur in downtrends. Testing if removing SMA-200 captures more deep-dip opportunities that still revert quickly. Published strategy on SPY used SMA-300 as improvement.

## Results

*No metrics recorded for this experiment.*

## Verdict

**PASS**

All 0 criteria met: 

## Delta vs Baseline

| Metric | Change |
|--------|--------|
| Cagr Pct | -2.20 |
| Sharpe | -0.64 |
| Sortino | -0.73 |
| Max Drawdown Pct | 1.86 |
| Win Rate Pct | -0.86 |
| Profit Factor | -0.15 |
| Total Trades | -13.00 |

## Learnings

- COUNTERINTUITIVE: Removing SMA-200 filter IMPROVES LBR — Sharpe -2.08→-1.44, PnL -$174→-$5
- With SMA-200 OFF: 283 trades, 59% WR, PF 1.00 (near breakeven vs clearly negative with filter)
- SMA-200 filter hurts LBR because LBR targets extreme dips — which often occur below the 200-day MA
- This is the OPPOSITE of what SMA-200 does for MR/TF/OG (where it helps by +0.28 Sharpe)
- KEY INSIGHT: Filters are strategy-dependent. SMA-200 is not universally beneficial.

---

Strategy:: [[Lower Band Reversion]]
Wave:: [[Wave 4]]