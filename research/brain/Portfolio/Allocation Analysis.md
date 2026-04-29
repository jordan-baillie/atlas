# Portfolio Allocation Analysis

> Generated: 2026-04-26 08:46

## Portfolio Metrics

- **Analytic Sharpe**: 0.3583
- **Simulated Sharpe**: 1.0452
- **Strategies active**: 5
- **Average correlation**: 0.3131
- **Annual return**: 7.9%
- **Annual volatility**: 7.5%
- **Max drawdown**: -8.9%

## Optimal Weights (Sharpe-tilted inverse-vol)

| Strategy | Weight | Sharpe | Trades | CAGR% | Group |
|----------|--------|--------|--------|-------|-------|
| momentum_breakout | 25.0% | 0.137 | 185 | 5.8% | momentum |
| pead_earnings_drift | 25.0% | 0.631 | 154 | 9.5% | other |
| sector_rotation | 25.0% | 0.325 | 1055 | 9.6% | other |
| volume_climax | 15.2% | 0.063 | 1106 | 5.2% | mean_reversion |
| mean_reversion | 9.8% | 0.047 | 273 | 5.1% | mean_reversion |

## Excluded Strategies (weight = 0)

- **adx_trend_pullback**: Sharpe=-0.230, trades=572 — low Sharpe
- **bb_squeeze**: Sharpe=-0.300, trades=387 — low Sharpe
- **connors_rsi2**: Sharpe=-0.012, trades=923 — low Sharpe
- **consecutive_down_days**: Sharpe=-0.343, trades=1488 — low Sharpe
- **demark_sequential**: Sharpe=-0.232, trades=440 — low Sharpe
- **dividend_capture**: Sharpe=-0.908, trades=240 — low Sharpe
- **donchian_breakout**: Sharpe=-0.364, trades=207 — low Sharpe
- **gap_and_go**: Sharpe=-0.370, trades=445 — low Sharpe
- **heikin_ashi_reversal**: Sharpe=-0.377, trades=523 — low Sharpe
- **inside_bar_nr7**: Sharpe=-0.816, trades=620 — low Sharpe
- **keltner_reversion**: Sharpe=-0.195, trades=462 — low Sharpe
- **lower_band_reversion**: Sharpe=-0.576, trades=909 — low Sharpe
- **macd_divergence**: Sharpe=-0.354, trades=361 — low Sharpe
- **monthly_rotation**: Sharpe=-0.698, trades=88 — low Sharpe
- **mtf_momentum**: Sharpe=-0.220, trades=143 — low Sharpe
- **opening_gap**: Sharpe=-0.004, trades=878 — low Sharpe
- **overnight_return**: Sharpe=-0.621, trades=1091 — low Sharpe
- **put_call_vix_proxy**: Sharpe=-1.332, trades=49 — low Sharpe
- **relative_strength_pullback**: Sharpe=-0.038, trades=505 — low Sharpe
- **rsi_divergence**: Sharpe=-0.754, trades=262 — low Sharpe
- **short_term_mr**: Sharpe=-0.638, trades=1011 — low Sharpe
- **stochastic_oversold**: Sharpe=-0.615, trades=710 — low Sharpe
- **trend_following**: Sharpe=-0.372, trades=127 — low Sharpe
- **triple_rsi**: Sharpe=-0.680, trades=484 — low Sharpe
- **vwap_reversion**: Sharpe=-0.108, trades=486 — low Sharpe
- **williams_percent_r**: Sharpe=-0.208, trades=568 — low Sharpe

## Method

Weights computed as w_i ∝ SR_i / σ_i (Sharpe-ratio-tilted inverse-volatility),
with Ledoit-Wolf shrinkage on the covariance matrix.
Constraints: max 25% per strategy, min 3%.

References: Bailey & López de Prado (2013), Treynor-Black theorem