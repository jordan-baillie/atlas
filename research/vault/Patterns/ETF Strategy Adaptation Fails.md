---
type: pattern
status: confirmed
impact: high
discovered_in: "[[Wave 2]]"
tags:
  - pattern
  - pattern/confirmed
---

# ETF Strategy Adaptation Fails

> **Type:** Pattern | **Status:** Confirmed | **Impact:** High | **Discovered:** [[Wave 2]]

ConnorsRSI2 and LBR designed for ETFs fail on individual stocks. Don't adapt ETF strategies to stocks.

## Finding

Strategies published for ETFs (SPY, QQQ) do not transfer directly to individual SP500 stocks:

- **ConnorsRSI2**: Designed for ETF mean reversion. On individual stocks generates insufficient trades and negative Sharpe.
- **Lower Band Reversion (LBR)**: Published Sharpe 2.11 on SPY. On individual stocks: Sharpe -2.08, despite 58% win rate.

## Root Cause

ETFs have smoother price action, stronger mean reversion properties, and lower volatility per unit than individual stocks.
Strategies calibrated for ETF distributions underfit the noisier individual stock signals.

## Implication

Do not directly adapt ETF-backtested strategies to individual stocks without:
1. Re-parameterizing for individual stock distributions
2. Adding stock-specific filters (earnings blackouts, liquidity)
3. Accepting significantly reduced signal quality

## Related

- [[Wave 2]]
- [[Wave 4]]
