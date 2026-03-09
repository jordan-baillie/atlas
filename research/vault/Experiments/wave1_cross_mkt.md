---
experiment_id: wave1_cross_mkt
wave: 1
strategy: sma200_filter
category: promotion
market: sp500
verdict: promoted
promoted: true
sharpe: 0.868
cagr: 11.66
max_drawdown: 5.33
win_rate: 58.89
total_trades: 270
profit_factor: 1.66
date: "2026-03-01"
tags:
  - experiment
  - "strategy/sma200-filter"
  - verdict/promoted
  - wave/1
  - category/promotion
  - market/sp500
---

# Cross Market

> **Wave:** [[Wave 1]] | **Strategy:** [[SMA-200 Filter]] | **Verdict:** `PROMOTED` | **Promoted:** ✅

## Hypothesis

SMA-200 filter was too aggressive when tested via coord descent (reduced trades to insignificant levels). Testing as a clean A/B toggle across all strategies to measure pure impact without confounding param changes.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | 0.87 |
| CAGR | 11.66% |
| Max Drawdown | 5.33% |
| Win Rate | 58.89% |
| Profit Factor | 1.66 |
| Total Trades | 270 |

## Verdict

**PROMOTED**

*Criteria:* Statistically significant correlation (p < 0.05) AND Sharpe improvement >= 0.03 when used as filter.

SMA-200 filter shows MASSIVE improvement across ALL metrics: Sharpe 0.59 → 0.87 (+47%), CAGR 10.05% → 11.66% (+1.6pp), DD 6.56% → 5.33% (-1.2pp), PF 1.38 → 1.66 (+20%), WR 57.6% → 58.9%. Trades drop from 443 to 270 (still well above 200 minimum). This is the strongest filter result in Wave 1. PROMOTION CANDIDATE.

## Delta vs Baseline

| Metric | Change |
|--------|--------|
| Sharpe | 0.28 |
| Cagr Pct | 1.60 |
| Max Dd Pct | -1.23 |
| Profit Factor | 0.28 |
| Win Rate Pct | 1.33 |

## Learnings

- SMA-200 filter promoted to SP500 active config v2.1
- Applied to all 3 active strategies: mean_reversion, trend_following, opening_gap
- Trades reduced 443→270 but quality improvement overwhelms quantity loss
- Human approved via Telegram promotion request
- SMA-200 filter is a CLEAR WIN on combined portfolio
- Sharpe improvement: +0.28 (0.59 → 0.87) — far exceeds 0.03 threshold
- CAGR improvement: +1.6pp (10.1% → 11.7%) with LESS risk
- DD reduction: -1.2pp (6.6% → 5.3%) — better risk profile
- Trade count 443 → 270 — healthy reduction, quality > quantity
- MECHANISM: filtering out entries below 200-day MA avoids buying into downtrends
- Previous coord descent rejected SMA-200 because it reduces trade count too aggressively
- As a clean A/B toggle, the quality improvement overwhelms the quantity loss
- PROMOTION CANDIDATE: Sharpe +0.28, CAGR +1.6pp, DD -1.2pp all exceed thresholds

---

Strategy:: [[SMA-200 Filter]]
Wave:: [[Wave 1]]