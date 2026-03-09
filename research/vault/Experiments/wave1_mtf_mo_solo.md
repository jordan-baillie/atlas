---
experiment_id: wave1_mtf_mo_solo
wave: 1
strategy: mtf_momentum
category: dormant
market: sp500
verdict: fail
promoted: false
sharpe: 0.0
cagr: 0.0
total_trades: 0
date: "2026-03-02"
tags:
  - experiment
  - "strategy/mtf-momentum"
  - verdict/fail
  - wave/1
  - category/dormant
  - market/sp500
---

# MTF Momentum Solo

> **Wave:** [[Wave 1]] | **Strategy:** [[MTF Momentum]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

MTF momentum enters daily pullbacks within weekly uptrends. This is similar in spirit to trend_following but uses a different timeframe for trend confirmation (weekly SMA vs daily MA crossover). Key question: signal overlap with trend_following. If >60% overlap, the strategy adds complexity without diversification.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | 0.00 |
| CAGR | 0.00% |
| Total Trades | 0 |

## Verdict

**FAIL**

*Criteria:* Solo viability check: strategy generates 10+ trades, WR > 35%, PF > 0.7. Optimization will tune for profitability. Updated: removed positive_pnl requirement (untuned defaults rarely profitable).

MTF Momentum generates signals at confidence=0.50 (hardcoded) but min_confidence=0.75 in config. All signals filtered out by backtest engine. This is the 3rd failure — root cause is now confirmed: the strategy never sets confidence above 0.50 so it can never pass the min_confidence gate.

## Learnings

- ROOT CAUSE: confidence=0.50 hardcoded in mtf_momentum.py, filtered by min_confidence=0.75 in config
- 3rd failure — first 2 were API signature bugs (fixed), this is the remaining blocker
- Strategy generates hundreds of signals but none pass confidence gate
- FIX: Add dynamic confidence scoring or per-strategy min_confidence override
- BLOCKED: Do not re-queue until confidence calculation is implemented
- ROOT CAUSE FOUND: strategies/mtf_momentum.py hardcodes confidence=0.5 at line 173
- Config risk.min_confidence=0.75 filters out ALL MTF signals (0.50 < 0.75)
- Previous 2 failures were misdiagnosed as API signature bugs — those were real but not the only issue
- FIX NEEDED: MTF momentum needs dynamic confidence scoring (like other strategies) or min_confidence override in config
- Two fix options: (1) Add confidence calculation based on signal quality, or (2) Set mtf_momentum.min_confidence=0.4 in config
- PATTERN: All strategies need confidence scoring above min_confidence threshold to generate trades
- This experiment chain (solo/opt/comb/oos) should be DEFERRED until the confidence bug is fixed

---

Strategy:: [[MTF Momentum]]
Wave:: [[Wave 1]]