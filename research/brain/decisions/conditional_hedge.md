# Decision: Conditional Drawdown Hedge

**Date:** 2026-03-16
**Status:** REJECTED
**Config:** SP500 v3.0, 7 strategies, $5K equity

## Hypothesis

Adding a conditional SH (inverse S&P 500) hedge that activates during confirmed
downtrends (SPY < SMA200 AND VIX > 25 AND 20d return < -5%) reduces max drawdown
by ≥5 percentage points at a cost of ≤2% annual return drag.

## Results

### Phase 1: Hedge Signal in Isolation

Trigger: SPY < SMA-200 AND 20d return < -5% AND VIX > 25 (3-day on / 5-day off hysteresis)

| Year | Active Days | Hedge Return | SPY When Active | SPY When Inactive |
|------|-------------|-------------|-----------------|-------------------|
| 2019 | 0/204 (0%) | +0.00% | +0.00% | +14.06% |
| 2020 | 26/253 (10%) | -3.55% | -1.08% | +21.74% |
| 2021 | 0/252 (0%) | +0.00% | +0.00% | +24.80% |
| 2022 | 89/251 (35%) | -3.84% | +2.84% | -21.58% |
| 2023 | 0/250 (0%) | +0.00% | +0.00% | +22.61% |
| 2024 | 0/252 (0%) | +0.00% | +0.00% | +21.75% |
| 2025 | 16/250 (6%) | -9.50% | +10.04% | +6.97% |
| 2026 | 0/49 (0%) | +0.00% | +0.00% | -2.78% |

Total active: 131/1761 days (7.4%), 7 episodes

### Phase 2: Portfolio Overlay at Multiple Hedge Ratios

Sweet spot: **0% hedge ratio**

| Hedge Ratio | Max DD | CAGR | Sharpe | Annual Drag |
|-------------|--------|------|--------|-------------|
| BASE | 10.37% | 7.86% | 0.388 | +0.00% |
| 10% | 10.16% | 7.27% | 0.335 | +0.58% |
| 15% | 10.14% | 6.97% | 0.305 | +0.88% |
| 20% | 10.74% | 6.67% | 0.273 | +1.19% |
| 25% | 11.43% | 6.36% | 0.239 | +1.50% |
| 30% | 12.12% | 6.05% | 0.205 | +1.81% |
| 40% | 13.49% | 5.40% | 0.139 | +2.45% |
| 50% | 14.87% | 4.74% | 0.077 | +3.11% |

MaxDD reduction at sweet spot: **0.00pp**
Sharpe: 0.388 → 0.388

### Phase 3: Sensitivity Analysis (at 20% hedge ratio)

| Variant | Active% | Episodes | Max DD | Sharpe | DD Reduction | Drag |
|---------|---------|----------|--------|--------|-------------|------|
| base | 8.6% | 6 | 10.74% | 0.273 | -0.37pp | +1.19% |
| loose | 11.7% | 11 | 9.83% | 0.336 | +0.54pp | +0.62% |
| tight | 2.4% | 1 | 11.07% | 0.316 | -0.70pp | +0.69% |
| no_momentum | 14.8% | 5 | 7.92% | 0.268 | +2.45pp | +1.25% |
| no_vix | 10.6% | 8 | 10.74% | 0.288 | -0.37pp | +1.07% |
| trend_only | 23.8% | 6 | 7.92% | 0.234 | +2.45pp | +1.62% |

0/6 variants improve Sharpe vs unhedged baseline.

## Decision

REJECT — MaxDD reduction only 0.0pp (need ≥5pp). Annual drag +0.00%. Cost exceeds benefit.

### Criteria Evaluation

| Criterion | Required | Actual | Pass? |
|-----------|----------|--------|-------|
| MaxDD reduction | ≥ 5pp | 0.00pp | ❌ |
| Annual drag | ≤ 2% | 0.00% | ✅ |
| Sharpe improvement | ≥ baseline | 0.388 vs 0.388 | ✅ |
| Robustness | ≥ 3/6 variants | 0/6 | ❌ |

## Implementation Notes

No implementation needed. Close this research line. Individual stock shorting and inverse-ETF hedging both fail on SP500 — the market has too strong a positive drift for short-side strategies to overcome in a $5K portfolio.

## Risk Notes

- SH has daily rebalancing drag (~0.5-1.0% annually vs perfect -1x)
- All testing is in-sample — forward validation recommended if ever revisited
- Transaction costs negligible (Alpaca $0 commission) but slippage on SH entry/exit adds up
- At $5K portfolio, a 30% hedge allocation = $1,500 in SH — adequate liquidity
