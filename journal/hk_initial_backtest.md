# HK Initial Backtest — 2026-03-02

## Summary
First backtest of Hong Kong (SEHK) market with 3 enabled strategies on 120-ticker Hang Seng Composite universe.

## Key Metrics
| Metric | Value |
|--------|-------|
| Total Trades | 58 |
| Win Rate | 56.9% |
| Sharpe Ratio | 0.818 |
| CAGR | 6.7% |
| Max Drawdown | 2.7% |
| Profit Factor | 2.36 |
| Avg Trade | HK$72.12 |
| Final Equity | HK$34,068 (from HK$30,000) |

## Strategies Active
- **mean_reversion** — enabled
- **trend_following** — enabled
- **opening_gap** — enabled

## Data
- 129/130 tickers downloaded (3799.HK / Dali Foods failed — likely delisted)
- 120 tickers passed universe liquidity filters
- 3 years history (2023-03-03 to 2026-03-02)

## Assessment
- **Sharpe 0.82** is strong for an unoptimized first run
- **Max DD 2.7%** is very conservative — room to increase position sizing
- **Profit factor 2.36** indicates good edge
- Ready for parameter optimization via reoptimize pipeline

## Next Steps
1. Run parallel coordinate descent optimization (reoptimize_parallel.py -m hk)
2. Validate OOS performance
3. Enable live paper trading once IBKR HK gateway is connected
