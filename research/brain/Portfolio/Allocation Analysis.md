# Portfolio Allocation Analysis

> Generated: 2026-03-12 17:35

## Portfolio Metrics

- **Analytic Sharpe**: 0.4634
- **Simulated Sharpe**: 0.9919
- **Strategies active**: 6
- **Average correlation**: 0.2090
- **Annual return**: 10.8%
- **Annual volatility**: 10.9%
- **Max drawdown**: -6.4%

## Optimal Weights (Sharpe-tilted inverse-vol)

| Strategy | Weight | Sharpe | Trades | CAGR% | Group |
|----------|--------|--------|--------|-------|-------|
| sector_rotation | 24.9% | 0.401 | 773 | 10.4% | other |
| mean_reversion | 23.8% | 0.229 | 240 | 7.2% | mean_reversion |
| opening_gap | 19.1% | 0.271 | 392 | 8.7% | other |
| connors_rsi2 | 14.8% | 0.175 | 925 | 6.7% | mean_reversion |
| momentum_breakout | 14.3% | 0.152 | 397 | 6.3% | momentum |
| short_term_mr | 3.0% | 0.395 | 878 | 36.0% | mean_reversion |

## Excluded Strategies (weight = 0)

- **bb_squeeze**: Sharpe=-0.746, trades=371 — low Sharpe
- **consecutive_down_days**: Sharpe=-1.610, trades=1605 — low Sharpe
- **lower_band_reversion**: Sharpe=-1.360, trades=780 — low Sharpe
- **trend_following**: Sharpe=-0.388, trades=302 — low Sharpe

## Method

Weights computed as w_i ∝ SR_i / σ_i (Sharpe-ratio-tilted inverse-volatility),
with Ledoit-Wolf shrinkage on the covariance matrix.
Constraints: max 25% per strategy, min 3%.

References: Bailey & López de Prado (2013), Treynor-Black theorem