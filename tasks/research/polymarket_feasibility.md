# Polymarket as Backtesting Features — Feasibility Study

**Task:** #45  
**Date:** 2026-03-04  
**Status:** CONCLUDED — NOT VIABLE for backtesting; limited live-signal potential  

---

## Hypothesis

Polymarket prediction market probabilities on macro/economic events may carry
predictive signal for S&P 500 price movements, usable as strategy features.

## Data Landscape

### Gamma API (public, no auth)
- **`/events`** — list events with sub-markets, volume, metadata
- **`/markets`** — individual market details, current prices, volume stats

### CLOB API (public for reads)
- **`/prices-history`** — hourly-resolution price timeseries per token

### Macro-Relevant Markets Found

| Category | Active | Closed | Example |
|----------|--------|--------|---------|
| Fed rate decisions | 1 (March 2026, 4 sub-markets) | 20+ (back to 2023) | "Fed decreases rates by 50+ bps after Sep 2024" |
| Fed chair nomination | 1 (39 sub-markets) | 0 | "Will Trump nominate Kevin Warsh?" |
| Recession | 0 | 1 | "US recession in 2025?" |
| Tariffs | 0 | 2 | "Will Trump lower tariffs on China in April?" |
| S&P 500 daily | ~2/day (very short-lived) | many | "S&P 500 opens up or down on March 3?" |
| Geopolitical (indirect) | ~5 | many | "Iran close Strait of Hormuz by March 31?" |

**Total macro-relevant events:** ~23 closed, 2 active

### Coverage: S&P 500, GDP, CPI, Unemployment, VIX
Zero markets found for: S&P 500 level/range, GDP, CPI, unemployment rate,
VIX level, earnings, treasury yields, dollar index, sector rotation.

Searched: "S&P", "stock market", "recession", "GDP", "inflation", "CPI",
"tariff", "interest rate", "unemployment", "treasury", "VIX", "crash",
"bear market", "bull market", "earnings", "economy", "trade war", "sanctions",
"dollar", "government shutdown"

## Critical Finding: No Historical Data for Resolved Markets

**The CLOB `prices-history` endpoint returns empty arrays for closed/resolved markets.**

| Market | Status | CLOB History |
|--------|--------|-------------|
| Fed March 2026 "no change" | Active | ✅ 203 points, 28 days |
| Fed Chair "Warsh" | Active | ✅ 300 points, 210 days (7 months) |
| Fed Jan 2026 "no change" | Closed | ❌ 0 points |
| Fed Sept 2024 "50bp cut" | Closed | ❌ 0 points |
| "No Fed cuts in 2025" | Closed | ❌ 0 points |
| Recession 2025 | Closed | ❌ 0 points |

**This is the kill shot for backtesting.** You cannot construct a historical
feature from odds that no longer exist in the API. The 21 closed Fed decision
markets spanning 2023–2026 would have been perfect training data, but their
price histories are gone.

### Max Available History (Active Markets Only)

- **Fed Chair nomination:** ~7 months (Aug 2025 → present)
- **Fed March 2026 meeting:** ~1 month (Feb 2026 → present)
- **S&P 500 daily up/down:** 1 day per market (useless for features)

## Assessment

### For Backtesting: ❌ NOT VIABLE

1. **No historical data** — resolved markets have zero price history
2. **Sparse coverage** — only Fed rate decisions are consistently tracked; no GDP, CPI, unemployment, VIX, or S&P level markets
3. **Short-lived markets** — S&P 500 daily markets expire same day
4. **Insufficient depth** — even if we started ingesting today, we'd need 12+ months to have enough data points aligned with SP500 returns
5. **No alternative source** — Gamma API has only `/markets` and `/events` endpoints; no archival timeseries

### For Live Signal: ⚠️ MARGINAL

Could theoretically use current Fed rate odds as a live feature:
- "If P(rate cut) surges 20% in 24h → increase defensiveness"
- "If P(no change) drops below 80% → widen stops"

**Problems:**
- Can't backtest this → flying blind
- Fed meetings are 8x/year → signal fires rarely
- Polymarket odds move AFTER news, not before it (lagging indicator)
- Better alternatives exist: CME FedWatch (same data, much deeper history, free)

### CME FedWatch: The Better Alternative

CME FedWatch tool provides Fed rate probabilities derived from Fed Funds futures:
- **History:** decades of Fed Funds futures data
- **Resolution:** daily
- **Coverage:** every FOMC meeting, plus forward curve
- **Source:** actual money at risk (much larger than Polymarket)
- **Access:** free via CME website, programmatic via FRED

**If we want "market-implied Fed rate expectations" as a feature, use FRED
series DFF (Fed Funds rate) or DFEDTAR, not Polymarket.**

## Conclusion

Polymarket is not useful as a backtesting feature source for Atlas because:
1. Historical price data is purged for resolved markets (no backtest possible)
2. Market coverage of economic indicators is extremely sparse
3. S&P 500 markets are too short-lived (1 day) to serve as features
4. Better alternatives exist (CME FedWatch / FRED) for the one relevant category (Fed rates)

**Recommendation:** Close task #45. If macro features are desired, explore
FRED economic data (see skill `fred-economic-data`) which has 800K+ series
with decades of history — infinitely more suitable for backtesting.

## Artifacts

- Research date: 2026-03-04
- API tested: Gamma API (gamma-api.polymarket.com), CLOB API (clob.polymarket.com)
- Markets surveyed: ~500 events, 15 keyword searches
- Time spent: ~30 minutes
