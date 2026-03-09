---
experiment_id: wave1_sector_comb
wave: 1
strategy: sector_rotation
category: dormant
market: sp500
verdict: fail
promoted: false
sharpe: 0.55
cagr: 11.06
max_drawdown: 11.57
total_trades: 339
date: "2026-03-01"
tags:
  - experiment
  - "strategy/sector-rotation"
  - verdict/fail
  - wave/1
  - category/dormant
  - market/sp500
---

# Sector Rotation Combined

> **Wave:** [[Wave 1]] | **Strategy:** [[Sector Rotation]] | **Verdict:** `FAIL` | **Promoted:** ❌

## Hypothesis

Adding optimized Sector Rotation to the active strategy set (MR+TF+OG) improves the portfolio.

## Results

| Metric | Value |
|--------|-------|
| Sharpe | 0.55 |
| CAGR | 11.06% |
| Max Drawdown | 11.57% |
| Total Trades | 339 |

**Combined vs Baseline:**

| Metric | Baseline | Combined | Delta |
|--------|----------|----------|-------|
| Sharpe | 0.87 | 0.55 | -0.32 |
| CAGR | 11.66% | 11.06% | -0.60% |
| Max DD | 5.33% | 11.57% | — |

## Verdict

**FAIL**

*Criteria:* Combined portfolio must maintain Sharpe >= 1.04, DD <= 6.4%, strategy contributes 10+ trades. Sector rotation is fundamentally different signal source. Even modest solo performance may improve portfolio via decorrelation.

Adding optimized sector rotation to active portfolio degrades Sharpe by 0.32 (0.87 → 0.55), doubles DD from 5.3% to 11.6%. Position contention: SR takes 178 of 339 trades, crowding MR (108→71) and TF (156→86). Same failure mode as momentum_breakout and short_term_mr combined tests.

## Delta vs Baseline

| Metric | Change |
|--------|--------|
| Sharpe | -0.32 |
| Cagr Pct | -0.60 |
| Max Drawdown Pct | 6.24 |

## Learnings

- PATTERN CONFIRMED (4th time): dormant strategies fail combined test due to position contention
- All 4 tested (momentum_breakout, short_term_mr, bb_squeeze, sector_rotation) degrade portfolio when added
- Root cause: max_open_positions=10 creates zero-sum competition for position slots
- SR crowds out MR (-34%) and TF (-45%) — both proven profit drivers
- DECISION: Wave 1 dormant activation theme is CLOSED
- NEXT PRIORITY: Position allocation pools (per-strategy-type max positions) to unlock new strategies
- Sector rotation solo is strong after optimization (Sharpe 0.43, CAGR 9.6%, PF 1.48, 237 trades)
- But combined portfolio DEGRADES: Sharpe 0.87→0.55, DD 5.3%→11.6%
- SR takes 178/339 trades, crowding out MR (108→71, -34%) and TF (156→86, -45%)
- PATTERN CONFIRMED: All 4 dormant strategies fail combined test due to max_open_positions=10 contention
- Position pool is the bottleneck — not strategy quality
- Future: separate allocation pools per strategy type, or increase max_open_positions
- Total PnL actually increases ($985→$1050) but risk-adjusted metrics collapse

---

Strategy:: [[Sector Rotation]]
Wave:: [[Wave 1]]