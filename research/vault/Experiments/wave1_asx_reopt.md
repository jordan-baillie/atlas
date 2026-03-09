---
experiment_id: wave1_asx_reopt
wave: 1
strategy: portfolio_filter
category: promotion
market: asx
verdict: promoted
promoted: true
date: "2026-02-28"
tags:
  - experiment
  - "strategy/portfolio-filter"
  - verdict/promoted
  - wave/1
  - category/promotion
  - market/asx
---

# ASX Re-optimization

> **Wave:** [[Wave 1]] | **Strategy:** [[Portfolio Filter]] | **Verdict:** `PROMOTED` | **Promoted:** ✅

## Hypothesis

The SMA-200 filter, IBS confirmation, and configurable RSI period were added during SP500 optimization but never tested on ASX. These features may improve ASX performance, particularly: SMA-200 filtering out downtrend entries, IBS improving mean reversion entry quality, and RSI period tuning finding a better signal frequency for the smaller ASX universe.

## Results

*No metrics recorded for this experiment.*

## Verdict

**PROMOTED**

*Criteria:* Sharpe improvement >= 0.05 OR DD reduction >= 1pp without Sharpe degradation. Min 250 trades.

1 pass, 2 fail: sharpe_improvement: metric not found; max_dd_increase: metric not found

## Learnings

- Promoted to /root/atlas/config/versions/asx_v9.3.json

---

Strategy:: [[Portfolio Filter]]
Wave:: [[Wave 1]]