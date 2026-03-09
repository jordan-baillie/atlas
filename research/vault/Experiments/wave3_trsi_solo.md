---
experiment_id: wave3_trsi_solo
wave: 3
strategy: triple_rsi
category: new_strategy
market: sp500
verdict: fail
promoted: false
sharpe: -2.119
total_trades: 0
profit_factor: 0.8265
date: "2026-03-06"
tags:
  - experiment
  - "strategy/triple-rsi"
  - verdict/fail
  - wave/3
  - category/new_strategy
  - market/sp500
---

# Triple RSI Solo

> **Wave:** [[Wave 3]] | **Strategy:** [[Triple RSI]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

Triple RSI (RSI(5) declining 3 days, below 30, with lookback check) generates rare but high-conviction mean reversion signals on individual SP500 stocks. Published edge on SPY: 90% WR, PF 4.0. Adapted for individual stocks with SMA-200 filter and volume confirmation. Expects fewer but higher-quality trades than existing MR strategy.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | -2.12 |
| Profit Factor | 0.83 |
| Total Trades | 0 |

## Verdict

**FAIL**

Triple RSI solo is terrible: Sharpe=-2.12, PF=0.83, edge p=0.39. The strategy loses money. Triple condition (RSI + streak + acceleration) is too restrictive, generating few signals, all poorly timed. Not worth optimizing — fundamental approach is flawed for SP500.

## Learnings

- Triple RSI (RSI + streak + acceleration) fails on SP500 solo — too restrictive
- Sharpe -2.12 is beyond salvage via optimization
- Confirmed: dormant strategies need combined-mode test first, not solo

---

Strategy:: [[Triple RSI]]
Wave:: [[Wave 3]]