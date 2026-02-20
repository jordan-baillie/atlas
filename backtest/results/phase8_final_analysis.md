# Phase 8BCD Analysis Report
## Date: 2026-02-17

## Summary
Phase 8 attempted three enhancements:
- **8B (Universe Expansion)**: Expanded from 100 to 159 tickers → ✅ KEPT
- **8C (Sector Rotation)**: Added SR strategy with min_conf=0.80 → ❌ DISABLED
- **8D (Dynamic Position Sizing)**: Confidence/vol/equity curve scaling → ❌ DISABLED

## Comprehensive Test Results

| Config | Trades | WR% | CAGR% | DD% | PF | PnL$ |
|--------|--------|-----|-------|-----|-----|------|
| A: v7.0 base (5pos, fixed) | 54 | 61.1% | 3.43% | 1.25% | 2.74 | $347 |
| C: 8D only (5pos, dyn) | 166 | 56.0% | 1.73% | 6.57% | 1.11 | $173 |
| D: 8B+8D (8pos, dyn) | 220 | 54.1% | 2.40% | 8.60% | 1.09 | $184 |
| E: 8B+8C (5pos, fixed+SR) | 74 | 54.0% | 2.65% | 2.54% | 1.52 | $267 |
| F: Full 8BCD (5pos, dyn+SR) | 183 | 51.9% | 5.28% | 6.96% | 1.29 | $527 |

## Key Findings

### 8D Dynamic Sizing: DESTRUCTIVE
- Caused trade count explosion from 54 → 166-220 trades
- Smaller position sizes created more capacity for marginal trades
- Degraded all risk-adjusted metrics: WR dropped 5-7%, DD increased 5-7x
- Mean reversion went from +$164 (profitable) to -$90 to -$172 (net loser)
- Root cause: With $5000 equity, reducing risk from 0.5% to 0.3% creates
  many positions barely above the $500 minimum threshold

### 8C Sector Rotation: NET NEGATIVE
- Even with min_confidence=0.80 filter, SR crowds out profitable TF/MR trades
- TF trades dropped from 27 to 17 when SR added (test E)
- SR itself is a net loser (-$34.59) despite passing higher confidence bar
- Reduces overall system WR and PF

### 8B Universe Expansion: BENEFICIAL
- More tickers provide more opportunities without changing strategy behavior
- Same 54 trades but from a larger pool = better signal selection
- This was already captured in the v7.0 expanded universe

## Optimal Configuration: v8.2
- TF + MR strategies only (no SR, no momentum breakout)
- Expanded universe (159 tickers, 115 with sufficient data)
- Fixed position sizing at 0.5% risk per trade
- max_open_positions = 5
- All Phase 7 enhancements retained (breadth modifiers, RS penalties, volume info)

## Decision
Reverted to 8B-only configuration. Dynamic sizing and sector rotation are
theoretically sound but empirically destructive for this specific system 
with its small equity base and limited trade frequency.
