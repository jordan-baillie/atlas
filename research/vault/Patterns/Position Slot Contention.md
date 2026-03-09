---
type: pattern
status: confirmed
impact: high
discovered_in: "[[Wave 1]]"
tags:
  - pattern
  - pattern/confirmed
---

# Position Slot Contention

> **Type:** Pattern | **Status:** Confirmed | **Impact:** High | **Discovered:** [[Wave 1]]

All 4 dormant strategies fail combined portfolio due to slot contention at max_positions=10. Need allocation pools before adding strategies.

## Finding

Every dormant strategy tested in waves 1-3 passed the solo test but failed when added to the combined portfolio:

| Strategy | Solo Sharpe | Combined Sharpe Delta |
|----------|-------------|----------------------|
| Momentum Breakout | 0.30 | -0.75 |
| Short Term MR | 0.27 | -0.29 |
| BB Squeeze | ~0 | degraded |
| Sector Rotation | 0.43 | degraded |

## Root Cause

`max_open_positions=10` is a zero-sum constraint. When a new strategy adds 200-700 trades/year,
it competes directly for position slots with the proven MR+TF+OG strategies that drive returns.

## Implication

**Do not add more strategies until allocation pools are implemented.**
Need per-strategy position caps (e.g., MR gets 5 slots, TF gets 3, new strategy gets 2).

## Resolution Path

Implement allocation pools feature to partition position slots per strategy type.
This unlocks all dormant strategies that passed solo testing.

## Related

- [[Wave 1]]
- [[Wave 3]]
