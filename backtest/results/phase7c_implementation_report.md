# Phase 7C Implementation Report: Market Breadth Confidence Modifiers

## Date: 2026-02-16
## Config: v6.0_phase7c_breadth

## Bug Fix
Initial run had ALL 39 trades penalized (0 boosted) due to scale mismatch:
- Breadth values stored as percentages (0-100) e.g. 38, 43, 67
- Thresholds were set as decimals (0.48, 0.58)
- All breadth values > 0.58 → all penalized
- FIX: Changed thresholds to 48.0 and 58.0

## Results (corrected thresholds)

| Metric | Baseline | Phase 7C | Change |
|--------|----------|----------|--------|
| CAGR | 3.19% | 3.24% | +0.05% |
| Max DD | 1.19% | 1.00% | -0.19% |
| Win Rate | 61.7% | 60.4% | -1.3% |
| Profit Factor | 2.77 | 2.65 | -0.12 |
| Sharpe | -0.421 | -0.385 | +0.036 |
| Sortino | -0.630 | -0.572 | +0.058 |
| Total Trades | 47 | 53 | +6 |
| Final Equity | $5,322.19 | $5,327.21 | +$5.02 |

## Breadth Zone Analysis (KEY FINDING)

| Zone | Trades | Win Rate | Total PnL | Avg PnL |
|------|--------|----------|-----------|---------|
| Boosted (<48%) | 27 | 81.5% | $317.90 | $11.77 |
| Neutral (48-58%) | 10 | 40.0% | $19.05 | $1.91 |
| Penalized (>58%) | 16 | 37.5% | -$9.76 | -$0.61 |

## By Strategy + Breadth Zone

| Strategy | Zone | Trades | WR | PnL |
|----------|------|--------|----|-----|
| Trend Following | Boosted | 14 | 86% | $221.12 |
| Trend Following | Penalized | 8 | 25% | $2.90 |
| Trend Following | Neutral | 4 | 0% | -$42.27 |
| Mean Reversion | Boosted | 13 | 77% | $96.78 |
| Mean Reversion | Penalized | 8 | 50% | -$12.66 |
| Mean Reversion | Neutral | 6 | 67% | $61.32 |

## Conclusion
Breadth modifiers are working as statistically predicted:
- Low breadth (recovery phase) = high win rate, captures 97% of total system profit
- High breadth (crowded market) = low win rate, net losers
- Conservative modifiers (±0.03 TF, ±0.015 MR) improve risk-adjusted metrics
- Max drawdown improved by 16% (1.19% → 1.00%)
- 6 additional trades captured via confidence boost (net positive PnL)

## Next Steps
- Out-of-sample validation before live deployment
- Consider stronger penalty for high-breadth regime
- Phase 7B (Relative Strength Ranking) as next enhancement
