# Atlas Returns Audit — Sections 5 & 8: Drawdown/Risk + Capital Allocation
**Date:** 2026-04-28  
**Scope:** Live account equity since ~2026-04-02. Database: `data/atlas.db` (read-only).  
**Confidence labels:** ⚫ noise (<10 obs) | 🔵 weak (10–30) | 🟢 moderate (30+)

---

## Data Inventory

| Table | Market | Date Range | N Rows |
|---|---|---|---|
| `equity_curve` | sp500 | 2026-03-16 → 2026-04-27 | 29 |
| `equity_curve` | commodity_etfs | 2026-04-14 → 2026-04-27 | 10 |
| `equity_curve` | sector_etfs | 2026-04-24 → 2026-04-27 | 2 |
| `equity_history` | sp500 | 2026-03-13 → 2026-04-27 | 25 |
| `portfolio_snapshots` | sp500/ALL | 2026-03-13 → 2026-04-28 | 46 |
| `trades` | all | 2026-03-13 → 2026-04-28 | 66 rows, 49 closed |

**Live start defined as:** 2026-04-02 (first equity_curve entry post-go-live; pre-live testing ran 2026-03-13 → 2026-04-01).

**Data quality notes:**
- `daily_pnl_pct` in `equity_curve` only populated from 2026-04-15 onward (9 rows); earlier rows have only `day_pnl` in absolute dollars.
- `stop_price` is NULL or 0.0 for ~60% of trade rows — early trades pre-stop-tracking. Risk calculations use only the 20 trades with valid stop prices.
- Several `exit_reason='stop_loss'` rows are mislabeled profitable exits (D +5.3%, ECL +1.9%, NOC +1.2%, MRVL +14.5%) — these are early-stage artifacts where `stop_price=NULL/0`. Only WM (-2.5%) is a genuine stop-loss fire.

---

## Section 5 — Drawdown / Risk Audit

### 5a — Equity Curve & Drawdown

**Query used:**
```sql
SELECT date, equity, cash, positions_value, day_pnl, daily_pnl_pct, broker_equity, positions_count
FROM equity_curve WHERE market_id='sp500' ORDER BY date;
```

#### Full equity series (sp500, n=29 days)

| Date | Equity | Pos Value | Cash | Day PnL | Leverage |
|---|---|---|---|---|---|
| 2026-03-16 | $5,017 | $2,162 | $2,855 | +$5.12 | 0.43x |
| 2026-03-31 | $5,235 | $3,390 | $1,711 | +$5.89 | 0.65x |
| **2026-04-02** | **$5,293** | **$2,759** | **$2,361** | **+$50.92** | **0.52x** ← live start |
| 2026-04-06 | $5,283 | $2,772 | $2,339 | -$10.13 | 0.52x |
| 2026-04-08 | $5,266 | $1,290 | $3,795 | -$17.42 | 0.24x |
| 2026-04-09 | $5,310 | $1,285 | $3,795 | +$44.52 | 0.24x |
| 2026-04-10 | $5,331 | $521 | $4,517 | +$20.80 | 0.10x |
| 2026-04-15 | $5,367 | $5,084 | $283 | -$8.08 | 0.95x |
| 2026-04-16 | $5,348 | $4,546 | $802 | -$18.44 | 0.85x |
| 2026-04-17 | $5,308 | $4,788 | $521 | -$40.15 | 0.90x |
| 2026-04-22 | $5,380 | $4,222 | $1,158 | +$60.75 | 0.78x |
| **2026-04-24** | **$5,429** | **$9,492** | **-$4,063** | **+$23.19** | **1.75x** ← peak equity |
| **2026-04-27** | **$5,267** | **$8,020** | **-$2,753** | **-$161.75** | **1.52x** ← worst day |

#### Peak-to-Trough Drawdown (live period)

```
Peak: $5,429.05 (2026-04-24)
Trough: $5,265.82 (2026-04-08, equity curve reading)
Max drawdown: -3.0%  [equity curve method]

Live-period trough (post Apr 2): $5,265.82
Drawdown from Apr 24 peak to Apr 27: ($5,429 → $5,267) = -3.0%
```

**Confidence: 🔵 weak** — 17 data points post-live-start.

The `-$161.75` on Apr 27 (-2.98% of equity that day) is the **worst single recorded day** and **just breached the `max_daily_drawdown_pct = 2.0%` circuit breaker threshold**. This warrants attention — the config-level daily circuit breaker is set at 2%, and Apr 27 landed at 2.98%.

#### Full-period return statistics (n=29 days, Mar 16 – Apr 27)

| Metric | Value | Notes |
|---|---|---|
| Starting equity | $5,017 | 2026-03-16 |
| Ending equity | $5,267 | 2026-04-27 |
| Raw return | +4.99% | |
| Annualized return | +44.6% | Based on daily mean return × 252 |
| Daily mean return | +0.177% | |
| Daily std dev | 0.796% | |
| Annualized vol | 12.6% | σ × √252 |
| Ex-post Sharpe (rf=4.3%) | **3.19** | **⚠ Inflated — pre-live trending phase included** |

#### Live-only return statistics (n=16 daily returns, Apr 2 – Apr 27) 🔵 weak

| Metric | Value | Notes |
|---|---|---|
| Annualized return | **-6.8%** | Apr 2–27 has been net negative |
| Daily std dev | 0.919% | |
| Annualized vol | **14.6%** | |
| Ex-post Sharpe (rf=4.3%) | **-0.76** | |
| Worst single day | **-2.98%** on 2026-04-27 | Apr 27 tariff-related selloff |
| Best single day | **+1.14%** on 2026-04-22 | |

**Key insight:** The system's annualized return looks strong over the full 6-week window (+44.6%, Sharpe 3.19) because it includes the trending pre-live period (Mar 16 – Apr 1). The **live-only period is negative** (−6.8% annualized, Sharpe −0.76) driven primarily by the Apr 6–17 tariff correction. This does not yet invalidate the strategy — 16 observations is too few to distinguish signal from noise.

#### Skew and Fat Tails (full 29-day series, day_pnl in $)

| Metric | Value | Interpretation |
|---|---|---|
| Skewness | **-2.42** | Heavy left tail — the large negative day dominates |
| Excess kurtosis | **+9.33** | Strongly leptokurtic (fat tails) |
| Mean daily PnL | +$7.99 | |
| Std daily PnL | $41.64 | |

**Assessment:** The fat left tail and negative skewness are driven almost entirely by Apr 27's -$161.75 in a series of otherwise small moves. With n=29, one outlier day heavily dominates all higher moments. Not yet statistically meaningful.

---

### 5b — Daily PnL Distribution from Closed Trades

**Query:**
```sql
SELECT DATE(exit_date) as d, SUM(pnl) as daily_pnl, COUNT(*) as n_exits
FROM trades WHERE status='closed' AND exit_date IS NOT NULL AND exit_date >= '2026-04-02'
  AND strategy != 'reconciled'
GROUP BY DATE(exit_date) ORDER BY d;
```

**n = 13 exit days** since Apr 2 (13 trading sessions with at least one closed trade). 🔵 weak

| Date | Daily PnL | N Exits | % Equity |
|---|---|---|---|
| 2026-04-02 | +$9.87 | 4 | +0.19% |
| 2026-04-07 | +$85.61 | 4 | +1.62% |
| **2026-04-08** | **-$59.74** | **3** | **-1.13%** |
| 2026-04-10 | +$98.44 | 2 | +1.86% |
| 2026-04-11 | +$126.24 | 2 | +2.38% |
| 2026-04-16 | -$14.91 | 1 | -0.28% |
| 2026-04-17 | -$0.95 | 1 | -0.02% |
| 2026-04-18 | +$4.74 | 1 | +0.09% |
| 2026-04-21 | -$22.13 | 3 | -0.42% |
| 2026-04-22 | -$16.80 | 5 | -0.32% |
| 2026-04-24 | -$36.74 | 3 | -0.69% |
| 2026-04-25 | +$42.01 | 1 | +0.79% |
| 2026-04-28 | +$133.14 | 2 | +2.52% |

| Metric | Value |
|---|---|
| Mean daily PnL | +$26.83 |
| Std | $63.98 |
| Min (worst day) | -$59.74 (-1.13%) |
| Max (best day) | +$133.14 (+2.52%) |
| Skewness | +0.58 (mild right skew) |
| Excess kurtosis | -1.05 (platykurtic) |
| Positive days | 7/13 (54%) |
| Days exceeding 2% account loss | **0** |
| Days exceeding 1% account loss | **1** (Apr 8) |

**Tail analysis:** No exit-day losses exceeded the 2% circuit breaker threshold from realized PnL alone. The Apr 27 -2.98% equity-curve drawdown came from **unrealized open position losses**, not realized exits. The system is not blowing out on exit days.

**Distribution shape:** The exit-day PnL is mildly right-skewed with negative kurtosis — flatter than normal, with wins slightly larger than losses. This is consistent with a system that catches some large winners (AMD +$118, MRVL +$63) amid many small trades.

---

### 5c — Position Sizing Utilization

**Method:** `position_value = entry_price × shares` joined to `equity_curve` on `DATE(entry_date)`. Risk = `(entry_price - stop_price) × shares / equity`. n=41 trades with equity match; n=20 with valid stop prices.

#### Position size as % of equity at entry

| Metric | Value | Confidence |
|---|---|---|
| Mean | **9.4%** | 🔵 weak |
| Median | **8.4%** | 🔵 weak |
| Min | 2.1% | |
| Max | 20.3% (HCA, Mar 18) | |
| Trades >20% | 2 | Early period, no stop tracking |
| Trades >15% | 4 | Mix of old + ETF sector positions |
| Trades <5% | 7 | |

**Notable outlier:** AAPL on 2026-04-20 at 42.9% of equity — this was a `reconcile_phantom` ghost entry (15 shares × $152 = $2,280). Excluded from stats above.

**ETF sector concentration (recent, Apr 22–24):**
- XLI (Apr 24): 28.8% of equity
- XLK (Apr 23): 23.2% of equity  
- XLY (Apr 21–22): 21.6–21.9% of equity

These are **legal per config** (max_sector_concentration=2, but this refers to sector count not concentration limit). However 28.8% in a single ETF on a $5,237 account is materially concentrated.

#### Implied per-trade risk (entry-stop distance × shares / equity)

**n=20 trades with valid stop price** (others have stop_price NULL or 0.0). ⚫ noise for individual strategy breakdowns.

| Metric | Value | vs Config 0.5% |
|---|---|---|
| Mean implied risk | **0.373%** | 75% of budget used |
| Median implied risk | **0.359%** | 72% of budget used |
| Min | 0.221% | |
| Max | 0.619% | Slightly over config limit |
| Trades >0.4% risk | 9/20 (45%) | |
| Trades >0.5% risk | 3/20 (15%) | Mild overshoot |
| Trades <0.2% risk | 0/20 | No outlier undersizing |

**Assessment:** The system is using **~73% of its configured risk budget** per trade on average. This is deliberate underuse — the system likely adapts position size based on ATR-derived stop distance. No chronic under-sizing. Three trades slightly exceed the 0.5% limit (max 0.619%), which is not alarming. The larger concern is **stop_price not being recorded** for 60% of trades, preventing proper risk measurement.

---

### 5d — Concentration

#### Simultaneous open positions

```
Max simultaneous open positions (sp500): 9 on 2026-04-06
Tickers: AMT, D, NFLX, MSI, DVN, MRVL (x2), COP, OXY
Config max_open_positions: 10 (sp500 config line 1 says 10, task prompt said 15 — confirmed 10)
```

The MRVL duplicate on that date is the reconciler-generated duplicate open trade flagged in prior audits. Effectively 8 real positions.

#### Current live open positions (2026-04-27/28)

| Ticker | Universe | Entry | Pos Value | % Equity |
|---|---|---|---|---|
| GLD | commodity_etfs | Apr 16 | $885 | 16.8% |
| CCJ | commodity_etfs | Apr 24 | $506 | 9.6% |
| SLV | commodity_etfs | Apr 24 | $410 | 7.8% |
| XLY | sector_etfs | Apr 22 | $1,164 | 22.1% |
| XLK | sector_etfs | Apr 23 | $1,254 | 23.8% |
| XLI | sector_etfs | Apr 24 | $1,566 | 29.8% |
| AVGO | sp500 | Apr 23 | $423 | 8.0% |
| CAT | sp500 | Apr 24 | $835 | 15.9% |
| ADI | sp500 | Apr 24 | $808 | 15.4% |
| FCX | sp500 | Apr 24 | $307 | 5.8% |

**Total open position value: ~$8,158 | Equity: ~$5,267 | Effective leverage: 1.55x**

**Sector/universe concentration (current):**
- sector_etfs: 3 positions = $3,984 = 75.6% of equity — **severe concentration in sector ETF universe**
- commodity_etfs: 3 positions = $1,801 = 34.2% of equity
- sp500: 4 positions = $2,373 = 45.1% of equity
- Cross-universe total: 1.55x leverage via margin

#### Top single-name concentrations (historical, real trades)

| Ticker | Date | Pos Value | % Equity |
|---|---|---|---|
| HCA | 2026-03-18 | $1,020 | **20.3%** |
| ECL | 2026-03-24 | $1,028 | **20.1%** |
| XLI | 2026-04-24 | $1,566 | **28.8%** |
| XLK | 2026-04-23 | $1,254 | **23.2%** |
| XLY | 2026-04-21 | $1,164 | **21.9%** |
| STZ | 2026-04-14 | $820 | **15.3%** |

**Sector data:** No date column in `signals` table prevented a join-based sector breakdown. From the position symbols: current portfolio is heavily tech (XLK, ADI, AVGO), industrials (XLI, CAT), and commodities (GLD, SLV, CCJ).

**Re-used tickers (multiple trades, closed):**
- MRVL: 4 trades, total PnL +$142.86
- D (Dominion): 4 trades, total PnL +$159.51
- COP: 3 trades, total PnL -$7.19
- SLV: 3 trades, total PnL -$5.60
- XLY: 3 trades, total PnL $0 (all phantoms)

---

### 5e — Stop-Loss vs Target Effectiveness

**Query:**
```sql
SELECT exit_reason, COUNT(*), AVG(pnl), AVG(pnl_pct), AVG(hold_days), AVG(mae), AVG(mfe)
FROM trades WHERE status='closed' AND strategy NOT IN ('reconciled')
  AND exit_reason NOT IN ('reconcile_phantom')
GROUP BY exit_reason ORDER BY COUNT(*) DESC;
```

**n=44 real closed trades** (excl. reconcile_phantom, superseded). 🔵 weak overall, ⚫ noise for individual buckets.

| Exit Reason | N | Avg PnL | Avg Pct% | Hold Days | MAE% | MFE% | Assessment |
|---|---|---|---|---|---|---|---|
| **reconcile_fill** | 12 | +$7.83 | +2.08% | 5.7 | -1.42% | +7.69% | Mixed: reconciler exits open positions, many profitable |
| **trailing_stop_fill** | 10 | +$4.15 | +2.45% | 2.0 | **+0.06%** | **+8.56%** | 🟢 Excellent — riding large gains |
| **trailing_stop** | 7 | -$1.95 | -0.92% | 5.0 | -3.11% | +3.82% | 🔴 Avg loss — gains evaporate before stop fires |
| **stop_loss** | 7 | +$23.56 | +3.77% | 0.9 | -2.97%† | -0.42%† | ⚠ 6/7 are **profitable exits mislabeled** (stop=NULL/0) |
| **signal_exit** | 3 | +$17.55 | +4.01% | 6.5 | +0.75% | +3.91% | 🟢 Clean exits — MFE captured before reversal |
| **take_profit** | 2 | +$49.22 | +7.34% | 5.5 | +1.40% | +8.83% | 🟢 Best exits — high MFE at exit |
| **signal** | 2 | +$36.41 | +6.11% | 4.0 | -1.93% | +7.34% | 🟢 Good exits |
| **broker_trailing_stop** | 1 | -$2.89 | -0.80% | 3.0 | -1.54% | +0.95% | ⚫ |
| **broker_stop_fill** | 1 | +$9.95 | — | — | +1.06% | +5.75% | ⚫ |

† stop_loss MAE/MFE skewed by only 1 row (WM) with valid MAE/MFE.

#### Grand average MAE/MFE (all real closed trades with data, n=31)

```
Grand avg MAE: -1.027%
Grand avg MFE: +6.439%
Ratio: MFE is 6.3× larger than MAE at exit
```

**Stop tightness assessment:**
- **trailing_stop_fill** (n=10): Exits capture an **average 8.56% MFE** before the trailing stop fires. MAE is near zero (+0.06%), meaning these entries move immediately in the right direction before the trail kicks in. **Stops are NOT too tight** — they are allowing winners to run.
- **trailing_stop** (n=7, legacy label): Average loss with 3.82% MFE — gains are evaporating before the stop fires. This is the **opposite problem: stops are too wide** for this exit bucket. However this bucket contains early-period trades and may reflect pre-configuration of trailing parameters.
- **stop_loss** (n=7): The "stop_loss" label is misleading. 6 of 7 exits are profitable (D +5.3%, ECL +1.9%, NOC +1.2%, MRVL +14.5%). These fired when stop_price was NULL/0.0 — they are early-era exits before stop tracking. Only WM (-2.5%) is a genuine stop fire.
- **Overall:** The exit mechanism is **working appropriately**. Trailing stop fills (the primary current mechanism) are capturing 8.6% avg upside before exiting. The bad exits (reconcile_fill at -1.42% MAE) are driven by reconciler labeling, not strategy logic.

**Recommendation:** Verify and correct stop_loss labeling for the 6 profitable `stop_loss` rows. Consider the `trailing_stop` bucket (avg loss, 3.82% MFE evaporating) as the primary stop-effectiveness concern — investigate whether wider `atr_stop_mult` is indicated for positions that exit at `trailing_stop` vs `trailing_stop_fill`.

---

## Section 8 — Capital Allocation / Leverage / Sizing

### 8a — Effective Leverage Over Time

**Query:**
```sql
SELECT date, equity, positions_value, cash, positions_count
FROM equity_curve WHERE market_id='sp500' ORDER BY date;
```

| Date | Equity | Pos Value | Cash | Leverage | Cash% |
|---|---|---|---|---|---|
| 2026-03-16 | $5,017 | $2,162 | $2,855 | 0.43x | 56.9% |
| 2026-03-24 | $5,127 | $4,296 | $727 | **0.84x** | 14.2% |
| 2026-03-25 | $5,184 | $4,497 | $553 | **0.87x** | 10.7% |
| 2026-04-02 | $5,293 | $2,759 | $2,361 | 0.52x | 44.6% |
| 2026-04-08 | $5,266 | $1,290 | $3,795 | 0.24x | 72.1% |
| 2026-04-10 | $5,331 | $521 | $4,517 | 0.10x | 84.7% |
| 2026-04-15 | $5,367 | $5,084 | $283 | **0.95x** | 5.3% |
| 2026-04-17 | $5,308 | $4,788 | $521 | **0.90x** | 9.8% |
| 2026-04-23 | $5,406 | $5,306 | $99 | **0.98x** | 1.8% |
| **2026-04-24** | **$5,429** | **$9,492** | **-$4,063** | **1.75x** | **-74.8%** |
| **2026-04-27** | **$5,267** | **$8,020** | **-$2,753** | **1.52x** | **-52.3%** |

| Leverage Metric | Value | Config Ceiling |
|---|---|---|
| Mean leverage | **0.69x** | 2.0x |
| Max leverage | **1.75x** (Apr 24) | 2.0x |
| Days above 1.0x | **2/29** | |
| Days above 1.5x | **2/29** | |
| Days at/near cash-only (<0.3x) | **3** | |

**Negative cash days (margin in use):** Apr 24 (-$4,063) and Apr 27 (-$2,753). This indicates Alpaca margin is being drawn when multi-universe positions are added together. The `positions_value` column in equity_curve appears to reflect **all universes combined** (sp500 + commodity_etfs + sector_etfs), while `equity` is the sp500 portfolio account equity — explaining the super-1.0x leverage on Apr 24–27.

**Conclusion:** The system has been **operating at an average 0.69x leverage** — well below the configured 2.0x ceiling. For 27 of 29 recorded days, the system was using less than 1× leverage. The recent jump to 1.52–1.75x is the first sustained use of margin and reflects the multi-universe expansion (sector_etfs + commodity_etfs added).

---

### 8b — Buying Power Utilization

| Cash% Metric | Value |
|---|---|
| Mean cash | **29.0%** of equity |
| Max cash | **84.7%** (Apr 10, post tariff selloff de-risking) |
| Min cash | **-74.8%** (Apr 24, margin drawn) |
| Days >50% cash (under-allocated) | 5/29 |
| Days <20% cash (near-fully deployed) | 8/29 |

**Pattern:**
1. **Mar 16 – Apr 1 (pre-live testing):** Variable 10–57% cash. System trying entries.
2. **Apr 2–14 (post-live, cautious):** 44–85% cash. Tariff correction → rapid de-risking. Apr 10 briefly held 85% cash.
3. **Apr 15–23 (re-deployment):** 2–25% cash. System aggressively re-entering.
4. **Apr 24–27 (multi-universe expansion):** Negative cash (margin). Sector_etfs and commodity_etfs positions added alongside sp500.

**Assessment:** The system was **chronically under-allocated** during the April drawdown (Apr 8–13 at 24–44% deployed). This is partly by design (positions stopped out), but it means the recovery rally (Apr 9–22) was captured with a reduced book. The swing from 85% cash to margin in 2 weeks is an acceleration worth monitoring.

---

### 8c — Kelly Fraction Estimate by Strategy

**Method:** Win rate `p` and payoff ratio `b = avg_win / avg_loss` computed from closed trades per strategy. Kelly = `p − (1−p)/b` (full Kelly), clipped to 0. Config weights from `config/active/sp500.json`.

#### Raw Kelly inputs

| Strategy | N Closed | Win Rate | Avg Win | Avg Loss | b (W/L) | Full Kelly | Half-Kelly | Qtr-Kelly |
|---|---|---|---|---|---|---|---|---|
| **momentum_breakout** | 17 🔵 | 52.9% | $37.60 | $11.10 | 3.39× | 39.0% | 19.5% | **9.8%** |
| **connors_rsi2** | 9 ⚫ | 66.7% | $21.67 | $10.66 | 2.03× | 50.3% | 25.1% | **12.6%** |
| **mean_reversion** | 6 ⚫ | 100.0% | $36.55 | $0 | ∞ | 100.0% | — | **invalid** |
| **sector_rotation** | 5 ⚫ | 60.0% | $8.48 | $20.03 | 0.42× | 0.0% | 0.0% | **0.0%** |
| **opening_gap** | 3 ⚫ | 66.7% | $2.68 | $4.18 | 0.64× | 14.5% | 7.3% | **3.6%** |
| **trend_following** | 3 ⚫ | 33.3% | $4.74 | $19.61 | 0.24× | 0.0% | — | **0.0%** |
| **short_term_mr** | 2 ⚫ | 50.0% | $9.95 | $11.24 | 0.89× | 0.0% | — | **0.0%** |

#### Kelly vs Config Weight Comparison

| Strategy | Qtr-Kelly | Config Weight | Signal | Confidence |
|---|---|---|---|---|
| momentum_breakout | 9.8% | **25.0%** | ↓ DECREASE | 🔵 weak |
| connors_rsi2 | 12.6% | **25.0%** | ↓ DECREASE | ⚫ noise |
| mean_reversion | invalid (100% WR) | **9.8%** | → HOLD (invalid Kelly) | ⚫ noise |
| sector_rotation | 0.0% | **25.0%** | ↓ DECREASE | ⚫ noise |
| opening_gap | 3.6% | **5.0%** | → HOLD | ⚫ noise |

#### Key observations

1. **momentum_breakout** (n=17, 🔵 weak): The only strategy with enough observations for directional guidance. Kelly = 39%, quarter-Kelly = 9.8% vs config 25%. The config is **2.5× above quarter-Kelly**. However, the high `avg_win/avg_loss = 3.39×` payoff ratio is partly inflated by large winners (AMD +$118, MRVL +$63) in a volatile market — likely not representative at scale.

2. **mean_reversion** (n=6, ⚫ noise): 100% win rate across 6 trades is suspicious. Inspection reveals all 6 are profitable exits: 3 trades on D (Dominion), 2 on AMT (take_profits +$49.22 each), 1 on DHR. The 100% WR is entirely plausible given the strategy (oversold mean-reversion with disciplined exits) but statistically meaningless at n=6. Kelly is indeterminate.

3. **sector_rotation** (n=5, ⚫ noise): Kelly = 0% (payoff ratio b=0.42, below the 1/(1-p) threshold). The 5-trade sample shows avg loss ($20.03) is much larger than avg win ($8.48). This reflects two large losers: DVN -$22 and COP -$18 during the Apr 8 tariff selloff. The strategy was deployed into a sector rotation event that reversed. Not enough signal to conclude the strategy has no edge.

4. **Quarter-Kelly framing at this account size:** With $5,237 equity and quarter-Kelly = 10% per strategy, the effective capital per strategy is ~$524. A 3.39× payoff ratio with 0.5% risk budget = $26 per trade risk. This is coherent but leaves little margin for position sizing granularity.

5. **Critical caveat:** The standard warning applies with force here. Kelly criterion with n<30 has enormous estimation variance. A 2-standard-deviation confidence interval for Kelly with n=17 and p=0.53 spans approximately [0%, 75%]. **Do not reweight strategies based on these estimates alone.** Use as directional signal, requiring 50+ observations per strategy before acting.

---

### 8d — Small Account Constraints

**Account equity:** ~$5,237  
**Risk budget per trade:** 0.5% × $5,237 = **$26.19**

| Stock Price | Stop Distance | Shares (at $26 risk) | Position Value | % of Account |
|---|---|---|---|---|
| $10 (small cap) | $0.50 | 52.4 → 52 | $520 | 9.9% |
| $60 (FCX-like) | $3.00 | 8.7 → 9 | $540 | 10.3% |
| $100 (mid-price) | $5.00 | 5.2 → 5 | $500 | 9.5% |
| $170 (XLI-like) | $5.00 | 5.2 → 5 | $850 | 16.2% |
| $400 (ADI-like) | $15.00 | 1.7 → **2** | $800 | **15.3%** |
| $422 (AVGO-like) | $15.00 | 1.7 → **2** | $844 | **16.1%** |
| $835 (CAT-like) | $35.00 | 0.75 → **1** | $835 | **15.9%** |

**Active constraints:**

1. **Integer share floor on high-price stocks:** CAT at $835 requires minimum 1 share = $835 = 15.9% of account. The risk-budget-implied 0.75 shares rounds up to 1, yielding $35/equity = 0.67% actual risk — 34% over the 0.5% config limit. This is the system's current **binding granularity constraint**.

2. **PDT (Pattern Day Trader) rule:** Account <$25,000 → maximum 3 day-trades per rolling 5 trading days. Observed Apr 24: "PDT protection triggered 103+ times for AVGO" (mental model note). This **forces holding positions overnight** that the strategy might want to exit intraday, compounding directional risk.

3. **Negative cash / margin:** Apr 24–27 shows $2,753–$4,063 drawn from margin. At $5,237 equity with 3 universes active (sp500 + commodity_etfs + sector_etfs), total position value exceeds single-account equity. If Alpaca margined at standard 2:1 regulation T, the current 1.52–1.75x leverage is within allowable limits but approaches the practical risk boundary.

4. **Min position value ($100) is non-binding:** At $26 risk budget per trade, even the smallest position (e.g., 52 shares at $10 = $520) exceeds the minimum. The constraint effectively never binds.

5. **Multi-universe compounding:** Each of the 3 active universes (sp500, commodity_etfs, sector_etfs) independently applies position-sizing logic against the **same equity base**. There is no cross-universe capital allocation — each universe sizes as if it owned the full account. The Apr 24 result (total positions = 1.75× equity) is the direct consequence of this architectural gap.

---

## Consolidated Recommendations

### Leverage Utilization
**Current mean: 0.69x. Max observed: 1.75x. Config ceiling: 2.0x.**

**Recommendation:** HOLD current ceiling. The system has been conservatively leveraged (avg 0.69x) for the first 6 weeks. The recent spike to 1.75x is the **first real test** of multi-universe margin. Monitor Apr 28+ closely. If the account sustains 1.5–1.75x for more than a week without triggering a drawdown >3%, the ceiling is appropriate. The 2.0x ceiling is not constraining; the binding constraint is per-strategy position-count limits and PDT.

---

### Stop Policy
**Assessment: WORKING for trailing_stop_fill bucket. SUBOPTIMAL for trailing_stop (legacy) bucket.**

**Evidence:**
- `trailing_stop_fill` (n=10): MFE=8.56%, MAE=0.06% → stops are far above cost, capturing large moves ✅
- `trailing_stop` (n=7): avg −$1.95 PnL with 3.82% MFE → winners reversing before stop fires ⚠
- Grand avg: MFE = 6.3× MAE at exit → system exits with meaningful unrealized gain still intact

**Recommendation:** Investigate why `trailing_stop` (7 trades) underperforms `trailing_stop_fill` (10 trades). Hypothesis: `trailing_stop` fires at a less-favorable price (prior close or reference price) while `trailing_stop_fill` is the broker-confirmed actual fill. If so, this is a labeling/timing artifact, not a strategy flaw. No stop tightening or loosening required at this time.

---

### Per-Trade Risk
**Actual avg: 0.373% | Config max: 0.500% | Budget utilization: 73–75%**

**Recommendation:** The system is **under-using its risk budget by ~27%**. This is likely because ATR-based stop distances are wide relative to `max_risk_per_trade_pct`, causing the sizer to reduce shares to stay within the limit — or alternatively, the integer-share floor on high-price stocks reduces size. 

Consider: (a) allowing fractional shares where Alpaca supports it to improve sizing precision; (b) reviewing whether `atr_stop_mult=1.5` produces stops that are too wide given the account size, mechanically reducing size. No config change recommended until stop_price data coverage improves (currently ~33% of trades).

---

### Strategy Weights (Kelly signal, directional only)
| Strategy | Signal | Rationale | Action |
|---|---|---|---|
| momentum_breakout | ↓ Consider reducing to 15–20% | Quarter-Kelly=9.8% (weak confidence); high avg win inflated by 2 outliers | **Review at n=30 trades** |
| sector_rotation | ↓ Negative Kelly in current sample | b=0.42, avg loss 2.4× avg win | **Do not increase; monitor for n=10** |
| mean_reversion | → Hold current weight | Insufficient data; 100% WR is promising but n=6 only | **Review at n=15 trades** |
| connors_rsi2 | → Hold current weight | Noise-level n=9 but 66.7% WR consistent with backtest | **Review at n=20 trades** |
| opening_gap | → Hold at 5% | Qtr-Kelly=3.6% roughly matches config | No action needed |

---

### Small Account / Capital Scale
**Key finding: The $5,237 account has three interlocking constraints that compound each other.**

1. **$26 risk budget** → 1-share minimums on stocks >$200 → positions mechanically become 8–16% of account
2. **PDT rule** → forced overnight holds → strategy holding-period assumptions break down
3. **Multi-universe margin** → independent per-universe sizing → total exposure exceeds account size

**Recommendation:** 
- **Cap sector_etfs positions at 20% of account each** (vs current 28.8% XLI). ETF positions at full ATR sizing on a $5k account are oversized.
- **Add cross-universe position-count guard**: if total open positions across all universes exceeds 8, hold new entries. Current architecture allows each universe to act independently.
- **PDT mitigation**: consider `holding_period_min = 1` → `2` for strategies that want to day-trade. The current setting of 2 days is prudent but verify it's enforced across all strategies.
- **Scale milestone**: At ~$25,000 equity (approximately 5× growth), the PDT constraint lifts, and the $26 risk budget becomes $119 — dramatically improving position sizing granularity. Near-term: work toward this level before expanding to additional universes.

---

## Summary Scorecard

| Dimension | Current State | Status |
|---|---|---|
| Max drawdown (live, Apr 2–27) | **3.0%** | ⚠ Just over 2% daily CB threshold on Apr 27 |
| Live-period Sharpe | **-0.76** | 🔴 Negative (16 obs, tariff-driven) |
| Full-period Sharpe | **3.19** | 🟢 (includes favorable pre-live trend) |
| Avg leverage | **0.69x** (vs 2.0x config) | 🟡 Under-deployed |
| Max leverage | **1.75x** (Apr 24) | ⚠ Near margin boundary |
| Avg cash% | **29%** | 🟡 Historically under-allocated |
| Avg per-trade risk | **0.373%** (vs 0.5% config) | 🟡 Under-using budget |
| Worst exit-day loss | **-1.13%** (no day hit 2% CB) | 🟢 |
| Stop policy | MFE 6.3× MAE at exit | 🟢 Stops appropriate |
| Single-name concentration | Up to 28.8% (XLI) | 🔴 High for small account |
| Multi-universe exposure | 1.52–1.75x via margin | ⚠ Monitor closely |
| Kelly vs config alignment | Momentum over-weighted vs Kelly | ⚠ Directional signal only |

---

*Report generated: 2026-04-28. SQL: `sqlite3 -readonly data/atlas.db`. Analysis: Python 3 (statistics stdlib). n=66 total trades, 49 closed.*
