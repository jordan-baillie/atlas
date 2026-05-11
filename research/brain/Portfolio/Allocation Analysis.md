# Portfolio Allocation Analysis

> Generated: 2026-05-10 08:40

## Portfolio Metrics

- **Analytic Sharpe**: 0.3449
- **Simulated Sharpe**: 0.9068
- **Strategies active**: 4
- **Average correlation**: 0.4287
- **Annual return**: 7.4%
- **Annual volatility**: 8.2%
- **Max drawdown**: -10.5%

## Optimal Weights (Sharpe-tilted inverse-vol)

| Strategy | Weight | Sharpe | Trades | CAGR% | Group |
|----------|--------|--------|--------|-------|-------|
| momentum_breakout | 25.0% | 0.040 | 172 | 5.0% | momentum |
| pead_earnings_drift | 25.0% | 0.679 | 158 | 9.6% | other |
| relative_strength_pullback | 25.0% | 0.086 | 512 | 5.4% | momentum |
| sector_rotation | 25.0% | 0.237 | 1073 | 7.8% | other |

## Excluded Strategies (weight = 0)

- **adx_trend_pullback**: Sharpe=-0.278, trades=577 — low Sharpe
- **bb_squeeze**: Sharpe=-0.383, trades=391 — low Sharpe
- **connors_rsi2**: Sharpe=-0.209, trades=997 — low Sharpe
- **consecutive_down_days**: Sharpe=-0.561, trades=1492 — low Sharpe
- **demark_sequential**: Sharpe=-0.420, trades=438 — low Sharpe
- **dividend_capture**: Sharpe=-0.536, trades=241 — low Sharpe
- **donchian_breakout**: Sharpe=-0.518, trades=209 — low Sharpe
- **gap_and_go**: Sharpe=-0.284, trades=447 — low Sharpe
- **heikin_ashi_reversal**: Sharpe=-0.496, trades=528 — low Sharpe
- **inside_bar_nr7**: Sharpe=-0.550, trades=623 — low Sharpe
- **keltner_reversion**: Sharpe=-0.326, trades=471 — low Sharpe
- **lower_band_reversion**: Sharpe=-0.470, trades=911 — low Sharpe
- **macd_divergence**: Sharpe=-0.178, trades=376 — low Sharpe
- **mean_reversion**: Sharpe=-0.410, trades=371 — low Sharpe
- **monthly_rotation**: Sharpe=-0.826, trades=90 — low Sharpe
- **mtf_momentum**: Sharpe=-0.474, trades=137 — low Sharpe
- **opening_gap**: Sharpe=-0.015, trades=967 — low Sharpe
- **overnight_return**: Sharpe=-0.482, trades=1085 — low Sharpe
- **put_call_vix_proxy**: Sharpe=-1.218, trades=48 — low Sharpe
- **rsi_divergence**: Sharpe=-0.826, trades=262 — low Sharpe
- **short_term_mr**: Sharpe=-1.088, trades=903 — low Sharpe
- **stochastic_oversold**: Sharpe=-0.421, trades=717 — low Sharpe
- **trend_following**: Sharpe=-0.525, trades=131 — low Sharpe
- **triple_rsi**: Sharpe=-0.532, trades=490 — low Sharpe
- **volume_climax**: Sharpe=-0.193, trades=1110 — low Sharpe
- **vwap_reversion**: Sharpe=-0.474, trades=490 — low Sharpe
- **williams_percent_r**: Sharpe=-0.179, trades=569 — low Sharpe

## Method

Weights computed as w_i ∝ SR_i / σ_i (Sharpe-ratio-tilted inverse-volatility),
with Ledoit-Wolf shrinkage on the covariance matrix.
Constraints: max 25% per strategy, min 3%.

References: Bailey & López de Prado (2013), Treynor-Black theorem