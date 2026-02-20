# Phase 8A Analysis Report: Momentum Breakout Re-enablement
## Date: 2026-02-17
## Status: COMPLETED - MB REJECTED, REVERTED TO v7.0 BASELINE

---

## Objective
Re-enable the Momentum Breakout (MB) strategy that was disabled in Phase 3 as a "consistent net loser",
now benefiting from Phase 7 improvements (wider ATR stops, breadth modifiers, RS filtering).

## Changes Made to MB Strategy
1. Removed hard volume filter (1.5x) → info-only (matching Phase 7A findings)
2. Adjusted confidence formula: base 0.6 + breakout strength + trend alignment bonuses
3. Widened ATR stops to 3.5x/4.0x (matching TF's proven parameters)
4. Added breadth/RS enrichment support
5. Increased max_hold_days from 15 → 20

## Test Results

### Standalone Momentum Breakout Variants
| Variant | Trades | WR% | PF | PnL | CAGR | MaxDD | Stops | Stop PnL |
|---------|--------|-----|-----|------|------|-------|-------|----------|
| v8.0 baseline (3.5x/4.0x) | 37 | 48.6% | 0.69 | -$136.66 | -1.38% | 6.08% | 10 | -$365.24 |
| v8.1 conf≥0.80 | 21 | 42.9% | 0.48 | -$166.96 | -1.69% | 4.46% | 7 | -$301.64 |
| v8.2 wider stops (4.0x/4.5x) | 15 | 66.7% | 1.44 | +$36.51 | +0.37% | 1.87% | 2 | -$54.62 |
| v8.3 both combined | 5 | 20.0% | 0.34 | -$26.24 | -0.26% | 0.91% | 0 | $0.00 |

### Key Finding: Stop Hits Are the Killer
- Exit analysis showed time_exit trades: 26 trades, 69.2% WR, +$250.66 (profitable!)
- Stop_hit trades: 10 trades, 0% WR, -$365.24 (catastrophic)
- Wider stops (v8.2) reduced stop-hits from 10 to 2, making standalone MB marginally profitable

### Combined 3-Strategy Results (v8.2 best variant)
| Metric | v7.0 Baseline (TF+MR) | v8.2 (TF+MR+MB) | Delta |
|--------|----------------------|------------------|-------|
| Total Trades | 53 | 51 | -2 |
| Win Rate | 62.3% | 60.8% | -1.5% |
| Profit Factor | 2.72 | 1.66 | -1.06 |
| Total PnL | $333.62 | $172.80 | -$160.82 |
| CAGR | 3.30% | 1.72% | -1.58% |
| Max Drawdown | 1.00% | 2.86% | +1.85% |
| OOS Trades | 25 | 20 | -5 |
| OOS Win Rate | 52.0% | 50.0% | -2.0% |
| OOS PF | 1.66 | 1.79 | +0.13 |
| OOS PnL | $65.26 | $63.35 | -$1.91 |

### Strategy Breakdown (Combined v8.2)
| Strategy | Trades | WR% | PnL | OOS Trades | OOS WR% | OOS PnL |
|----------|--------|-----|------|------------|---------|----------|
| Trend Following | 13 | 61.5% | $99.84 | 7 | 42.9% | $18.15 |
| Mean Reversion | 21 | 61.9% | $95.78 | 8 | 50.0% | $3.24 |
| Momentum Breakout | 17 | 58.8% | -$22.82 | 5 | 60.0% | $41.96 |

## Root Cause Analysis

### Why MB Fails in Combined Mode (The Crowding Problem)
1. **Position Slot Competition**: With max_open_positions=5, MB competes with TF/MR for slots
2. **TF trades halved**: 26 → 13 trades ($188.26 → $99.84) when MB is active
3. **MR trades reduced**: 27 → 21 trades ($145.36 → $95.78)
4. **Net destruction**: MB's marginal gains don't offset the lost TF/MR profits
5. **MB cannibalizes higher-quality strategies** by occupying position slots

### Why MB Fails Standalone
1. **Poor risk:reward**: Avg winner $16.63 vs avg loser $22.94
2. **Stop-hit vulnerability**: Breakout entries often near highs, prone to pullback stops
3. **Low confidence quality**: 65% of trades in 0.75-0.80 bucket with only 41.7% WR
4. **Breakout characteristics on ASX**: ASX mid-caps tend to mean-revert after breakouts

## Decision: REJECT Momentum Breakout
Reverted to v7.0 baseline (TF + MR only). Even the best MB variant (v8.2 wider stops)
degrades the combined system from 3.30% to 1.72% CAGR while nearly tripling drawdown.

## Implications for Phase 8 Strategy
The trade frequency bottleneck cannot be solved by adding momentum breakout.
Alternative approaches for increasing capital utilization:

1. **Universe Expansion (Phase 8B)**: More stocks = more TF/MR signals without slot competition
2. **Increase max_open_positions**: Allow more concurrent trades (requires risk analysis)
3. **Different Strategy Concept**: Sector rotation, pair trading, or gap-fade strategies
   that generate signals uncorrelated with TF/MR timing
4. **Dynamic Position Sizing**: Use idle capital more efficiently

## Files Modified
- `strategies/momentum_breakout.py` - Updated with Phase 7 improvements
- `config/config_v8.0_phase8a_momentum.json` - Created and tested
- `config/active_config.json` - REVERTED to v7.0_phase7_live

## Files Created
- `backtest/results/phase8a_momentum_results.json`
- `backtest/results/phase8a_v82_comparison.json`
- `backtest/results/phase8a_analysis_report.md` (this file)
