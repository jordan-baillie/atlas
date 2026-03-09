---
type: pattern
status: confirmed
impact: high
discovered_in: "[[Wave 1]]"
tags:
  - pattern
  - pattern/confirmed
---

# VIX Filter Counterproductive

> **Type:** Pattern | **Status:** Confirmed | **Impact:** High | **Discovered:** [[Wave 1]]

VIX filter is counterproductive for MR-heavy portfolio because MR needs high-VIX panic periods.

## Finding

All 4 VIX threshold levels tested (20, 25, 30, 35) degrade portfolio performance.

## Root Cause

**Mean reversion thrives during high-VIX (panic) periods.** When VIX is high:
- Stocks are oversold, creating large z-score dislocations
- Reversal probability is highest
- MR generates its best signals

Applying a VIX filter blocks entries precisely when MR alpha is highest.

## Implication

- **CLOSED**: Do not re-test VIX filters on combined portfolio
- VIX filter might work for a **trend-only** portfolio (trends break down in panic)
- For MR-heavy portfolios: VIX is a signal TO enter, not to avoid

## Related

- [[Wave 1]]
- [[Wave 3]]
