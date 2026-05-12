# Regime Performance Report — 2026-04-28

## Data Quality

- **FRED health:** WARN: Credit OAS (BAMLC0A0CM)
- **regime_history rows last 90d:** 60 / 90 (expected ≤90)
- **Latest regime_history row:** 2026-04-27
- **Credit feature populated last 90d:** 60/60
- **Yield Curve feature populated last 90d:** 60/60
- **Trend feature populated last 90d:** 60/60
- **Risk feature populated last 90d:** 60/60

Coverage: 93% of last-90-day trades tagged with regime (41/44)

## By Strategy × Regime

| Strategy | Universe | Regime | Trades | WinRate | AvgR | TotalPnL | Sharpe |
|----------|----------|--------|--------|---------|------|----------|--------|
| connors_rsi2 | commodity_etfs | recovery_early | 3 | 33% | +0.63% | $+11.87 | n/a (<10) |
| connors_rsi2 | sp500 | bull_risk_on | 2 | 50% | +4.75% | $+45.76 | n/a (<10) |
| connors_rsi2 | sp500 | transition_uncertain | 4 | 100% | +1.59% | $+43.28 | n/a (<10) |
| mean_reversion | sp500 | transition_uncertain | 5 | 100% | +4.80% | $+170.09 | n/a (<10) |
| momentum_breakout | commodity_etfs | recovery_early | 1 | 0% | -1.30% | $-5.60 | n/a (<10) |
| momentum_breakout | sector_etfs | recovery_early | 1 | 0% | +0.00% | $+0.00 | n/a (<10) |
| momentum_breakout | sp500 | recovery_early | 6 | 17% | -0.50% | $-30.01 | n/a (<10) |
| momentum_breakout | sp500 | transition_uncertain | 4 | 100% | +5.50% | $+93.13 | n/a (<10) |
| opening_gap | sp500 | bull_risk_on | 2 | 100% | +0.53% | $+5.35 | n/a (<10) |
| opening_gap | sp500 | transition_uncertain | 1 | 0% | -0.95% | $-4.18 | n/a (<10) |
| reconciled | sp500 | recovery_early | 1 | 0% | -0.86% | $-2.09 | n/a (<10) |
| sector_rotation | sp500 | transition_uncertain | 4 | 50% | +0.41% | $-20.56 | n/a (<10) |
| short_term_mr | sp500 | transition_uncertain | 2 | 50% | -1.24% | $-1.29 | n/a (<10) |
| trend_following | sp500 | bull_risk_on | 1 | 0% | -5.67% | $-19.54 | n/a (<10) |
| trend_following | sp500 | transition_uncertain | 2 | 50% | -4.37% | $-14.94 | n/a (<10) |

## Regime Coverage Summary

| Regime State | Total Trades | Included in Report |
|-------------|--------------|-------------------|
| bear_risk_off | 2 | no (<5 trades) |
| bull_risk_on | 5 | yes |
| recovery_early | 12 | yes |
| transition_uncertain | 22 | yes |
| untagged | 3 | no (<5 trades) |

*Generated 2026-04-28T03:19:00.267034+00:00 UTC | window=90d*
