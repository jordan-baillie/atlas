---
experiment_id: wave3_rsi_period
wave: 3
strategy: mean_reversion
category: param_drift
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
  - category/param_drift
  - market/sp500
---

# RSI Period

> **Wave:** [[Wave 3]] | **Strategy:** [[Mean Reversion]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

Web research (Triple RSI, Connors, Alvarez) consistently shows RSI(2-5) outperforming RSI(14) for mean reversion signals. Our MR uses RSI(14). Shorter RSI periods may improve entry timing. Combined-mode sweep gives realistic portfolio-level impact unlike unreliable solo sweeps (lesson #30).

## Results

| Metric | Value |
|--------|-------|
| Sharpe | 0.61 |
| Total Trades | 101 |

## Verdict

**FAIL**

RSI(14) is optimal for MR. All shorter periods degrade performance: RSI(3)=-1.54, RSI(5)=-1.56, RSI(7)=-0.99, RSI(10)=-0.73, RSI(14)=+0.61. Shorter RSI generates more "oversold" signals but quality drops catastrophically. RSI(14) should remain the default.

## Learnings

- RSI(14) is clearly optimal for MR on SP500 — confirmed empirically
- Shorter RSI periods increase trade count but destroy quality (more false positives)
- RSI(5) and RSI(3) produce near-random entries (Sharpe < -1.5)
- DO NOT try RSI period optimization again — this is definitive

---

Strategy:: [[Mean Reversion]]
Wave:: [[Wave 3]]