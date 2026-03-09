---
wave: 4
status: planned
theme: "New strategy: Lower Band Reversion — IBS-based mean reversion with published 2.11 Sharpe edge"
start_date: "2026-03-06"
experiment_count: 10
tags:
  - wave
  - wave/4
---

# Wave 4: New strategy: Lower Band Reversion — IBS-based mean reversion with published 2.11 Sharpe edge

> **Status:** `PLANNED` | **Experiments:** 10 | **Started:** 2026-03-06

## Theme Rationale

Central hypothesis: The Quantitativo/Pagonidis IBS lower-band strategy (published Sharpe 2.11 on SPY, 69% WR, PF 1.98) can be adapted for individual SP500 stocks to add uncorrelated returns to the active portfolio.

Why this theme:
1. PUBLISHED EDGE: 25 years of backtested results on SPY/QQQ with strong risk-adjusted returns
2. DIFFERENT SIGNAL: Uses price band + IBS (not RSI/z-score like existing MR) — expected to be uncorrelated
3. INFREQUENT TRADES: High-conviction entries won't flood position slots (the #1 failure mode of prior strategies)
4. MAX_POS NOW 15: Prior strategies failed combined tests at max_pos=10, now 15 — more headroom
5. QUICK EXITS: 'Close > prev high' exit rule = fast capital turnover, low time-in-market

Secondary track: OOS validation of max_hold=5 for existing MR (wave 3 finding, Sharpe +0.035, CAGR +3.1pp).

Together: if LBR passes combined test AND max_hold=5 confirms OOS, we could promote both in a single config update for estimated +15-25% Sharpe improvement.

## Experiments

| Experiment | Verdict | Strategy | Sharpe | Promoted |
|------------|---------|----------|--------|----------|
| [[wave4_lbr_solo]] | `partial` | Lower Band Reversion | -2.08 |  |
| [[wave4_lbr_solo_relaxed]] | `partial` | Lower Band Reversion | -1.85 |  |
| [[wave4_lbr_opt]] | — | — | — | |
| [[wave4_lbr_combined]] | — | — | — | |
| [[wave4_lbr_oos]] | — | — | — | |
| [[wave4_mr_hold5_oos]] | `fail` | Mean Reversion | N/A |  |
| [[wave4_mr_strength_exit]] | `fail` | Mean Reversion | -2.10 |  |
| [[wave4_lbr_band_sweep]] | `fail` | Lower Band Reversion | 0.71 |  |
| [[wave4_lbr_ibs_sweep]] | `fail` | Lower Band Reversion | -1.21 |  |
| [[wave4_lbr_no_sma200]] | `pass` | Lower Band Reversion | N/A |  |
