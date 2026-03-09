---
experiment_id: wave2_vol_combined
wave: 2
strategy: portfolio_filter
category: filter
market: sp500
verdict: fail
promoted: false
sharpe: -1.0448
cagr: 1.36
max_drawdown: 4.51
win_rate: 50.43
total_trades: 117
profit_factor: 1.1572
total_pnl: 104.92
date: "2026-03-04"
tags:
  - experiment
  - "strategy/portfolio-filter"
  - verdict/fail
  - wave/2
  - category/filter
  - market/sp500
---

# Volume Combined

> **Wave:** [[Wave 2]] | **Strategy:** [[Portfolio Filter]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

Wave 1 proved 1.5x volume filter on MR solo: Sharpe -0.02→0.38, PF 1.30→1.62. Applying to the full combined portfolio should similarly improve signal quality by filtering out low-conviction entries across all strategies.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | -1.04 |
| Sortino | -1.56 |
| CAGR | 1.36% |
| Max Drawdown | 4.51% |
| Win Rate | 50.43% |
| Profit Factor | 1.16 |
| Total Trades | 117 |
| Total PnL | $104.92 |
| Avg Trade | $0.90 |
| Final Equity | $4110.12 |

## Strategy Breakdown

| Strategy | Trades | Total PnL | Win Rate |
|----------|--------|-----------|----------|
| trend_following | 93 | $60.84 | 49.50% |
| mean_reversion | 24 | $44.08 | 54.20% |

## Verdict

**FAIL**

*Criteria:* Sharpe improvement >= +0.03 over baseline combined portfolio. Max drawdown increase <= 1pp. Min 150 trades after filtering.

All 4 criteria failed: sharpe_improvement: metric not found; max_dd_increase_pp: metric not found; min_trades: 117.0000 < 150; edge_p_value: 0.3424 >= 0.05 (edge not statistically significant)

## Learnings

- INFRASTRUCTURE FAILURE: All 3 volume variants produced near-identical results (116/117/111 trades)
- filter_test sets s_cfg['volume_min_ratio'] but strategies read from s_cfg['volume']['min_ratio'] (nested path)
- Need to fix filter_test to handle nested config params before retesting volume filter
- The hypothesis is NOT rejected — test was invalid due to config path mismatch
- Wave 1 solo result (1.5x volume filter: Sharpe -0.02→0.38) remains valid and promising

---

Strategy:: [[Portfolio Filter]]
Wave:: [[Wave 2]]