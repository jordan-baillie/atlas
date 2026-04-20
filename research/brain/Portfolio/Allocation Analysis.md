# Portfolio Allocation Analysis

> Generated: 2026-04-17 13:21

## Portfolio Metrics

- **Analytic Sharpe**: 0.1252
- **Simulated Sharpe**: 0.7331
- **Strategies active**: 3
- **Average correlation**: 0.3630
- **Annual return**: 6.4%
- **Annual volatility**: 8.7%
- **Max drawdown**: -13.4%

## Optimal Weights (Sharpe-tilted inverse-vol)

| Strategy | Weight | Sharpe | Trades | CAGR% | Group |
|----------|--------|--------|--------|-------|-------|
| bb_squeeze | 33.3% | 0.050 | 378 | 5.1% | mean_reversion |
| mean_reversion | 33.3% | 0.014 | 305 | 4.8% | mean_reversion |
| sector_rotation | 33.3% | 0.220 | 1048 | 7.5% | other |

## Excluded Strategies (weight = 0)

- **adx_trend_pullback**: Sharpe=-0.227, trades=575 — low Sharpe
- **connors_rsi2**: Sharpe=-0.227, trades=912 — low Sharpe
- **consecutive_down_days**: Sharpe=-0.677, trades=1572 — low Sharpe
- **demark_sequential**: Sharpe=-0.092, trades=440 — low Sharpe
- **dividend_capture**: Sharpe=-0.484, trades=241 — low Sharpe
- **donchian_breakout**: Sharpe=-0.198, trades=203 — low Sharpe
- **gap_and_go**: Sharpe=-0.223, trades=453 — low Sharpe
- **heikin_ashi_reversal**: Sharpe=-0.715, trades=520 — low Sharpe
- **inside_bar_nr7**: Sharpe=-0.747, trades=625 — low Sharpe
- **keltner_reversion**: Sharpe=-0.093, trades=462 — low Sharpe
- **lower_band_reversion**: Sharpe=-0.485, trades=900 — low Sharpe
- **macd_divergence**: Sharpe=-0.081, trades=370 — low Sharpe
- **momentum_breakout**: Sharpe=-0.357, trades=114 — low Sharpe
- **monthly_rotation**: Sharpe=-0.353, trades=88 — low Sharpe
- **mtf_momentum**: Sharpe=-0.378, trades=70 — low Sharpe
- **opening_gap**: Sharpe=-0.594, trades=508 — low Sharpe
- **overnight_return**: Sharpe=-0.544, trades=1087 — low Sharpe
- **pead_earnings_drift**: Sharpe=-0.125, trades=161 — low Sharpe
- **put_call_vix_proxy**: Sharpe=-1.404, trades=49 — low Sharpe
- **relative_strength_pullback**: Sharpe=-0.014, trades=505 — low Sharpe
- **rsi_divergence**: Sharpe=-0.905, trades=265 — low Sharpe
- **short_term_mr**: Sharpe=-0.500, trades=1007 — low Sharpe
- **stochastic_oversold**: Sharpe=-0.546, trades=722 — low Sharpe
- **trend_following**: Sharpe=-0.453, trades=122 — low Sharpe
- **triple_rsi**: Sharpe=-0.272, trades=488 — low Sharpe
- **volume_climax**: Sharpe=-0.177, trades=1104 — low Sharpe
- **vwap_reversion**: Sharpe=-0.301, trades=483 — low Sharpe
- **williams_percent_r**: Sharpe=-0.126, trades=568 — low Sharpe

## Method

Weights computed as w_i ∝ SR_i / σ_i (Sharpe-ratio-tilted inverse-volatility),
with Ledoit-Wolf shrinkage on the covariance matrix.
Constraints: max 25% per strategy, min 3%.

References: Bailey & López de Prado (2013), Treynor-Black theorem