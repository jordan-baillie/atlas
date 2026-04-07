# Portfolio Allocation Analysis

> Generated: 2026-04-05 08:39

## Portfolio Metrics

- **Analytic Sharpe**: -1.9308
- **Simulated Sharpe**: 0.8180
- **Strategies active**: 31
- **Average correlation**: 0.2126
- **Annual return**: 1.7%
- **Annual volatility**: 2.1%
- **Max drawdown**: -2.8%

## Optimal Weights (Sharpe-tilted inverse-vol)

| Strategy | Weight | Sharpe | Trades | CAGR% | Group |
|----------|--------|--------|--------|-------|-------|
| connors_rsi2 | 3.2% | -0.094 | 916 | 4.0% | mean_reversion |
| demark_sequential | 3.2% | -0.521 | 223 | 2.6% | mean_reversion |
| bb_squeeze | 3.2% | -0.410 | 191 | 3.2% | mean_reversion |
| donchian_breakout | 3.2% | -0.482 | 98 | 2.4% | momentum |
| gap_and_go | 3.2% | -0.995 | 254 | 1.8% | momentum |
| consecutive_down_days | 3.2% | -1.574 | 793 | -0.9% | mean_reversion |
| adx_trend_pullback | 3.2% | -0.721 | 285 | 1.4% | momentum |
| inside_bar_nr7 | 3.2% | -0.935 | 312 | 1.4% | other |
| lower_band_reversion | 3.2% | -1.433 | 462 | 0.6% | mean_reversion |
| macd_divergence | 3.2% | -0.753 | 188 | 2.2% | momentum |
| keltner_reversion | 3.2% | -1.378 | 233 | 0.0% | mean_reversion |
| mean_reversion | 3.2% | -0.056 | 250 | 4.4% | mean_reversion |
| monthly_rotation | 3.2% | -0.852 | 43 | 2.5% | other |
| momentum_breakout | 3.2% | -0.787 | 129 | 0.1% | momentum |
| opening_gap | 3.2% | -0.456 | 501 | 1.9% | other |
| dividend_capture | 3.2% | -0.905 | 113 | 2.6% | other |
| pead_earnings_drift | 3.2% | -0.749 | 82 | 2.1% | other |
| put_call_vix_proxy | 3.2% | -3.878 | 25 | 0.2% | other |
| relative_strength_pullback | 3.2% | -1.223 | 255 | -1.2% | momentum |
| overnight_return | 3.2% | -0.618 | 544 | 2.4% | other |
| sector_rotation | 3.2% | -0.178 | 288 | 3.7% | other |
| short_term_mr | 3.2% | -0.525 | 988 | 1.4% | mean_reversion |
| stochastic_oversold | 3.2% | -0.791 | 355 | 2.3% | mean_reversion |
| trend_following | 3.2% | -0.339 | 125 | 3.1% | momentum |
| rsi_divergence | 3.2% | -2.081 | 134 | -0.9% | mean_reversion |
| volume_climax | 3.2% | -1.332 | 582 | -0.5% | mean_reversion |
| williams_percent_r | 3.2% | -0.582 | 282 | 2.3% | mean_reversion |
| triple_rsi | 3.2% | -1.348 | 239 | 0.4% | other |
| vwap_reversion | 3.2% | -1.139 | 244 | 1.1% | other |
| mtf_momentum | 3.2% | -1.538 | 35 | 0.5% | other |
| heikin_ashi_reversal | 3.2% | -0.529 | 266 | 2.9% | momentum |

## Excluded Strategies (weight = 0)


## Method

Weights computed as w_i ∝ SR_i / σ_i (Sharpe-ratio-tilted inverse-volatility),
with Ledoit-Wolf shrinkage on the covariance matrix.
Constraints: max 25% per strategy, min 3%.

References: Bailey & López de Prado (2013), Treynor-Black theorem