---
experiment_id: wave2_tom_filter
wave: 2
strategy: portfolio_filter
category: filter
market: sp500
verdict: partial
promoted: false
sharpe: -0.6446
cagr: 2.87
max_drawdown: 2.58
win_rate: 52.17
total_trades: 115
profit_factor: 1.4003
total_pnl: 229.0
date: "2026-03-04"
tags:
  - experiment
  - "strategy/portfolio-filter"
  - verdict/partial
  - wave/2
  - category/filter
  - market/sp500
---

# Turn-of-Month Filter

> **Wave:** [[Wave 2]] | **Strategy:** [[Portfolio Filter]] | **Verdict:** `PARTIAL` | **Promoted:** ❌

## Hypothesis

The Turn of Month effect (last 5 + first 3 trading days) shows stocks generate virtually all monthly returns in this window (Lakonishok & Smidt 1988, confirmed 2024). Boosting signal confidence during TOM window (or suppressing signals mid-month) should improve trade quality. This is a calendar-based filter, completely uncorrelated with our price-based signals.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | -0.64 |
| Sortino | -0.95 |
| CAGR | 2.87% |
| Max Drawdown | 2.58% |
| Win Rate | 52.17% |
| Profit Factor | 1.40 |
| Total Trades | 115 |
| Total PnL | $229.00 |
| Avg Trade | $1.99 |
| Final Equity | $4234.11 |

## Strategy Breakdown

| Strategy | Trades | Total PnL | Win Rate |
|----------|--------|-----------|----------|
| trend_following | 87 | $174.80 | 50.60% |
| mean_reversion | 28 | $54.20 | 57.10% |

## Verdict

**PARTIAL**

*Criteria:* Sharpe improvement >= 0.02 when only taking trades in TOM window vs taking trades anytime. Min 100 trades to avoid under-trading.

1 pass, 2 fail: sharpe_improvement: metric not found; edge_p_value: 0.1266 >= 0.05 (edge not statistically significant)

## Learnings

- INFRASTRUCTURE FAILURE: All 3 TOM variants produced near-identical results (116/77/115 trades)
- filter_test sets s_cfg['turn_of_month'] but no strategy reads this parameter
- TOM filter needs to be IMPLEMENTED in the backtest engine or strategy base class before testing
- Calendar-based filters need engine-level support (check date against TOM window before signal generation)
- The hypothesis is NOT rejected — test was invalid due to missing implementation

---

Strategy:: [[Portfolio Filter]]
Wave:: [[Wave 2]]