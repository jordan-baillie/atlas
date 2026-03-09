---
experiment_id: wave1_sector_opt
wave: 1
strategy: sector_rotation
category: dormant
market: sp500
verdict: pass
promoted: false
sharpe: 0.43
cagr: 9.61
max_drawdown: 12.68
win_rate: 44.3
total_trades: 237
profit_factor: 1.48
date: "2026-03-01"
tags:
  - experiment
  - "strategy/sector-rotation"
  - verdict/pass
  - wave/1
  - category/dormant
  - market/sp500
---

# Sector Rotation Optimization

> **Wave:** [[Wave 1]] | **Strategy:** [[Sector Rotation]] | **Verdict:** `PASS` | **Promoted:** ❌

## Hypothesis

Coordinate descent on Sector Rotation params will improve solo performance significantly.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | 0.43 |
| CAGR | 9.61% |
| Max Drawdown | 12.68% |
| Win Rate | 44.30% |
| Profit Factor | 1.48 |
| Total Trades | 237 |

## Verdict

**PASS**

*Criteria:* Optimization should lift Sharpe by 0.1+ vs untuned solo, maintain 15+ trades.

All 3 criteria met: sharpe_improvement_vs_solo: 0.5364 >= 0.1; min_trades: 237.0000 >= 15; min_profit_factor: 1.4757 >= 1.1

## Delta vs Baseline

| Metric | Change |
|--------|--------|
| Sharpe | 0.54 |
| Cagr Pct | 6.36 |
| Profit Factor | 0.23 |
| Params Changed | {'top_sectors': '3→2', 'atr_stop_mult': '3.0→2.5', 'max_hold_days': '25→30'} |

## Learnings

- Sector rotation viable solo after optimization: Sharpe 0.43, CAGR 9.6%, PF 1.48
- Fewer sectors (2) + tighter stops (2.5x ATR) + longer holds (30d) is optimal
- Edge is statistically significant (p=0.015)
- Sector rotation optimization: 3 of 6 params changed (top_sectors 3→2, atr_stop_mult 3→2.5, max_hold_days 25→30)
- Solo Sharpe improved dramatically: -0.11 → 0.43 (+0.54)
- CAGR improved: 3.25% → 9.61% (+6.4pp)
- PF improved: 1.24 → 1.48, edge p-value now significant (0.015)
- Fewer sectors (2 vs 3) concentrates on strongest sectors → better returns
- Tighter stops (2.5x vs 3.0x ATR) and longer holds (30 vs 25 days) help

---

Strategy:: [[Sector Rotation]]
Wave:: [[Wave 1]]