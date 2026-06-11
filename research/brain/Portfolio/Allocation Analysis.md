# Portfolio Allocation Analysis

> Generated: 2026-06-07 08:40

## Portfolio Metrics

- **Analytic Sharpe**: 0.4650
- **Simulated Sharpe**: 1.0197
- **Strategies active**: 5
- **Average correlation**: 0.4452
- **Annual return**: 9.3%
- **Annual volatility**: 9.2%
- **Max drawdown**: -9.1%

## Optimal Weights (Sharpe-tilted inverse-vol)

| Strategy | Weight | Sharpe | Trades | CAGR% | Group |
|----------|--------|--------|--------|-------|-------|
| cross_sectional_momentum | 25.0% | 0.437 | 439 | 11.0% | other |
| pead_earnings_drift | 25.0% | 0.777 | 255 | 11.0% | other |
| sector_rotation | 18.1% | 0.300 | 1072 | 9.1% | other |
| momentum_breakout | 16.5% | 0.111 | 491 | 5.6% | momentum |
| relative_strength_pullback | 15.4% | 0.109 | 452 | 5.6% | momentum |

## Excluded Strategies (weight = 0)

- **adx_trend_pullback**: Sharpe=-0.580, trades=574 — low Sharpe
- **bb_squeeze**: Sharpe=-0.317, trades=336 — low Sharpe
- **connors_rsi2**: Sharpe=-0.082, trades=1008 — low Sharpe
- **consecutive_down_days**: Sharpe=-0.572, trades=1470 — low Sharpe
- **demark_sequential**: Sharpe=-0.226, trades=436 — low Sharpe
- **dividend_capture**: Sharpe=-0.386, trades=236 — low Sharpe
- **donchian_breakout**: Sharpe=-0.212, trades=206 — low Sharpe
- **gap_and_go**: Sharpe=-0.211, trades=433 — low Sharpe
- **heikin_ashi_reversal**: Sharpe=-0.176, trades=513 — low Sharpe
- **inside_bar_nr7**: Sharpe=-0.744, trades=626 — low Sharpe
- **keltner_reversion**: Sharpe=-0.360, trades=468 — low Sharpe
- **lower_band_reversion**: Sharpe=-0.367, trades=907 — low Sharpe
- **macd_divergence**: Sharpe=-0.782, trades=370 — low Sharpe
- **mean_reversion**: Sharpe=-0.220, trades=364 — low Sharpe
- **monthly_rotation**: Sharpe=-0.627, trades=88 — low Sharpe
- **mtf_momentum**: Sharpe=-0.617, trades=144 — low Sharpe
- **opening_gap**: Sharpe=-0.229, trades=974 — low Sharpe
- **overnight_return**: Sharpe=-0.281, trades=1427 — low Sharpe
- **put_call_vix_proxy**: Sharpe=-1.588, trades=47 — low Sharpe
- **rsi_divergence**: Sharpe=-0.954, trades=266 — low Sharpe
- **short_horizon_mr**: Sharpe=-0.229, trades=851 — low Sharpe
- **short_term_mr**: Sharpe=-1.232, trades=906 — low Sharpe
- **stochastic_oversold**: Sharpe=-0.485, trades=712 — low Sharpe
- **trend_following**: Sharpe=-0.623, trades=97 — low Sharpe
- **triple_rsi**: Sharpe=-0.253, trades=495 — low Sharpe
- **volume_climax**: Sharpe=-0.279, trades=1089 — low Sharpe
- **vwap_reversion**: Sharpe=-0.527, trades=495 — low Sharpe
- **williams_percent_r**: Sharpe=-0.530, trades=568 — low Sharpe

## Method

Weights computed as w_i ∝ SR_i / σ_i (Sharpe-ratio-tilted inverse-volatility),
with Ledoit-Wolf shrinkage on the covariance matrix.
Constraints: max 25% per strategy, min 3%.

References: Bailey & López de Prado (2013), Treynor-Black theorem