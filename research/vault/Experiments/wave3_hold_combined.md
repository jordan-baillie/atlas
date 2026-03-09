---
experiment_id: wave3_hold_combined
wave: 3
strategy: mean_reversion
category: param_drift
market: sp500
verdict: partial
promoted: false
sharpe: 0.6424
cagr: 30.86
max_drawdown: 3.78
total_trades: 109
date: "2026-03-06"
tags:
  - experiment
  - "strategy/mean-reversion"
  - verdict/partial
  - wave/3
  - category/param_drift
  - market/sp500
---

# Hold Combined

> **Wave:** [[Wave 3]] | **Strategy:** [[Mean Reversion]] | **Verdict:** `PARTIAL` | **Promoted:** ❌

## Hypothesis

Wave 2 tested max_hold_days in SOLO mode (all negative Sharpe due to fee drag at $4K). Relative ranking showed 10 > 15 > 7 > 5 > 3. Combined-mode sweep gives realistic absolute Sharpe. Short holds (3-5d) may reduce time risk; longer holds (10-15d) capture more reversion.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | 0.64 |
| CAGR | 30.86% |
| Max Drawdown | 3.78% |
| Total Trades | 109 |

## Verdict

**PARTIAL**

max_hold=5 shows promising improvement: Sharpe +0.035, CAGR +3.1pp, DD -0.2pp, PF 4.55 vs 3.64. Consistent pattern: 5 > 7 > 12 > 3 > 10 > 15. Short holds work better for MR. BUT edge p=0.30 — not statistically significant. Needs OOS validation before promotion. max_hold=15 is catastrophic (Sharpe=-1.74) — never use.

## Learnings

- max_hold=5 beats max_hold=10: Sharpe +0.035, CAGR +3.1pp, PF 4.55 vs 3.64
- MR trades resolve quickly — 5-day hold captures most reversion, longer holds add noise
- max_hold=15 is catastrophic — trades that havent reverted by day 10 are losers
- Promising but needs OOS confirmation before promotion (p=0.30)

---

Strategy:: [[Mean Reversion]]
Wave:: [[Wave 3]]