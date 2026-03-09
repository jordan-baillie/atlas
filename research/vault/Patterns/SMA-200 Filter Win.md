---
type: pattern
status: confirmed
impact: high
discovered_in: "[[Wave 1]]"
tags:
  - pattern
  - pattern/confirmed
---

# SMA-200 Filter Win

> **Type:** Pattern | **Status:** Confirmed | **Impact:** High | **Discovered:** [[Wave 1]]

Biggest filter win: +0.28 Sharpe improvement, promoted to v2.1 config.

## Finding

Adding SMA-200 filter (only enter trades when stock is above its 200-day moving average) to all 3 active strategies:

| Metric | Before | After | Delta |
|--------|--------|-------|-------|
| Sharpe | 0.59 | 0.87 | **+0.28** |
| CAGR | 10.1% | 11.7% | +1.6pp |
| Max DD | 6.6% | 5.3% | -1.2pp |
| Trades | 443 | 270 | -39% |

## Why It Works

SMA-200 filters out entries in downtrending stocks. When a stock is below its 200-day MA:
- More likely to continue lower (trend confirmation)
- Mean reversion entries face stronger headwinds
- Breakout entries have lower follow-through

## Note

Previous coordinate descent optimization **rejected** SMA-200 because it reduces trade count too aggressively
(optimizer penalizes low trade counts). The filter only shows its value in a clean A/B test.

## Promotion

Promoted to `config/versions/sp500_v2.1.json`. Applied to mean_reversion, trend_following, and opening_gap.

## Related

- [[Wave 1]]
- [[SMA-200 Filter]]
