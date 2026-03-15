# Mean-Variance Portfolio Optimization

**Date:** 2026-03-14  
**Status:** IMPLEMENTED (live comparison pending)

## Decision

Added mean-variance (MV) portfolio optimization as an alternative to the default Sharpe-weighted inverse-volatility allocation.

## Problem

The original allocation system used Sharpe-ratio-tilted inverse-volatility weights. This ignores cross-strategy correlations — two highly correlated strategies could receive large combined weight, concentrating risk.

## Implementation

### Optimizer

- `compute_optimal_weights_mv()` in the portfolio optimizer module
- Uses scipy SLSQP to minimize portfolio variance subject to constraints
- Constraints: weights sum to 1, each weight ≥ 0 (no shorting at portfolio level)
- Objective: minimize w'Σw (portfolio variance) with Sharpe tilt for expected returns

### Correlation Clustering

- `cluster_strategies()` uses union-find algorithm
- Groups strategies with correlation > 0.7 into clusters
- Correctly identifies MR/CR2/OG as correlated (all mean-reversion family)
- Cluster weight caps prevent over-concentration

### Fallback

- If MV optimization fails (singular covariance, insufficient data), falls back to Sharpe-weighted inverse-volatility
- Config dispatch: `portfolio_optimizer.method: "mean_variance"` or `"sharpe_inverse_vol"`

## Files

- Portfolio optimizer module (`compute_optimal_weights_mv`, `cluster_strategies`)
- Config: `portfolio_optimizer.method` (default: `"sharpe_inverse_vol"`)
- Tests: 17 tests covering optimizer and clustering

## TODO

- [ ] Compare MV weights vs SR/σ weights on live forward returns
- [ ] Evaluate Sharpe improvement from MV on 2024-2025 data
- [ ] Consider adding Black-Litterman views for subjective strategy beliefs
