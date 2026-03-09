---
experiment_id: wave1_short__comb
wave: 1
strategy: short_term_mr
category: dormant
market: sp500
verdict: fail
promoted: false
sharpe: 0.3
cagr: 7.69
max_drawdown: 8.06
date: "2026-02-27"
tags:
  - experiment
  - "strategy/short-term-mr"
  - verdict/fail
  - wave/1
  - category/dormant
  - market/sp500
---

# Short Comb

> **Wave:** [[Wave 1]] | **Strategy:** [[Short Term MR]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

Adding optimized Short-Term Mean Reversion to the active strategy set (MR+TF+OG) improves the portfolio.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | 0.30 |
| CAGR | 7.69% |
| Max Drawdown | 8.06% |

**Combined vs Baseline:**

| Metric | Baseline | Combined | Delta |
|--------|----------|----------|-------|
| Sharpe | 0.59 | 0.30 | -0.29 |
| CAGR | 10.05% | 7.69% | -2.36% |
| Max DD | 6.56% | 8.06% | — |

## Verdict

**FAIL**

*Criteria:* Combined portfolio must maintain Sharpe >= 1.04, DD <= 6.4%, strategy contributes 10+ trades. Critical: measure signal overlap with existing mean_reversion. If overlap >30%, diversification value is too low.

2 pass, 2 fail: min_combined_sharpe: 0.3023 < 1.04; max_combined_dd: 8.0621 > 6.39 — Short-term MR degrades portfolio (Sharpe -0.29, CAGR -2.4pp). REJECT for portfolio inclusion.

## Delta vs Baseline

| Metric | Change |
|--------|--------|
| Sharpe | -0.29 |
| Cagr Pct | -2.36 |
| Max Drawdown Pct | 1.50 |

## Learnings

- Short-term MR is profitable solo after optimization (Sharpe 0.27, CAGR 7.6%, 63% WR)
- But adding it to the active portfolio degrades Sharpe by 0.29 and CAGR by 2.4pp
- The 697 STMR trades compete with MR/TF for 10 max positions
- With both MR variants active, the portfolio is over-concentrated in mean reversion signals
- PATTERN: Both dormant strategies fail the combined test due to position allocation contention
- Future work: test with increased max_open_positions or separate allocation pools per strategy type

---

Strategy:: [[Short Term MR]]
Wave:: [[Wave 1]]