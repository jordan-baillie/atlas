---
experiment_id: wave3_vol_sweep
wave: 3
strategy: mean_reversion
category: filter
market: sp500
verdict: fail
promoted: false
sharpe: 0.6076
total_trades: 101
date: "2026-03-06"
tags:
  - experiment
  - "strategy/mean-reversion"
  - verdict/fail
  - wave/3
  - category/filter
  - market/sp500
---

# Volume Sweep

> **Wave:** [[Wave 3]] | **Strategy:** [[Mean Reversion]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

Higher volume threshold for MR entries improves trade quality. Wave 1 proved 1.5x volume on MR solo: Sharpe -0.02→0.38, PF 1.30→1.62. Wave 2 combined test FAILED due to infrastructure bug (nested params). This experiment uses full volume dict sweep to bypass the nested param issue. Expect 1.5x to be optimal in combined mode too.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | 0.61 |
| Total Trades | 101 |

## Verdict

**FAIL**

Volume min_ratio filter has ZERO effect on MR strategy. All 5 variants (0.5, 1.0, 1.25, 1.5, 2.0) produced identical results (Sharpe=0.6076, 101 trades). The filter never triggers — MR signals already occur on high-volume days. Volume filter is dead code for MR.

## Learnings

- Volume min_ratio filter has zero effect on MR — never triggers
- MR signals naturally occur on high-vol days (oversold conditions correlate with volume spikes)
- Remove vol_surge from MR acceptance criteria — it adds nothing

---

Strategy:: [[Mean Reversion]]
Wave:: [[Wave 3]]