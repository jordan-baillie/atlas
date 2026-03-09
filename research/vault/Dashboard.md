---
tags:
  - dashboard
  - MOC
---
# Atlas Research Dashboard

> Research-first trading lab. Building evidence for future go-live decisions.

## Quick Views
- [[All Experiments]] — Database of all 82+ experiments with filtering
- [[Strategy Scorecard]] — Performance overview by strategy  
- [[Promoted]] — Experiments that led to config changes

## Strategies
- [[Mean Reversion]] | [[Trend Following]] | [[Opening Gap]]
- [[Momentum Breakout]] | [[Short Term MR]] | [[Sector Rotation]]
- [[MTF Momentum]] | [[Bollinger Band Squeeze]] | [[ConnorsRSI2]]
- [[Lower Band Reversion]] | [[Triple RSI]] | [[SMA-200 Filter]]

## Research Waves
- [[Wave 1]] — Dormant Strategy Activation & Portfolio Filters
- [[Wave 2]] — Parameter Sensitivity & Combined Filters
- [[Wave 3]] — Config Refinement & Hold Period Optimization
- [[Wave 4]] — New Strategy Exploration (LBR, ConnorsRSI2)
- [[Wave 5]] — Re-optimization & CDD Strategy

## Confirmed Patterns
- [[Fee Drag at Low Equity]]
- [[ETF Strategy Adaptation Fails]]
- [[Position Slot Contention]]
- [[SMA-200 Filter Win]]
- [[VIX Filter Counterproductive]]

## Key Metrics (Baseline v2.2)
| Metric | Value |
|--------|-------|
| Sharpe | 1.04 |
| CAGR | 15.7% |
| Total Trades | 425 |
| Win Rate | 56% |
| Profit Factor | 1.50 |
| Max Drawdown | ~12% |

## How to Use This Vault
1. Browse experiments in [[All Experiments]] base view — filter by strategy, verdict, wave
2. Click any strategy to see its full experiment history via backlinks
3. Use the Graph View to see how strategies, experiments, and patterns connect
4. Add new experiments using the [[Experiment]] template
5. Regenerate vault from data: `python3 scripts/build_obsidian_vault.py --force`
