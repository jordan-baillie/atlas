---
experiment_id: wave5_og_gap_sweep
wave: 5
strategy: opening_gap
category: param_drift
market: sp500
verdict: partial
promoted: false
sharpe: 0.6183
total_trades: 101
date: "2026-03-10"
tags:
  - experiment
  - "strategy/opening-gap"
  - verdict/partial
  - wave/5
  - category/param_drift
  - market/sp500
---

# Og Gap Sweep

> **Wave:** [[Wave 5]] | **Strategy:** [[Opening Gap]] | **Verdict:** `PARTIAL` | **Promoted:** ❌

## Hypothesis

Opening gap only generates 9 trades over the backtest period with current thresholds. The -1.5% gap threshold may be too strict with SMA-200 filtering. Relaxing gap threshold to -1.0% or -0.5% could increase trade count while maintaining quality.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | 0.62 |
| Total Trades | 101 |

## Verdict

**PARTIAL**

All gap_threshold values produced identical Sharpe (0.6183). This confirms Wave 2 finding: OG generates very few trades (9 total in backtest). The gap threshold parameter has zero impact because OG barely fires. OG may need the reopt changes (gap=-0.025, sma200=False) to become more active.

## Delta vs Baseline

| Metric | Change |
|--------|--------|
| Sharpe | 0.00 |

## Learnings

- OG generates <10 trades — gap_threshold parameter is meaningless
- OG needs reopt params to become active
- OG gap_threshold sweep is meaningless when OG generates <10 trades total
- This test used pre-reopt params — the reopt found different params that may produce more trades
- OG needs fundamental param changes (from reopt) before fine-tuning is useful

---

Strategy:: [[Opening Gap]]
Wave:: [[Wave 5]]