# Atlas Returns Audit — Sections 1 & 2
**Date**: 2026-04-28  
**Analyst**: Research Analyst (PI Planning Team)  
**Database**: `/root/atlas/data/atlas.db` (read-only)  
**Trade date range**: 2026-03-13 → 2026-04-24 (entry), 2026-04-28 (latest exit)  
**Total rows in `trades`**: 66  
**Non-superseded trades**: 60 (6 `superseded` excluded from all analysis)

---

## ⚠️ PREAMBLE: DATA QUALITY ASSESSMENT (Read This First)

Before interpreting any numbers below, understand the following contamination issues. These are NOT hypothetical — they are confirmed from row-level inspection.

### A. Reconciler Duplicate Trades (CRITICAL — inflates PnL)

The reconciler bug documented in the system audit creates **non-superseded duplicate closed trades** — pairs that are NOT marked `superseded` but represent the same fill twice. Identified pairs:

| Ticker | Strategy | Trade A (id) | Trade B (id) | Entry A | Entry B | Same PnL? |
|--------|----------|--------------|--------------|---------|---------|-----------|
| D | mean_reversion | 92 (Mar 24) | 124 (Mar 25) | $59.38 | $59.38 | Yes ($41.08) |
| AMT | mean_reversion | 99 (Mar 31) | 128 (Apr 9) | $167.53 | $167.53 | Yes (~$49.22) |
| MRVL | momentum_breakout | 114 (Apr 2) | 127 (Apr 3) | $102.38 | $99.12 | Yes ($17.57) |
| MRVL | momentum_breakout | 117 (Apr 7) | 129 (Apr 10) | $108.63 | $108.63 | Yes ($63.12) |
| SLV | momentum_breakout/commodity_etfs | 136, 157, 178 | — | $71.92 | $71.92 | Yes (-$5.60 all three) |

**Impact**: mean_reversion reported P&L is ~$91 overstated. momentum_breakout reported P&L is ~$81 overstated. I report both raw and deduplicated estimates.

### B. Reconcile Phantom Trades (pnl=0)

Four trades have `exit_reason='reconcile_phantom'` and `pnl=0.0`. These are NOT real trades — they are reconciler artifacts where the system opened and immediately closed a position at identical prices.

| id | Ticker | Strategy/Universe |
|----|--------|-------------------|
| 150 | UNG | connors_rsi2/commodity_etfs |
| 141 | XLY | momentum_breakout/sector_etfs |
| 151 | XLY | momentum_breakout/sector_etfs |
| 142 | AAPL | momentum_breakout/sp500 |

These inflate "n_flat" counts and corrupt win-rate calculations. I flag them throughout.

### C. Regime History Gap (Apr 2–11, 2026)

`regime_history` has **zero entries for Apr 2–11, 2026** — exactly the 10-day tariff selloff period. Any trade entered in this window **cannot be backfilled** to a regime, and `regime_at_entry` in the trades table is also blank for most of them. These trades appear in an "UNKNOWN" bucket in Section 2. The regime during this period was almost certainly `bear_risk_off`.

### D. Summary: Raw vs Deduplicated P&L

| Strategy/Universe | Raw Realized PnL | Dedup Estimate | Raw n_closed | Dedup n_closed |
|-------------------|-----------------|----------------|--------------|----------------|
| momentum_breakout/sp500 | +$266.36 | ~$185.67 | 15 | ~12 |
| mean_reversion/sp500 | +$219.31 | ~$128.01 | 6 | ~4 unique |
| connors_rsi2/sp500 | +$86.15 | +$86.15 | 7 | 7 |
| opening_gap/sp500 | +$1.17 | +$1.17 | 3 | 3 |
| sector_rotation/sp500 | -$14.62 | -$14.62 | 5 | 5 |
| trend_following/sp500 | -$34.48 | -$34.48 | 3 | 3 |

**All tables below use raw (non-deduplicated) data** with dedup notes appended per strategy.

---

## SQL Queries Used

```sql
-- Q1: Per-strategy aggregate stats (sp500, non-superseded)
SELECT 
    strategy,
    COUNT(*) AS n_total,
    SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS n_open,
    SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS n_closed,
    SUM(CASE WHEN status='closed' AND pnl>0 THEN 1 ELSE 0 END) AS n_wins,
    SUM(CASE WHEN status='closed' AND pnl<0 THEN 1 ELSE 0 END) AS n_losses,
    SUM(CASE WHEN status='closed' AND pnl=0 THEN 1 ELSE 0 END) AS n_flat,
    ROUND(100.0*SUM(CASE WHEN status='closed' AND pnl>0 THEN 1 ELSE 0 END)/
          NULLIF(SUM(CASE WHEN status='closed' AND pnl<>0 THEN 1 ELSE 0 END),0),1) AS win_rate_pct,
    ROUND(AVG(CASE WHEN status='closed' AND pnl>0 THEN pnl END),2) AS avg_win_dollar,
    ROUND(AVG(CASE WHEN status='closed' AND pnl<0 THEN pnl END),2) AS avg_loss_dollar,
    ROUND(SUM(CASE WHEN status='closed' AND pnl>0 THEN pnl ELSE 0 END) /
          NULLIF(ABS(SUM(CASE WHEN status='closed' AND pnl<0 THEN pnl ELSE 0 END)),0),2) AS profit_factor,
    ROUND(SUM(CASE WHEN status='closed' THEN pnl ELSE 0 END),2) AS total_realized_pnl,
    ROUND(AVG(CASE WHEN status='closed' THEN pnl_pct END),4) AS avg_pnl_pct,
    ROUND(AVG(CASE WHEN status='closed' THEN hold_days END),1) AS avg_hold_days
FROM trades
WHERE universe='sp500' AND status != 'superseded'
GROUP BY strategy ORDER BY total_realized_pnl DESC;

-- Q2: Non-sp500 aggregate stats
-- [Same query with universe != 'sp500']

-- Q3: Regime coverage
SELECT regime_at_entry, COUNT(*) FROM trades GROUP BY regime_at_entry;

-- Q4: Regime-conditional matrix (sp500, non-superseded, with backfill)
SELECT 
    t.strategy,
    COALESCE(NULLIF(t.regime_at_entry,''), rh.regime_state, 'UNKNOWN') AS regime,
    COUNT(*) AS n_trades,
    SUM(CASE WHEN t.status='closed' AND t.pnl>0 THEN 1 ELSE 0 END) AS n_wins,
    SUM(CASE WHEN t.status='closed' AND t.pnl<0 THEN 1 ELSE 0 END) AS n_losses,
    SUM(CASE WHEN t.status='open' THEN 1 ELSE 0 END) AS n_open,
    ROUND(100.0*SUM(CASE WHEN t.status='closed' AND t.pnl>0 THEN 1 ELSE 0 END)/
          NULLIF(SUM(CASE WHEN t.status='closed' THEN 1 ELSE 0 END),0),1) AS win_rate_pct,
    ROUND(SUM(CASE WHEN t.status='closed' THEN t.pnl ELSE 0 END),2) AS total_pnl,
    ROUND(AVG(CASE WHEN t.status='closed' THEN t.pnl_pct END),3) AS avg_pnl_pct
FROM trades t
LEFT JOIN regime_history rh ON DATE(t.entry_date) = rh.date
WHERE t.universe = 'sp500' AND t.status != 'superseded'
GROUP BY t.strategy, regime ORDER BY t.strategy, regime;

-- Q5: Regime history for trade date range
SELECT date, regime_state FROM regime_history WHERE date >= '2026-03-13' ORDER BY date;
```

---

## Section 1 — Per-Strategy P&L Decomposition

### 1.1 SP500 Strategies — Aggregate Table

> **Sharpe formula**: `Sharpe_est = (mean_pnl_pct / std_pnl_pct) × √(252 / avg_hold_days)`  
> This is a per-trade annualized Sharpe, treating each closed trade return as one observation and scaling by the implied number of non-overlapping trades per year. Risk-free rate assumed = 0.  
> **⚠️ All Sharpe estimates are unreliable with n < 30. Mean_reversion Sharpe is particularly misleading due to zero-variance artificial floor from 100% win rate on a tiny sample.**

| Strategy | n_total | n_open | n_closed | n_wins | n_losses | n_flat | WR% | WR 95% CI | avg_win$ | avg_loss$ | Profit Factor | Total Realized$ | Avg PnL% | Avg Hold (d) | Sharpe_est | Reliability |
|----------|---------|--------|----------|--------|----------|--------|-----|-----------|----------|-----------|---------------|-----------------|----------|--------------|-----------|-------------|
| momentum_breakout | 18 | 3 | 15 | 9 | 5 | 1 | 64.3% | [35.7%, 80.2%] | +$37.60 | -$14.41 | 4.70 | +$266.36 | +3.70% | 3.7 | 3.71 | moderate |
| mean_reversion | 6 | 0 | 6 | 6 | 0 | 0 | 100.0% | [61.0%, 100.0%] | +$36.55 | N/A | ∞ | +$219.31 | +5.22% | 4.3 | 18.03 | weak signal |
| connors_rsi2 | 8 | 1 | 7 | 5 | 2 | 0 | 71.4% | [35.9%, 91.8%] | +$23.09 | -$14.64 | 3.94 | +$86.15 | +2.51% | 2.7 | 4.72 | weak signal |
| sector_rotation | 5 | 0 | 5 | 3 | 2 | 0 | 60.0% | [23.1%, 88.2%] | +$8.48 | -$20.03 | 0.64 | -$14.62 | +0.33%† | 1.7 | 0.46 | weak signal |
| opening_gap | 3 | 0 | 3 | 2 | 1 | 0 | 66.7% | [20.8%, 93.9%] | +$2.68 | -$4.18 | 1.28 | +$1.17 | +0.04% | 2.3 | 0.43 | NOISE |

> † sector_rotation avg_pnl_pct is inflated: 2 of 5 trades have null pnl_pct in the DB (COP Apr-1, DVN). The 0.0 value for COP Apr-1 is a data entry artifact.

**Unrealized PnL note**: Open sp500 positions (FCX @ $61.48×5, AVGO @ $422.57×1, CAT @ $835.24×1, ADI @ $403.88×2) have a combined entry book value of ~$2,836. Current market prices not available in DB — unrealized P&L cannot be computed here.

---

### 1.2 Per-Strategy Detail

#### momentum_breakout / sp500
**n=18 total | 3 open | 15 closed (9W / 5L / 1F)**  
*Dedup note: contains 2 MRVL duplicate pairs (trades 114/127 and 117/129) + 1 AAPL phantom. Deduplicated count: ~12 closed, ~$185.67 realized.*

| Trade | Entry | Exit | PnL$ | PnL% | Hold | Exit Reason | Note |
|-------|-------|------|------|------|------|-------------|------|
| AMD | Apr-18 | Apr-28 | +$118.57 | +21.3% | 9d | reconcile_fill | |
| MRVL | Apr-7 | Apr-11 | +$63.12 | +14.5% | 3d | trailing_stop_fill | |
| MRVL* | Apr-10 | Apr-11 | +$63.12 | +14.5% | 1d | stop_loss | **DUPLICATE of above** |
| ON | Apr-22 | Apr-25 | +$42.01 | +12.3% | 2d | reconcile_fill | |
| MRVL | Apr-2 | Apr-7 | +$17.57 | +4.3% | 4d | trailing_stop_fill | |
| MRVL* | Apr-3 | Apr-7 | +$17.57 | 0.0%† | 4d | trailing_stop | **DUPLICATE of above** |
| CVX | Mar-31 | Apr-1 | +$10.82 | +1.76% | 1d | trailing_stop_fill | |
| CVX | Mar-27 | Mar-28 | +$3.99 | +0.65% | 1d | stop_loss | |
| OXY | Mar-13 | Mar-23 | +$1.62 | +1.41% | 10d | trailing_stop | |
| AAPL‡ | Apr-20 | Apr-21 | $0.00 | 0.0% | 0d | reconcile_phantom | **PHANTOM** |
| MRVL | Apr-14 | Apr-17 | -$0.95 | -0.18% | 3d | reconcile_fill | |
| CHTR | Apr-22 | Apr-24 | -$2.09 | -0.86% | 1d | reconcile_fill | |
| CARR | Apr-14 | Apr-16 | -$14.91 | -2.14% | 2d | reconcile_fill | |
| STZ | Apr-14 | Apr-21 | -$22.13 | -2.70% | 7d | reconcile_fill | |
| FCX | Apr-16 | Apr-24 | -$31.95 | -9.39% | 8d | reconcile_fill | |

> † MRVL trade 127: pnl=$17.57 but pnl_pct stored as 0.0 — entry price $99.12 vs $102.38 in trade 114; data quality issue.  
> ‡ AAPL trade 142: entered Apr 20 (Sunday), phantom exit.

**Best**: AMD +$118.57 (+21.3%) | **Worst**: FCX -$31.95 (-9.4%)

---

#### mean_reversion / sp500
**n=6 total | 0 open | 6 closed (6W / 0L / 0F)**  
*Dedup note: D appears twice (trades 92 and 124, same trade), AMT appears twice (trades 99 and 128). Unique trades: 4. Deduplicated realized PnL: ~$128.01.*

| Trade | Entry | Exit | PnL$ | PnL% | Hold | Exit Reason | Note |
|-------|-------|------|------|------|------|-------------|------|
| AMT | Mar-31 | Apr-10 | +$49.22 | +7.34% | 10d | take_profit | |
| AMT† | Apr-9 | Apr-10 | +$49.22 | +7.34% | 1d | take_profit | **DUPLICATE** |
| D | Mar-24 | Mar-25 | +$41.08 | +5.32% | 1d | stop_loss | |
| D† | Mar-25 | Mar-26 | +$41.08 | +5.32% | 1d | stop_loss | **DUPLICATE** |
| D | Mar-31 | Apr-7 | +$36.27 | +4.70% | 7d | signal_exit | Distinct trade |
| DHR | Mar-13 | Mar-19 | +$2.44 | +1.31% | 6d | trailing_stop | |

**Best**: AMT +$49.22 (+7.34%) | **Worst** (still a win): DHR +$2.44 (+1.31%)  
**Note on 100% win rate**: 4 unique closed trades, all winners. The Wilson CI [61%–100%] confirms the lower bound exceeds 50%, but with n=4, this has almost no statistical weight.

---

#### connors_rsi2 / sp500
**n=8 total | 1 open (FCX) | 7 closed (5W / 2L / 0F)**  
*No duplicates identified. HCA signal_exit (Apr 1) has null pnl_pct — excluded from pnl_pct calculations.*

| Trade | Entry | Exit | PnL$ | PnL% | Hold | Exit Reason |
|-------|-------|------|------|------|------|-------------|
| BKR | Mar-16 | Mar-20 | +$72.16 | +12.09% | 4d | signal |
| MSI | Apr-1 | Apr-7 | +$14.20 | +3.33% | 6d | signal_exit |
| ECL | Mar-24 | Mar-25 | +$19.16 | +1.86% | 1d | stop_loss |
| NOC | Mar-24 | Mar-25 | +$7.75 | +1.16% | 1d | stop_loss |
| HCA | Apr-1 | Apr-2 | +$2.17 | null | — | signal_exit |
| MSFT | Mar-27 | Mar-30 | -$2.89 | -0.80% | 3d | broker_trailing_stop |
| HCA | Mar-18 | Mar-19 | -$26.40 | -2.59% | 1d | trailing_stop |

**Best**: BKR +$72.16 (+12.1%) | **Worst**: HCA -$26.40 (-2.6%)  
**Note on profit_factor**: PF=3.94 is dominated by BKR single outlier. Remove BKR → PF drops to ~0.48 (net negative). Distribution is heavy-tailed on upside.

---

#### sector_rotation / sp500
**n=5 total | 0 open | 5 closed (3W / 2L / 0F)**

| Trade | Entry | Exit | PnL$ | PnL% | Hold | Exit Reason | Note |
|-------|-------|------|------|------|------|-------------|------|
| DOW | Mar-31 | Apr-1 | +$13.56 | +12.42% | 1d | trailing_stop_fill | |
| COP | Mar-31 | Apr-2 | +$5.94 | +4.91% | 2d | trailing_stop_fill | |
| COP | Apr-1 | Apr-2 | +$5.94 | null† | — | trailing_stop | Separate entry |
| COP | Apr-6 | Apr-8 | -$18.06 | -6.92% | 2d | trailing_stop_fill | |
| DVN | Apr-2 | Apr-8 | -$22.00 | -8.75% | null | trailing_stop_fill | |

> † COP trade 126 (Apr 1): pnl_pct stored as 0.0; actual ~4.6%. Data entry artifact.

**Best**: DOW +$13.56 (+12.4%) | **Worst**: DVN -$22.00 (-8.75%)  
**Profit factor**: PF=0.64 — the only active sp500 strategy with PF < 1.0. Avg loss (-$20.03) is 2.4× avg win (+$8.48).

---

#### opening_gap / sp500
**n=3 total | 0 open | 3 closed (2W / 1L / 0F)**

| Trade | Entry | Exit | PnL$ | PnL% | Hold | Exit Reason |
|-------|-------|------|------|------|------|-------------|
| ADBE | Mar-16 | Mar-18 | +$4.70 | +0.95% | 2d | trailing_stop |
| ULTA | Mar-16 | Mar-20 | +$0.65 | +0.12% | 4d | signal |
| BSX | Mar-31 | Apr-2 | -$4.18 | -0.95% | 1d | trailing_stop_fill |

**Best**: ADBE +$4.70 (+0.95%) | **Worst**: BSX -$4.18 (-0.95%)  
**Note**: Total PnL = +$1.17. Average win $2.68 vs average loss $4.18 — risk:reward is inverted. n=3 = pure noise.

---

### 1.3 Non-SP500 Universe Combos

| Combo | n_total | n_open | n_closed | n_real_closed | n_wins | n_losses | WR% | Total Realized$ | Avg PnL% | Avg Hold | Notes |
|-------|---------|--------|----------|---------------|--------|----------|-----|-----------------|----------|----------|-------|
| momentum_breakout/sector_etfs | 5 | 3 | 2 | **0** | 0 | 0 | N/A | $0.00 | 0.0% | 1.5d | Both closed = phantoms (pnl=0) |
| momentum_breakout/commodity_etfs | 5 | 2 | 3 | **1†** | 0 | 1 | 0.0% | -$16.80 | -1.30% | 2.0d | Likely triple-counted SLV loss |
| connors_rsi2/commodity_etfs | 4 | 1 | 3 | **2** | 1 | 1 | 50.0% | +$11.87 | +0.63% | 1.3d | 1 phantom UNG excluded |

> **momentum_breakout/sector_etfs**: Trades 141 (XLY Apr-18, phantom) + 151 (XLY Apr-21, phantom) = $0.00 realized. Three live positions (XLY, XLK, XLI) currently open. No real closed P&L data exists.  
> **momentum_breakout/commodity_etfs**: All 3 closed trades are SLV at identical entry/exit ($71.92→$70.9867), identical pnl (-$5.60 each, trades 136/157/178). Almost certainly one real SLV trade triple-counted by reconciler. GLD (open Apr-16, $442.80) and CCJ (open Apr-24, $126.47) are the live positions.  
> **connors_rsi2/commodity_etfs** real closed: UNG Apr-22→24 (-$2.70, -0.47%) and UNG Apr-24→28 (+$14.57, +2.35%). Net +$11.87.

---

### 1.4 Supplementary — Inactive/Deprecated SP500 Strategies

| Strategy | n_closed | n_wins | n_losses | WR% | Total Realized$ | Avg PnL% | Note |
|----------|----------|--------|----------|-----|-----------------|----------|------|
| trend_following (disabled) | 3 | 1 | 2 | 33.3% | -$34.48 | -4.80% | All 3 trades from pre-disable period |
| short_term_mr (deprecated) | 2 | 1 | 1 | 50.0% | -$1.29 | -2.48%† | |
| reconciled (artifact) | 1 | 0 | 1 | 0.0% | -$2.09 | -0.86% | CHTR reconciler artifact |

> † short_term_mr CARR has null pnl_pct (broker_stop_fill). WM stop_loss: -$11.24 (-2.48%). CARR broker_stop_fill: +$9.95 (pnl_pct unknown).

---

### 1.5 Strategy Verdicts

| Strategy | Sample Tier | Verdict | Confidence | Dedup-Adjusted Verdict |
|----------|-------------|---------|------------|------------------------|
| momentum_breakout/sp500 | n=15, **moderate** | **NET_POSITIVE** | Moderate — but 2 dup pairs and AMD outlier (+21%) = 45% of gross wins; dedup-adjusted still positive | NET_POSITIVE with material caveats |
| mean_reversion/sp500 | n=4 unique, **noise** | **NET_POSITIVE** | Noise — 4 unique wins, 100% WR statistically meaningless | NET_POSITIVE (but noise) |
| connors_rsi2/sp500 | n=7, **weak signal** | **NET_POSITIVE** | Weak signal — BKR single trade = 84% of gross wins; without it, net negative | NET_POSITIVE with caveats |
| sector_rotation/sp500 | n=5, **weak signal** | **NET_NEGATIVE** | Noise/weak — PF=0.64, Sharpe=0.46, avg loss > avg win | NET_NEGATIVE / NOISE |
| opening_gap/sp500 | n=3, **noise** | **NOISE** | Noise — n=3, total PnL $1.17, inverted R:R | NOISE |
| momentum_breakout/sector_etfs | n=0 real closed | **INCONCLUSIVE** | No data | — |
| momentum_breakout/commodity_etfs | n=1 real closed | **NOISE** | — | — |
| connors_rsi2/commodity_etfs | n=2 real closed | **NOISE** | — | — |

**Confidence tier definitions**:
- **noise — ignore**: < 5 closed trades
- **weak signal**: 5–15 closed trades  
- **moderate**: 15–30 closed trades  
- **decent**: 30+ closed trades

---

### Section 1 Recommendations

1. **Do not promote or suppress any sp500 strategy on the basis of this data alone.** No strategy has reached the 30-trade "decent" threshold. The best we have is momentum_breakout at n=15 (moderate), contaminated by 2 duplicate pairs.

2. **URGENT — Fix reconciler duplicate detection.** The duplicate trade pairs (D, AMT, MRVL×2, SLV×3) corrupt all performance metrics. Until fixed, true per-strategy P&L cannot be reliably computed. This is the single highest-leverage data hygiene fix.

3. **mean_reversion 100% win rate is a statistical artifact of small sample + duplicate contamination.** Do not increase allocation based on this. 4 unique closed trades over 46 days — one losing trade destroys the metric.

4. **momentum_breakout profit_factor (4.70 raw) is misleading.** AMD +$118.57 accounts for 44% of all gross wins. This was an exceptional tariff-recovery move. Mean-reversion of this outlier is the base case.

5. **sector_rotation is the only net-negative active strategy (PF=0.64, -$14.62).** At n=5, this is weak-signal territory, but avg loss (-$20.03) is 2.4× avg win (+$8.48). If the next 5 trades continue this pattern, consider reducing allocation weight from 15% to ≤5%.

6. **opening_gap is functionally inactive** — 3 trades in 46 days. The 5% weight is idle capital generating near-zero signal.

7. **sector_etfs and commodity_etfs combos need reconciler cleanup before any analysis is meaningful.** Both are dominated by phantom trades and duplicate artifacts.

---

## Section 2 — Regime-Conditional Performance

### 2.1 Regime Coverage Assessment

```
-- Result of: SELECT regime_at_entry, COUNT(*) FROM trades GROUP BY regime_at_entry;
(blank)              : 27 trades  (45%)
bear_risk_off        :  2 trades
bull_risk_on         :  5 trades
recovery_early       : 17 trades
transition_uncertain : 15 trades
```

**Coverage**: 38 of 60 non-superseded trades (63%) have direct `regime_at_entry`. 22 are blank.

**After LEFT JOIN backfill** from `regime_history`: 12 additional assignments recovered. **10 trades remain UNKNOWN** — their entry dates fall in the Apr 2–11 regime gap or on weekends (Saturdays/Sundays have no regime_history entries).

**Regime_history for the full trade period** (2026-03-13 → 2026-04-27):

| Date | Regime |
|------|--------|
| 2026-03-13 | transition_uncertain |
| 2026-03-16 to 03-19 | bull_risk_on |
| 2026-03-20, 03-23 to 03-26 | transition_uncertain |
| 2026-03-27, 03-30 | bear_risk_off |
| 2026-03-31, 04-01 | transition_uncertain |
| **2026-04-02 to 04-11** | **MISSING (tariff crash — 10 days unrecorded)** |
| 2026-04-12 to 04-13 | recovery_early |
| 2026-04-16 to 04-24 | recovery_early |
| **2026-04-27** | **bull_risk_on** ← newly flipped as of yesterday |

**Critical context**: The Apr 2–11 gap is NOT random missing data — it precisely covers the tariff-driven selloff (S&P 500 down ~15% intraday peak during this window). Trades in UNKNOWN that entered Apr 2–11 almost certainly experienced `bear_risk_off` conditions.

---

### 2.2 Regime × Strategy Matrix (SP500, non-superseded)

> **Cell format**: n_total | n_wins / n_losses / n_open | WR%(closed) | Total PnL$ | Avg PnL%  
> **Cells with n_closed < 3 are marked NOISE.** Do not drive decisions from single-cell findings.

#### momentum_breakout / sp500

| Regime | n_total | n_wins | n_losses | n_open | WR%(closed) | Total PnL$ | Avg PnL% | Flag |
|--------|---------|--------|----------|--------|-------------|------------|----------|------|
| bear_risk_off | 1 | 1 | 0 | 0 | 100% | +$3.99 | +0.65% | NOISE (n=1) |
| recovery_early | 9 | 1 | 5 | 3 | **16.7%** | **-$30.01** | **-0.50%** | ⚑ **TIGHTEN_OR_DISABLE** |
| transition_uncertain | 3 | 3 | 0 | 0 | 100% | +$30.01 | +2.49% | ✓ REGIME_STRENGTH (weak, n=3) |
| UNKNOWN† | 5 | 4 | 0 | 0 | 80.0% | +$262.38 | +10.07% | ⚠️ ARTIFACT — do not use |

> † UNKNOWN trades: MRVL Apr 3 (dup of trade 114), MRVL Apr 7 (dup of trade 117), MRVL Apr 10 (dup), AMD Apr 18 (Saturday — no regime_history entry), AAPL Apr 20 (Sunday phantom). The 80% WR and +$262 in this bucket are almost entirely from AMD +$118 outlier and MRVL duplicate chain. This is NOT a genuine regime signal.

**recovery_early detail** — the key finding for Section 2:  
The 6 closed recovery_early trades: CARR(-$14.91), MRVL(-$0.95), STZ(-$22.13), FCX(-$31.95), CHTR(-$2.09), ON(+$42.01). Five losses, one win. Total -$30.01. **All exits are `reconcile_fill`** — none exited via the strategy's own stop/target logic. These positions were swept out by the reconciler during its hourly runs, suggesting the strategy generated entries at the start of the recovery_early regime but the positions deteriorated without hitting their programmed exits.

---

#### connors_rsi2 / sp500

| Regime | n_total | n_wins | n_losses | n_open | WR%(closed) | Total PnL$ | Avg PnL% | Flag |
|--------|---------|--------|----------|--------|-------------|------------|----------|------|
| bear_risk_off | 1 | 0 | 1 | 0 | 0.0% | -$2.89 | -0.80% | NOISE (n=1) |
| bull_risk_on | 2 | 1 | 1 | 0 | 50.0% | +$45.76 | +4.75%† | NOISE (n=2) |
| recovery_early | 1 | 0 | 0 | 1 | N/A | $0.00 | — | NOISE (n=1 open) |
| transition_uncertain | 4 | 4 | 0 | 0 | **100%** | **+$43.28** | **+2.12%** | ✓ REGIME_STRENGTH (weak, n=4) |

> † bull_risk_on: BKR +$72.16 and HCA -$26.40. Net positive dominated by single outlier. 2 trades = noise.

---

#### mean_reversion / sp500

| Regime | n_total | n_wins | n_losses | n_open | WR%(closed) | Total PnL$ | Avg PnL% | Flag |
|--------|---------|--------|----------|--------|-------------|------------|----------|------|
| UNKNOWN† | 1 | 1 | 0 | 0 | 100% | +$49.22 | +7.34% | NOISE (n=1) |
| transition_uncertain | 5 | 5 | 0 | 0 | **100%** | **+$170.09** | **+4.80%** | ✓ REGIME_STRENGTH (contaminated) |

> † AMT trade 128 entered Apr 9 (no regime_history entry for that date).  
> **CRITICAL NOTE**: transition_uncertain mean_reversion contains n=5 with 2 duplicates (AMT×2 and D×2). Unique closed trades in transition_uncertain: D-Mar24, D-Mar31, DHR, AMT ≈ 4 unique trades, all wins, total ~$120. The REGIME_STRENGTH directional flag holds but is contaminated.

---

#### sector_rotation / sp500

| Regime | n_total | n_wins | n_losses | n_open | WR%(closed) | Total PnL$ | Avg PnL% | Flag |
|--------|---------|--------|----------|--------|-------------|------------|----------|------|
| UNKNOWN (likely bear_risk_off†) | 2 | 0 | 2 | 0 | 0.0% | **-$40.06** | **-7.84%** | ⚑ CANDIDATE (n=2, noise) |
| transition_uncertain | 3 | 3 | 0 | 0 | 100% | +$25.44 | +5.78% | ✓ REGIME_STRENGTH (n=3, weak) |

> † UNKNOWN: DVN (Apr 2 — tariff crash onset) and COP (Apr 6 — during crash), both exited at loss Apr 8. These were almost certainly entered and held through `bear_risk_off` conditions. If reclassified as bear_risk_off: 0W/2L (-$40.06, -7.84%) would be the strongest TIGHTEN_OR_DISABLE candidate in the dataset after momentum_breakout/recovery_early.

---

#### opening_gap / sp500

| Regime | n_total | n_wins | n_losses | n_open | WR%(closed) | Total PnL$ | Avg PnL% | Flag |
|--------|---------|--------|----------|--------|-------------|------------|----------|------|
| bull_risk_on | 2 | 2 | 0 | 0 | 100% | +$5.35 | +0.54% | NOISE (n=2) |
| transition_uncertain | 1 | 0 | 1 | 0 | 0.0% | -$4.18 | -0.95% | NOISE (n=1) |

All cells pure noise. No actionable signal.

---

#### trend_following / sp500 (disabled — reference only)

| Regime | n_total | n_wins | n_losses | n_open | WR%(closed) | Total PnL$ | Avg PnL% | Flag |
|--------|---------|--------|----------|--------|-------------|------------|----------|------|
| UNKNOWN | 1 | 0 | 1 | 0 | 0.0% | -$19.68 | -10.42% | NOISE (n=1) |
| bull_risk_on | 1 | 0 | 1 | 0 | 0.0% | -$19.54 | -5.67% | NOISE (n=1) |
| transition_uncertain | 1 | 1 | 0 | 0 | 100% | +$4.74 | +1.68% | NOISE (n=1) |

---

### 2.3 Summary: Flags

#### ⚑ TIGHTEN_OR_DISABLE_IN_REGIME (negative P&L AND ≥ 3 closed trades)

| Strategy | Regime | n_closed | WR% | Total PnL$ | Avg PnL% | Confidence | Recommended Action |
|----------|--------|----------|-----|------------|----------|------------|--------------------|
| **momentum_breakout/sp500** | **recovery_early** | **6** | **16.7%** | **-$30.01** | **-0.50%** | **Weak signal (n=6)** | **Tighten entry criteria in recovery_early; monitor closely** |

*This is the only cell meeting both criteria (negative P&L AND n_closed ≥ 3) in this dataset.*

**Below-threshold candidates** (n < 3, but directionally worth monitoring):

| Strategy | Regime | n_closed | WR% | Total PnL$ | Avg PnL% | Confidence | Note |
|----------|--------|----------|-----|------------|----------|------------|----|
| sector_rotation/sp500 | UNKNOWN (likely bear_risk_off) | 2 | 0.0% | -$40.06 | -7.84% | Noise (n=2) | Would qualify if regime backfilled to bear_risk_off |
| connors_rsi2/sp500 | bear_risk_off | 1 | 0.0% | -$2.89 | -0.80% | Noise (n=1) | Far too small |

#### ✓ REGIME_STRENGTH (avg_pnl_pct > 5% AND ≥ 3 trades)

| Strategy | Regime | n_total | WR%(closed) | Total PnL$ | Avg PnL% | Confidence | Note |
|----------|--------|---------|-------------|------------|----------|------------|------|
| sector_rotation/sp500 | transition_uncertain | 3 | 100% | +$25.44 | +5.78% | Noise (n=3) | All 3 wins were Mar 31–Apr 2 energy trades before crash |
| mean_reversion/sp500 | transition_uncertain | 5 (4 unique) | 100% | +$170.09 (raw) | +4.80% | Weak signal (4 unique) | Contains duplicates; directional flag valid but contaminated |
| connors_rsi2/sp500 | transition_uncertain | 4 | 100% | +$43.28 | +2.12% | Noise (n=4) | Avg PnL% below 5% threshold; included for completeness |

> **Note**: avg_pnl_pct of 2.12% for connors_rsi2 is below the >5% REGIME_STRENGTH threshold I defined. It's included in the table for completeness but does NOT formally qualify. The only strategy meeting >5% avg_pnl% AND ≥ 3 trades in a single regime is sector_rotation/transition_uncertain (avg 5.78%) and mean_reversion/transition_uncertain (avg 4.80% — just below threshold on deduplicated basis).

---

### 2.4 Regime Context — The Apr 2–11 "Invisible Bear" Problem

The biggest structural issue in Section 2: **the regime classification gap for the tariff crash period makes the regime matrix fundamentally incomplete**.

10 trades have no assignable regime:

| id | Ticker | Strategy | Entry Date | Assumed Missing Regime | PnL |
|----|--------|----------|------------|------------------------|-----|
| 127 | MRVL | momentum_breakout | Apr 3 | bear_risk_off | +$17.57 (dup) |
| 113 | DVN | sector_rotation | Apr 2 | bear_risk_off | -$22.00 |
| 115 | COP | sector_rotation | Apr 6 | bear_risk_off | -$18.06 |
| 116 | OXY | trend_following | Apr 6 | bear_risk_off | -$19.68 |
| 117 | MRVL | momentum_breakout | Apr 7 | bear_risk_off | +$63.12 (dup) |
| 128 | AMT | mean_reversion | Apr 9 | bear_risk_off/transition | +$49.22 (dup) |
| 129 | MRVL | momentum_breakout | Apr 10 | bear_risk_off/transition | +$63.12 |
| 140 | AMD | momentum_breakout | Apr 18 (Sat) | recovery_early | +$118.57 |
| 142 | AAPL | momentum_breakout | Apr 20 (Sun) | recovery_early | $0.00 (phantom) |
| 167 | XLY | momentum_breakout/sector_etfs | Apr 22 | recovery_early | — (open) |

**If the Apr 2–11 UNKNOWN trades were correctly classified as bear_risk_off**:
- sector_rotation would have bear_risk_off = 0W/2L (-$40.06, -7.84%) → second TIGHTEN flag
- trend_following would have bear_risk_off = 0W/1L (-$19.68) → directional but n=1

---

### Section 2 Recommendations

1. **MONITOR momentum_breakout/recovery_early — the only statistically-viable regime flag.** 1W/5L, -$30.01, n=6 closed. This is "weak signal" (5–15 trades) but directionally consistent. Recommended action: **require stricter entry confirmation** (e.g., RSI momentum confirmation above threshold, minimum ADX level) for momentum_breakout signals generated during recovery_early regime. Do NOT disable — the sample is too small to warrant full gating.

   *Important update*: regime_history flipped to `bull_risk_on` as of Apr 27. If this persists, the recovery_early finding becomes historical context. The 3 open momentum_breakout/sp500 positions (AVGO, CAT, ADI) entered during recovery_early should be monitored with reference to this pattern.

2. **Backfill regime_history for Apr 2–11.** This is a data correction (not a model change). These 10 days were unambiguously `bear_risk_off` — classifying them properly would surface a second TIGHTEN candidate (sector_rotation/bear_risk_off: 0W/2L) and clean up the misleading UNKNOWN bucket in momentum_breakout.

3. **Do not draw regime-conditional conclusions from bull_risk_on or bear_risk_off regimes.** After removing Apr 2–11 UNKNOWN trades, no strategy has ≥ 3 closed trades in either of these regimes. These cells are currently unobservable.

4. **The transition_uncertain REGIME_STRENGTH pattern** (three strategies positive, two with 100% WR) is likely a base-rate artifact, not alpha. transition_uncertain was the dominant regime from Mar 13 to Apr 1 — the period when the system had its freshest signals and the market was range-bound. As the regime mix diversifies over more months, this signal will dilute significantly.

5. **Consider explicitly gating sector_rotation in bear_risk_off** — pending regime backfill confirmation. The UNKNOWN Apr 2–11 sector_rotation trades (DVN -$22, COP -$18) suggest the strategy enters commodity/energy names near volatility spikes and holds through adverse moves without adequate stops. If a genuine bear_risk_off trigger is added to the active regime config, sector_rotation should be one of the first strategies to be gated off.

6. **Track the Apr 27 bull_risk_on regime transition carefully.** Yesterday's regime_history entry shows a flip from recovery_early to bull_risk_on. This is the first bull_risk_on classification since Mar 18-19. If confirmed over the next few trading days, it will be the first regime-conditional data we collect on the current strategy mix in bull conditions. No historical baseline exists for these strategies in bull_risk_on — treat it as an open experiment.

---

## Appendix: Open Positions as of Audit Date (2026-04-28)

| id | Ticker | Strategy | Universe | Entry Date | Entry Price | Shares | Regime at Entry |
|----|--------|----------|----------|------------|-------------|--------|-----------------|
| 190 | FCX | connors_rsi2 | sp500 | 2026-04-24 | $61.48 | 5 | recovery_early |
| 181 | AVGO | momentum_breakout | sp500 | 2026-04-23 | $422.57 | 1 | recovery_early |
| 187 | CAT | momentum_breakout | sp500 | 2026-04-24 | $835.24 | 1 | recovery_early |
| 189 | ADI | momentum_breakout | sp500 | 2026-04-24 | $403.88 | 2 | recovery_early |
| 186 | SLV | connors_rsi2 | commodity_etfs | 2026-04-24 | $68.27 | 6 | recovery_early |
| 135 | GLD | momentum_breakout | commodity_etfs | 2026-04-16 | $442.80 | 2 | recovery_early† |
| 182 | CCJ | momentum_breakout | commodity_etfs | 2026-04-24 | $126.47 | 4 | recovery_early |
| 167 | XLY | momentum_breakout | sector_etfs | 2026-04-22 | $116.44 | 10 | recovery_early† |
| 180 | XLK | momentum_backout | sector_etfs | 2026-04-23 | $156.77 | 8 | recovery_early |
| 185 | XLI | momentum_breakout | sector_etfs | 2026-04-24 | $173.97 | 9 | recovery_early |

> † regime_at_entry blank in DB; backfilled from regime_history.

*All 10 open positions entered during recovery_early regime. Apr 27 regime_history shows bull_risk_on — whether this affects position management depends on strategy regime-gating config (currently regime_filter.enabled=false per sp500.json v3.2).*

---

*Report generated 2026-04-28. All analysis is read-only. No trades, configs, or DB rows were modified.*  
*Saved to: `/root/.pi/expertise/research-analyst/audit_returns_2026-04-28_section_1_2.md`*  
*Intended path: `/root/atlas/research/audit_returns_2026-04-28_section_1_2.md` (write access denied — requires Planning Lead to copy)*
