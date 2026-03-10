---
strategy_id: short_term_mr
type: strategy
status: dormant
total_experiments: 7
best_sharpe: 0.3913
tags:
  - strategy
  - "strategy/short-term-mr"
---

# Short Term MR

> **Status:** `DORMANT` | **Experiments:** 7 | **Promotions:** 0

## Overview

Research strategy `short_term_mr`. See experiments below.

## Aggregate Metrics

| Metric | Value |
|--------|-------|
| Best Sharpe | 0.39 |
| Worst Sharpe | -1.52 |
| Avg Sharpe | -0.11 |
| Total Experiments | 7 |
| Pass / Partial / Fail | 2 / 4 / 1 |
| Promotions | 0 |

## Experiment History

| Experiment | Wave | Verdict | Sharpe | CAGR | Promoted |
|------------|------|---------|--------|------|----------|
| [[wave1_short__solo]] | 1 | `pass` | -0.45 | -1.67% |  |
| [[wave1_short__opt]] | 1 | `pass` | 0.27 | 7.65% |  |
| [[wave1_short__comb]] | 1 | `partial` | 0.27 | 7.65% |  |
| [[wave1_short__comb]] | 1 | `fail` | 0.30 | 7.69% |  |
| [[20260310_181024_d14b81]] | ? | `partial` | -1.52 | -2.04% |  |
| [[20260310_183205_f86e93]] | ? | `partial` | 0.39 | 39.71% |  |
| [[20260310_185801_754a55]] | ? | `partial` | 0.39 | 39.71% |  |

## Key Learnings

- Short-term MR generates 946 trades — highest trade count of any dormant strategy tested
- 58.6% WR suggests signal quality, but PF 0.96 means losses slightly exceed wins
- Massive trade count (946) will create severe slot contention in combined portfolio at max_positions=10
- Viable for optimization — high trade count gives optimizer plenty of data to tune parameters
- Optimization improved Sharpe from -0.45 to +0.27 — significant improvement
- Post-optimization: 697 trades, 63% WR, PF 1.17, CAGR 7.6%
- Trade count reduced 946→697 (26%) through optimization — still very high
- PF improvement 0.96→1.17 shows optimizer found genuine edge in parameter space
- Short-term MR is profitable solo after optimization (Sharpe 0.27, CAGR 7.6%, 63% WR)
- But adding it to the active portfolio degrades Sharpe by 0.29 and CAGR by 2.4pp
- The 697 STMR trades compete with MR/TF for 10 max positions
- With both MR variants active, the portfolio is over-concentrated in mean reversion signals
- PATTERN: Both dormant strategies fail the combined test due to position allocation contention
- Future work: test with increased max_open_positions or separate allocation pools per strategy type
