---
experiment_id: wave3_ibs_sweep
wave: 3
strategy: mean_reversion
category: filter
market: sp500
verdict: fail
promoted: false
sharpe: 0.6101
total_trades: 100
date: "2026-03-06"
tags:
  - experiment
  - "strategy/mean-reversion"
  - verdict/fail
  - wave/3
  - category/filter
  - market/sp500
---

# IBS Sweep

> **Wave:** [[Wave 3]] | **Strategy:** [[Mean Reversion]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

Requiring low IBS (close near day's low) for MR entries improves signal quality. Alvarez research shows IBS < 25 gives 58% avg gain improvement on RSI(2) strategy. Our MR has ibs_max=1.0 (disabled). Testing restrictive thresholds should filter out weak MR signals.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | 0.61 |
| Total Trades | 100 |

## Verdict

**FAIL**

IBS entry filter adds near-zero value to MR in combined mode. Best variant (IBS=0.2) improved Sharpe by only +0.0025 (threshold: 0.03). Edge p=0.32 — not significant. Volume filter and RSI already capture the same oversold conditions. IBS is redundant.

## Learnings

- IBS filter is redundant with RSI+vol in combined mode — adds no alpha
- IBS < 0.15 crashes performance (kills 10% of trades, all good ones)
- Baseline is stable: Sharpe=0.608, 101 trades, CAGR=27.8%

---

Strategy:: [[Mean Reversion]]
Wave:: [[Wave 3]]