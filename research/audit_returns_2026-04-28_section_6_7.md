# Atlas Returns Audit — Sections 6 & 7
## Universe Activation Analysis + Parameter Tuning Candidates
**Date:** 2026-04-28  
**Analyst:** Research Analyst (Planning Team)  
**Scope:** Read-only analysis of `data/atlas.db`, all `config/active/*.json`  
**Accounts:** Alpaca live ($5,380 equity); sp500, sector_etfs, commodity_etfs live; others passive/paper

---

## ⚠️ Critical Cross-Cutting Findings (Read Before Tables)

Before interpreting any number below, three issues colour the entire analysis:

1. **research_best Sharpe ≠ solo Sharpe for sp500.** The `autoresearch_runner.py` pipeline logs combined-portfolio Sharpe into `research_best`. Sweeper experiments record isolated single-strategy Sharpe. These are different metrics. For sp500: sweeper best for mean_reversion is **0.9831** (`kept` status, 134 experiments) vs research_best **0.2691**. Do not conflate.

2. **sector_etfs / gold_etfs / treasury_etfs research_best are CLONES.** The three universes share byte-for-byte identical `mean_reversion` params (MD5 `1137c6bb`, sharpe=0.9122, trades=53) and identical `momentum_breakout` params (sharpe=0.6949, trades=52). The research engine produced the same optimal point for three materially different asset classes. This is almost certainly a copy/broadcast artefact or an extremely short history that converges on the same local optimum. **Do not treat these as independently validated results.**

3. **ETF universe backtest samples are critically small.** sector_etfs, gold_etfs, treasury_etfs, defensive_etfs all show 50–53 backtest trades in research_best. The minimum for reliable Sharpe estimation is ~200 trades; <100 is overfit risk territory. **All ETF universe Sharpe values except commodity_etfs should be treated as preliminary signals only.**

---

## Section 6 — Universe Expansion Analysis

### 6a — Config Inventory

```
Queries run: python3 parsing of /root/atlas/config/active/*.json
```

| Universe | Mode | live_enabled | Enabled Strategies | Max Positions | Starting Equity | Leverage | Last Modified |
|---|---|---|---|---|---|---|---|
| **sp500** | live | ✅ True | momentum_breakout (0.25), connors_rsi2 (0.25), sector_rotation (0.25), mean_reversion (0.0978), opening_gap (0.05) | 10 | $5,011.79 | 2× | 2026-04-28 09:21 |
| **sector_etfs** | live | ✅ True | sector_rotation (0.4), mean_reversion (0.3), momentum_breakout (0.3) | 5 | $5,000 | 1× | 2026-04-27 21:43 |
| **commodity_etfs** | live | ✅ True | momentum_breakout (0.5), mean_reversion (0.3), connors_rsi2 (0.2) | 5 | $5,000 | 1× | 2026-04-28 01:01 |
| **gold_etfs** | passive | ❌ False | connors_rsi2, short_term_mr | 2 | $5,000 | 1× | 2026-04-24 14:30 |
| **treasury_etfs** | passive | ❌ False | mean_reversion, connors_rsi2 | 3 | $5,000 | 1× | 2026-04-24 14:30 |
| **defensive_etfs** | passive | ❌ False | mean_reversion, connors_rsi2 | 3 | $5,000 | 1× | 2026-04-24 14:30 |
| **crypto** | paper | ❌ False | momentum_breakout | 5 | $5,011.79 | 1× | 2026-04-14 22:42 |
| **asx** | passive | ❌ False | (none — monitoring only) | 10 | $0 | n/a | 2026-04-24 14:30 |

**Notes:**
- sp500 `mean_reversion` weight = **0.0978** (non-round). This was set by the portfolio optimizer commit `ca4adcad` (2026-04-28 01:57): "sp500: mean_reversion weight 0.3 → 0.0978, sector_rotation 0.15 → 0.25". The optimizer simultaneously pushed `sector_rotation` weight UP from 0.15 → 0.25, which conflicts with its near-zero research Sharpe (0.0442). This warrants scrutiny — see Section 7.
- `sector_rotation` weight in config = 0.25, but research_best stores the weight from the optimization as 0.15. The optimizer output appears contradictory.
- ASX uses Moomoo broker, not Alpaca — **cannot be live-traded through this system** regardless of mode.

---

### 6b — Backtest Evidence per Universe

```sql
SELECT universe, strategy, sharpe, trades, max_dd_pct, updated_at,
       json_extract(params, '$.weight') as weight
FROM research_best ORDER BY universe, sharpe DESC;
```

**Full results:**

| Universe | Strategy | Sharpe | Trades | Max DD% | Days Stale | Trade Count Flag |
|---|---|---|---|---|---|---|
| commodity_etfs | momentum_breakout | **1.2393** | 519 | 16.67 | 0.4 | ✅ OK |
| commodity_etfs | mean_reversion | **1.0748** | 495 | 20.34 | 0.4 | ✅ OK |
| defensive_etfs | mean_reversion | 1.0053 | 50 | 0.0 | 6.3 | ⚠️ LOW |
| sector_etfs | mean_reversion | 0.9122 | 53 | 0.0 | 6.4 | ⚠️ LOW + CLONE |
| gold_etfs | mean_reversion | 0.9122 | 53 | 0.0 | 6.3 | ⚠️ LOW + CLONE |
| treasury_etfs | mean_reversion | 0.9122 | 53 | 0.0 | 6.3 | ⚠️ LOW + CLONE |
| sp500 | momentum_breakout | 0.6505 | 1145 | 28.21 | 0.4 | ✅ OK |
| sp500 | consecutive_down_days | 0.6986 | 234 | 16.34 | 2.4 | ✅ OK (not yet deployed) |
| sp500 | trend_following | 0.6618 | 228 | 7.88 | 47.7 | ⚠️ STALE 48d |
| sp500 | connors_rsi2 | 0.4148 | 942 | 24.54 | 48.2 | ⚠️ STALE 48d |
| gold_etfs | connors_rsi2 | 0.5735 | 528 | 0.0 | 25.8 | ✅ OK |
| sector_etfs | connors_rsi2 | 0.2987 | 859 | 0.0 | 25.8 | ⚠️ Below 0.3 |
| defensive_etfs | momentum_breakout | 0.6949 | 52 | 0.0 | 6.3 | ⚠️ LOW + CLONE |
| gold_etfs | momentum_breakout | 0.6949 | 52 | 0.0 | 6.3 | ⚠️ LOW + CLONE |
| sector_etfs | momentum_breakout | 0.6949 | 52 | 0.0 | 6.4 | ⚠️ LOW + CLONE |
| treasury_etfs | momentum_breakout | 0.6949 | 52 | 0.0 | 6.3 | ⚠️ LOW + CLONE |
| sp500 | bb_squeeze | 0.4859 | 331 | 9.67 | 47.4 | ⚠️ STALE |
| sp500 | short_term_mr | 0.5036 | 543 | 4.29 | 46.6 | ⚠️ STALE |
| sp500 | mean_reversion | 0.2691 | 1049 | 32.57 | 0.5 | ⚠️ Low (combined SR) |
| sp500 | opening_gap | 0.0989 | 1071 | 33.87 | 0.5 | ⚠️ Marginal |
| sp500 | sector_rotation | 0.0442 | 647 | 0.0 | 14.6 | ⚠️ Near-zero |
| crypto | mean_reversion | **-0.0081** | 26 | 0.0 | 6.2 | 🔴 NEGATIVE + LOW |

**Experiment counts (to assess research depth):**

| Universe | Strategy | Experiments (all statuses) | Max Solo Sharpe Found |
|---|---|---|---|
| sp500 | momentum_breakout | 490 | 1.0234 (kept), 1.1499 (discard_solo) |
| sp500 | mean_reversion | 699 | 0.9831 (kept), 1.014 (discard_solo) |
| sp500 | opening_gap | 719 | 0.9099 (kept) |
| sp500 | sector_rotation | 70 | 0.9099 (kept) |
| commodity_etfs | momentum_breakout | 106 | 1.2393 |
| commodity_etfs | mean_reversion | 132 | 1.0748 |
| sector_etfs | mean_reversion | 53 | 1.0941 |
| gold_etfs | mean_reversion | 46 | 1.0941 |
| treasury_etfs | mean_reversion | 59 | 1.0941 |
| defensive_etfs | mean_reversion | 46 | 1.0941 |
| crypto | mean_reversion | 69 | 0.1044 |
| crypto | momentum_breakout | 5 | 0.1886 |

**KEY FINDING:** sector_rotation/sp500 in experiments shows max_sharpe=0.9099 (`kept` status, 39 experiments) yet research_best stores only 0.0442. This confirms the `discard_solo` / combined-portfolio weighting effect — sector_rotation has decent solo Sharpe but contributes near-zero when combined. Similarly opening_gap solo Sharpe reaches 0.9099 but combined is 0.0989. The portfolio dilution effect is real.

**KEY FINDING (NEW):** `consecutive_down_days` / sp500 has research_best Sharpe = **0.6986** with 234 trades (updated Apr 25, fresh). This strategy is **not in the active config** but has been swept and found viable. It is a candidate for activation.

---

### 6c — Live Trade History per Non-SP500 Universe

```sql
SELECT universe, strategy, COUNT(*) as n, 
       SUM(pnl) as total_pnl, 
       AVG(pnl_pct) as avg_pnl_pct,
       SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins
FROM trades WHERE universe != 'sp500' AND status='closed' 
GROUP BY universe, strategy;
```

**commodity_etfs — closed trades:**

| Strategy | n | Total PnL | Avg pnl_pct | Wins | Notes |
|---|---|---|---|---|---|
| connors_rsi2 | 3 | **+$11.87** | +0.63% | 1/3 | UNG ×2, SLV. Mixed but net positive |
| momentum_breakout | 3 | **-$16.80** | -1.30% | 0/3 | All SLV. Likely reconciler duplicates (same pnl_pct=-1.30% on all 3) |

Raw commodity_etfs closed trades:
```
connors_rsi2|UNG |Apr 21→Apr 22 |$0.00  | 0.000% (superseded)
connors_rsi2|UNG |Apr 22→Apr 24 |-$2.70 |-0.468%
connors_rsi2|UNG |Apr 24→Apr 28 |+$14.57|+2.349%
momentum_breakout|SLV|Apr 16→Apr 22|-$5.60|-1.298% (original)
momentum_breakout|SLV|Apr 21→Apr 22|-$5.60|-1.298% (duplicate — reconciler)
momentum_breakout|SLV|Apr 22→Apr 22|-$5.60|-1.298% (triplicate — reconciler)
```

⚠️ **All three momentum_breakout/commodity_etfs losses are the same SLV trade duplicated by the reconciler.** The actual economic event is one trade: SLV entered Apr 16, lost $5.60 (-1.3%). Do not weight this 3×. Net P&L from one real commodity_etfs momentum_breakout trade = -$5.60.

**sector_etfs — closed trades:**

| Strategy | n | Total PnL | Avg pnl_pct | Wins | Notes |
|---|---|---|---|---|---|
| momentum_breakout | 2 | **$0.00** | 0.00% | 0/2 | Both XLY — flat $0 exit |

Both XLY closed trades show pnl=0.00, pnl_pct=0.00 — these are reconciler-created phantom exits (stop order at $0), not genuine price-based exits.

**Net economic summary (non-sp500, excluding reconciler artefacts):**
- 1 real commodity_etfs trade: SLV MB -$5.60
- 2 real commodity_etfs connors_rsi2 trades: UNG -$2.70 and UNG +$14.57 = net +$11.87
- 2 sector_etfs trades: flat $0.00 each (phantom)
- **Net live P&L non-sp500: ~+$6 from ~3 real trades. Statistically meaningless.**

---

### 6d — Correlation with SP500 (Proxy)

```sql
SELECT universe, DATE(exit_date) as edate, SUM(pnl) as daily_pnl
FROM trades WHERE status='closed' AND exit_date IS NOT NULL
GROUP BY universe, DATE(exit_date)
ORDER BY universe, edate;
```

**Overlapping trading days:**
- sector_etfs ∩ sp500: **1 date** — Pearson r undefined (n < 3)
- commodity_etfs ∩ sp500: **2 dates** — Pearson r undefined (n < 3)

Live trade history is too sparse for a quantitative correlation estimate.

**Qualitative assessment by asset class:**

| Universe | Expected Correlation vs SP500 | Basis |
|---|---|---|
| sector_etfs (XLK, XLY, XLI…) | HIGH (0.85–0.95) | These ARE S&P 500 sectors. By construction they move with the index. Very low diversification benefit. |
| commodity_etfs (GLD, SLV, UNG, CCJ…) | LOW to MODERATE (0.1–0.4) | Commodities price on supply/demand and macro; not equity earnings. GLD is often inversely correlated in risk-off. |
| gold_etfs (GLD, IAU, SGOL, BAR) | LOW to NEGATIVE (−0.2–0.2) | Gold is a safe-haven hedge. Typically negative correlation during equity selloffs. High diversification value. |
| treasury_etfs (TLT, IEF, SHY…) | NEGATIVE in risk-off (−0.3–0.0) | "Flight to safety" asset. Duration risk is independent of equity risk. |
| defensive_etfs (XLP, XLU, VYM…) | MODERATE (0.4–0.7) | Defensive sectors still correlate with the broad market but dampen in downturns. |
| crypto (BTC, ETH…) | VARIABLE (−0.1–0.7) | Uncorrelated during 2019–2020, increasingly correlated 2021+. Currently mildly correlated with risk-on/risk-off. |
| asx | NEAR-ZERO | Different timezone, different economy. Minimal correlation with US equities at daily close. |

**Diversification implication:** sector_etfs is the worst diversifier (already live, high correlation). treasury_etfs and gold_etfs are the best diversifiers (not yet live). commodity_etfs (already live) provides meaningful diversification.

---

### 6e — Activation Cost Analysis per Dormant Universe

#### gold_etfs (passive → live)
- **Config diff:** `mode: passive → live`, `live_enabled: false → true`. Alpaca block already present (`paper: false`). Strategies already defined in config with good connors_rsi2 params.
- **Monitoring overhead:** Research cron already runs nightly at 01:00 AEST (gold_etfs timer active). No new scripts needed. `execute_approved` cron at 23:15 AEST already fires for sp500 — would need to be extended to gold_etfs. ⚠️ Prior audit (Apr 22) flagged that `execute_approved` is NOT scheduled for non-sp500 universes.
- **Capital allocation:** max_open_positions=2, max_risk_per_trade=0.5% → ~$50–100/trade at $5000 equity. Maximum open exposure ~$400 (2 positions at 4% of equity). Workable at current AUM.
- **Risk level:** LOW. ETFs only (GLD, IAU, SGOL, BAR), no leverage, 2-position cap.
- **Research confidence:** connors_rsi2 at 0.5735 Sharpe / 528 trades is the ONLY gold_etfs result with adequate sample size. mean_reversion (0.9122 / 53 trades) is LOW CONFIDENCE and identical to sector/treasury.
- **Prerequisite:** Fix `execute_approved` cron to support non-sp500 universes (from Apr 22 audit). Without this, signals won't auto-execute.

#### treasury_etfs (passive → live)
- **Config diff:** Same as gold_etfs.
- **Monitoring overhead:** Research timer active (02:00 AEST). Same execute_approved prerequisite.
- **Capital allocation:** max_open_positions=3 → ~$150–450 max exposure. Manageable.
- **Risk level:** LOW-MEDIUM. Duration risk (TLT can move 2–3% on Fed surprises). ETFs only, no leverage.
- **Research confidence:** 🔴 CRITICALLY LOW. Only mean_reversion (53 trades, CLONE params) and momentum_breakout (52 trades, CLONE params). NOT independently validated.
- **Prerequisite:** Must run universe-specific research sweep to get >200 trade backtest before activation.

#### defensive_etfs (passive → live)
- **Config diff:** Same pattern.
- **Monitoring overhead:** Research timer active (04:00 AEST).
- **Capital allocation:** max_open_positions=3.
- **Risk level:** LOW.
- **Research confidence:** 🔴 CRITICALLY LOW. mean_reversion 50 trades, momentum_breakout 52 trades. Even slightly different from the gold/sector/treasury clone (defensive has zscore_lookback=34 vs 38 for others) suggesting it ran a separate sweep — but 50 trades is still insufficient.
- **Prerequisite:** Re-sweep to >200 trades.

#### crypto (paper → live)
- **Config diff:** `mode: paper → live`, `live_enabled: false → true`.
- **Monitoring overhead:** Research timer active (05:00 AEST). Bybit not connected — Alpaca crypto pairs would be used.
- **Capital allocation:** max_open_positions=5, max_risk=1% → ~$50/trade, max $500 exposure.
- **Risk level:** HIGH. 24/7 markets, high volatility, gap risk at open.
- **Research confidence:** 🔴 DISQUALIFYING. mean_reversion Sharpe = **−0.0081** with 26 trades. momentum_breakout max_sharpe=0.1886 (5 experiments — not enough). 69 experiments for mean_reversion found max=0.1044 (effectively zero). No viable strategy identified.
- **Decision:** KEEP PAPER until a strategy with Sharpe >0.4 and >200 trades is found.

#### asx (passive — monitoring)
- **Activation cost:** Impossible via Alpaca. ASX uses Moomoo broker (`"broker": "moomoo"`). The live trading infrastructure does not support Moomoo automated order execution.
- **Decision:** Permanently KEEP_DORMANT for live trading. Monitoring only is appropriate.

---

### 6f — Recommendation Table

| Universe | Best Backtest Sharpe (strategy, trades) | Sample Flag | Live Trades n | Live PnL (net) | Correlation w/ SP500 | **Recommendation** | Rationale |
|---|---|---|---|---|---|---|---|
| **asx** | 0.60 (combined, 1 exp, Feb 2026) | 🔴 1 experiment | 0 (passive) | $0 | n/a (wrong broker) | **KEEP_DORMANT** | Wrong broker (Moomoo). Cannot auto-trade via Alpaca. Monitoring-only is correct. |
| **sector_etfs** | 0.9122 (MR, 53 trades) | ⚠️ LOW + CLONE | 2 closed + 3 open | ~$0 net (phantom exits) | HIGH (0.85–0.95) | **KEEP_LIVE_BUT_PAUSE_NEW_SIGNALS** | Already live (mode=live); 3 open positions already exist and should run to completion. HIGH correlation with sp500 = low diversification benefit. Research sample critically small (53 trades) AND likely cloned from gold_etfs. Before accepting new signals, complete a universe-specific sweep targeting >200 trades. |
| **commodity_etfs** | 1.2393 (MB, 519 trades) | ✅ STRONG | 3 closed (1 real) | ~-$5 (real) | LOW (0.1–0.4) | **ACTIVATE_LIVE_FULL** | Already live (mode=live). Strongest ETF research signal (MB: Sharpe 1.24/519T, MR: 1.07/495T). Real sample too small to evaluate but research evidence is robust. Diversification benefit from commodity exposure. Continue and expand. |
| **gold_etfs** | 0.5735 (connors_rsi2, 528 trades) | ✅ OK | 0 | $0 | LOW-NEGATIVE | **ACTIVATE_PAPER** | connors_rsi2 has adequate backtest depth. Best diversifier after treasury. Small portfolio means capital constraint is real. Paper mode validates execution before going live. **Prerequisite: fix execute_approved cron for non-sp500.** |
| **treasury_etfs** | 0.9122 (MR, 53 trades) | 🔴 LOW + CLONE | 0 | $0 | NEGATIVE | **RE_SWEEP_THEN_PAPER** | Research params are cloned from gold/sector — not universe-specific. Re-sweep with longer horizon to get >200 trades. If sweep confirms Sharpe >0.5, activate paper for portfolio hedge benefit. |
| **defensive_etfs** | 1.0053 (MR, 50 trades) | 🔴 LOW | 0 | $0 | MODERATE | **KEEP_DORMANT** | 50-trade backtest is insufficient. Moderate correlation with sp500 means diversification benefit is limited. Lower activation priority vs treasury/gold. Keep dormant until research sample is adequate. |
| **crypto** | -0.0081 (MR, 26 trades) | 🔴 NEGATIVE + LOW | 0 | $0 | VARIABLE | **KEEP_DORMANT** | Negative Sharpe after 69 mean_reversion experiments. No viable strategy found. Not ready for paper let alone live. |

---

## Section 7 — Parameter / Sizing Tuning Candidates

### 7a — Staleness Audit

```sql
SELECT strategy, universe, sharpe, trades, max_dd_pct, updated_at,
       ROUND(julianday('now') - julianday(updated_at), 1) as days_stale
FROM research_best ORDER BY days_stale DESC;
```

**Full staleness table (critical flags highlighted):**

| Strategy | Universe | Sharpe | Trades | Days Stale | Flag |
|---|---|---|---|---|---|
| connors_rsi2 | sp500 | 0.4148 | 942 | **48.2** | 🔴 STALE + INACTIVE (pre-April tariff selloff) |
| trend_following | sp500 | 0.6618 | 228 | **47.7** | 🔴 STALE (not in active config) |
| dividend_capture | sp500 | −3.9152 | 23 | **47.5** | LOW TRADES + STALE (disabled — no action) |
| gap_and_go / macd_divergence / etc | sp500 | 0.0 | 0 | **47.5** | Disabled strategies with 0 trades — legacy |
| inside_bar_nr7 | sp500 | −58.2065 | 1 | **47.5** | Grotesque artefact (1 trade) |
| bb_squeeze | sp500 | 0.4859 | 331 | **47.4** | 🔴 STALE (not in active config but viable solo) |
| short_term_mr | sp500 | 0.5036 | 543 | **46.6** | 🔴 STALE (disabled in config) |
| stochastic_oversold / demark / williams_r | sp500 | 0.13–0.40 | 145–262 | 43–44 | STALE (not active) |
| lower_band_reversion | sp500 | 0.3934 | 393 | 43.5 | STALE (not active) |
| adx_trend_pullback | sp500 | 0.4423 | 274 | 43.0 | STALE (not active) |
| donchian_breakout / triple_rsi / keltner | sp500 | 0.03–0.34 | 129–200 | 42–43 | STALE (not active) |
| connors_rsi2 | gold_etfs | 0.5735 | 528 | **25.8** | Borderline stale |
| connors_rsi2 | sector_etfs | 0.2987 | 859 | **25.8** | Borderline stale + below 0.3 |
| sector_rotation | sp500 | 0.0442 | 647 | 14.6 | Near-zero Sharpe even if fresh |
| sector_etfs / gold_etfs / treasury_etfs (MR, MB) | all | 0.69–0.91 | 50–53 | 6.3–6.4 | LOW TRADES (<100) |
| defensive_etfs (MR, MB) | all | 0.69–1.01 | 50–52 | 6.3 | LOW TRADES (<100) |
| crypto / mean_reversion | crypto | −0.0081 | 26 | 6.2 | NEGATIVE + LOW |
| sp500 active strategies (MB, MR, OG) | sp500 | 0.10–0.65 | 1045–1145 | **0.4–0.5** | ✅ Fresh |
| commodity_etfs (MB, MR) | commodity_etfs | 1.07–1.24 | 495–519 | **0.4** | ✅ Fresh + Strong |
| consecutive_down_days | sp500 | 0.6986 | 234 | 2.4 | ✅ Fresh — undeployed candidate |

**Low-confidence flags (trades < 100 in research_best):**
- `sector_etfs/mean_reversion` (53 trades), `gold_etfs/mean_reversion` (53 trades), `treasury_etfs/mean_reversion` (53 trades), `defensive_etfs/mean_reversion` (50 trades), `sector_etfs/momentum_breakout` (52 trades), `gold_etfs/momentum_breakout` (52 trades), `treasury_etfs/momentum_breakout` (52 trades), `defensive_etfs/momentum_breakout` (52 trades), `crypto/mean_reversion` (26 trades), `sp500/dividend_capture` (23 trades), `sp500/donchian_breakout` (136 trades — borderline), `sp500/keltner_reversion` (129 trades — borderline).

---

### 7b — Live vs Research_best Gap

```sql
WITH live_stats AS (
  SELECT strategy, universe, COUNT(*) as n, AVG(pnl_pct) as avg_pnl_pct, 
         (CASE WHEN COUNT(*)>1 THEN 
           (AVG(pnl_pct*pnl_pct) - AVG(pnl_pct)*AVG(pnl_pct)) 
         ELSE 0 END) as var_pnl,
         SUM(pnl) as total_pnl,
         SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins
  FROM trades WHERE status='closed' GROUP BY strategy, universe
)
SELECT l.strategy, l.universe, l.n, ROUND(l.avg_pnl_pct*100,4) as avg_pnl_pct, 
       ROUND(l.total_pnl,2) as total_pnl, l.wins,
       rb.sharpe as research_sharpe, rb.trades as research_n,
       ROUND(rb.max_dd_pct,2) as rb_max_dd
FROM live_stats l LEFT JOIN research_best rb 
  ON l.strategy=rb.strategy AND l.universe=rb.universe
ORDER BY l.n DESC;
```

**Rough live annualised Sharpe** (per-trade method; assumes 50 trades/year for daily strategies):

```python
# Method: SR_annualized ≈ (mean_pnl_pct / std_pnl_pct) * sqrt(trades_per_year)
# WARNING: With n < 30 trades, these estimates are ±2 Sharpe units. Use as directional signal only.
```

| Strategy | Universe | Live n | Live avg pnl% | Live total PnL | Live wins/n | Rough Live SR | Research SR | Gap (Live−Research) | Sample Flag |
|---|---|---|---|---|---|---|---|---|---|
| momentum_breakout | sp500 | 15 | +3.70% | +$266 | 9/15 (60%) | **+3.18** | 0.6505 | +2.53 | ⚠️ n=15, SR estimate unreliable |
| mean_reversion | sp500 | 6 | +5.22% | +$219 | 6/6 (100%) | **+16.65** | 0.2691 | +16.4 | 🔴 n=6 ALL WINS — cherry-picked window, meaningless SR |
| connors_rsi2 | sp500 | 7 | +2.51% | +$86 | 5/7 (71%) | **+3.46** | 0.4148 | +3.05 | ⚠️ n=7, SR estimate unreliable |
| sector_rotation | sp500 | 5 | +0.33% | **−$15** | 3/5 (60%) | **+0.27** | 0.0442 | +0.23 | n=5, consistent with weak research |
| opening_gap | sp500 | 3 | +0.04% | +$4 | 2/3 (67%) | **+0.29** | 0.0989 | +0.20 | n=3, meaningless |
| trend_following | sp500 | 3 | −4.81% | **−$34** | 1/3 (33%) | **−5.58** | 0.6618 | −6.24 | n=3, huge but still small sample |
| connors_rsi2 | commodity_etfs | 3 | +0.63% | +$12 | 1/3 (33%) | **+2.94** | none | n/a | n=3 |
| momentum_breakout | commodity_etfs | 3 | −1.30% | −$17 | 0/3 (0%) | **0.00** | 1.2393 | n/a | 🔴 All 3 are reconciler duplicates of 1 SLV trade |
| momentum_breakout | sector_etfs | 2 | 0.00% | $0 | 0/2 | **0.00** | 0.6949 | n/a | Both are phantom $0 exits |
| short_term_mr | sp500 | 1–2 | −2.48% | −$1 | 1/2 | **0.00** | 0.5036 | n/a | n < 3, meaningless |

**CRITICAL CAVEAT on live Sharpe estimates:** At n=3–15, confidence intervals span ±4–8 Sharpe units. Any apparent outperformance of live vs research is statistically indistinguishable from noise. The **only valid interpretation** is:
- sector_rotation live (-$15, SR≈0.27) is **consistent** with its research Sharpe (0.04) — both are near zero
- mean_reversion live (6/6 wins, +$219) is likely a run of luck — the 100% win rate is not sustainable and is a small-sample artifact
- trend_following live (−$34) is directionally consistent with being shut down (disabled in current config)

---

### 7c — Strategy/Universe Recommendations

```
Scoring criteria:
RE_SWEEP   = params stale (>30d) OR research_best Sharpe inconsistent with experiments
ACCEPT     = params fresh (<14d), adequate backtest sample (>200 trades), live performance not alarming
WAIT       = insufficient live trades (< 10) to draw conclusions but research is reasonable
DISABLE    = both research weak (Sharpe < 0.3) AND live weak (negative or near-zero PnL)
```

| Strategy | Universe | Live n | Research SR | Verdict | **Recommendation** | Action |
|---|---|---|---|---|---|---|
| momentum_breakout | sp500 | 15 | 0.6505 | Live outperforming (n=15, cautious) | **RE_SWEEP** | Config atr_stop_mult=1.5 vs research_best=0.61. lookback_days=15 vs 14. Drift is significant. Trigger parameter sync. |
| mean_reversion | sp500 | 6 | 0.2691 | Live outperforming but n=6 meaningless | **RE_SWEEP** | Config is gen-0 (rsio=35, zsl=30, zse=-2.0, asm=1.5). research_best at 0.2691 is combined-portfolio Sharpe. Sweeper found 0.9831 (solo). Deploy sweeper params: rsio=39, zsl=20, zse=-0.9, asm=1.35. |
| connors_rsi2 | sp500 | 7 | 0.4148 | Live performing; research stale 48d | **RE_SWEEP** | Research_best from Mar 10 — pre-tariff selloff, pre-April regime shift. Need fresh sweep. Only 58 `kept` experiments (low sweep depth vs MB's 126). |
| sector_rotation | sp500 | 5 | 0.0442 | Live -$15 (consistent with research) | **DISABLE / REDUCE** | Portfolio optimizer just pushed weight UP to 0.25 (from 0.15) despite near-zero research SR. This is counter-productive. Weight 0.25 consumes 25% of strategy allocation for ~zero expected return. Reduce to 0.05 or disable. |
| opening_gap | sp500 | 3 | 0.0989 | Too early; research marginal | **WAIT** | Solo Sharpe from sweeper reaches 0.9099 but research_best is 0.0989 (combined drag). Config params drift: gap_threshold=-0.008 vs research_best=-0.0. n=3 trades only. |
| momentum_breakout | commodity_etfs | 3* | 1.2393 | Fresh research; live data tainted | **ACCEPT** | Strong research signal (519 trades). Live trades are reconciler artifacts, not real price discovery. Config was synced Apr 28. |
| mean_reversion | commodity_etfs | 0 closed | 1.0748 | Fresh research; no live yet | **ACCEPT** | Good research signal (495 trades). Config synced Apr 28. |
| connors_rsi2 | commodity_etfs | 3 | none | No research_best yet | **WAIT** | 3 live trades with net +$12. Need sweep to establish baseline. |
| mean_reversion | sector_etfs | 0 closed | 0.9122 / 53T | LOW sample + CLONE params | **RE_SWEEP** | 53-trade sample, identical params to gold/treasury (suspect). Not validated for sector_etfs specifically. Need universe-specific sweep >200 trades. |
| momentum_breakout | sector_etfs | 2 | 0.6949 / 52T | Phantom trades; LOW sample | **RE_SWEEP** | 52-trade sample, identical params across 4 universes. Phantom live data ($0 exits). Need universe-specific sweep. |
| connors_rsi2 | sector_etfs | 0 | 0.2987 | Below 0.3 threshold | **DISABLE** | Sharpe=0.2987 below minimum acceptable (0.3). Consider disabled. |
| mean_reversion | gold_etfs | 0 | 0.9122 / 53T | LOW sample + CLONE | **RE_SWEEP** | Same clone issue. Run before activating gold_etfs. |
| connors_rsi2 | gold_etfs | 0 | 0.5735 / 528T | Best ETF result; borderline stale | **ACCEPT (for paper)** | Only credible ETF result with adequate sample. 26 days stale but no major regime change since Apr 2. Good basis for paper activation. |
| mean_reversion | treasury_etfs | 0 | 0.9122 / 53T | LOW sample + CLONE | **RE_SWEEP** | Must sweep before any activation. |
| momentum_breakout | treasury_etfs | 0 | 0.6949 / 52T | LOW sample + CLONE | **RE_SWEEP** | As above. |
| mean_reversion | defensive_etfs | 0 | 1.0053 / 50T | LOW sample | **RE_SWEEP** | Even with slightly different params (zscore_lookback=34 vs 38), 50 trades is insufficient. |
| momentum_breakout | defensive_etfs | 0 | 0.6949 / 52T | LOW sample + CLONE | **RE_SWEEP** | As above. |
| mean_reversion | crypto | 0 | **−0.0081** | Negative Sharpe | **DISABLE** | 69 experiments, max found = 0.1044, research_best = −0.0081. No evidence of edge. |
| consecutive_down_days | sp500 | 0 | 0.6986 / 234T | Fresh, undeployed | **CONSIDER_ACTIVATE** | Not in active config. Fresh (Apr 25), adequate sample. Warrants investigation as a candidate strategy. |
| short_term_mr | sp500 | 2 | 0.5036 / 543T | Disabled; 46d stale | **RE_SWEEP then consider** | Disabled in config. 46-day-old result. Has decent sample size. Could re-evaluate. |
| trend_following | sp500 | 3 | 0.6618 / 228T | Disabled; 48d stale; live −$34 | **KEEP_DISABLED** | Disabled in config is correct. 48d stale. Live 3 trades all stop-hit. |
| bb_squeeze | sp500 | 0 | 0.4859 / 331T | Disabled; 47d stale | **RE_SWEEP if needed** | Not active but has reasonable sample. Lower priority. |

---

## Section 7d — Top 3 Highest-Leverage Parameter Changes

### 🥇 #1: `sector_rotation/sp500` — Weight 0.25 → ≤0.05 (or Disable)

**The Issue:** Portfolio optimizer commit `ca4adcad` (Apr 28, 01:57) set `sector_rotation` weight to **0.25**, simultaneously reducing `mean_reversion` weight from 0.30 to 0.0978. This is backwards relative to the research signal:

| Strategy | Research SR | Weight in Config |
|---|---|---|
| mean_reversion | 0.2691 (combined, 0.98 solo) | **0.0978** |
| sector_rotation | **0.0442** | **0.25** |

sector_rotation consumes 25% of the strategy allocation for near-zero expected Sharpe contribution. Live evidence: 5 closed trades, total **−$14.62**, only 3/5 wins. The prior sweep found sector_rotation optimal weight = **0.15**, not 0.25.

**Recommended change:** Reduce sector_rotation weight from 0.25 to 0.05 (floor). Redistribute freed weight to connors_rsi2 or mean_reversion.

**Impact estimate:** Strategy portfolio Sharpe includes sector_rotation's near-zero contribution weighted at 25%. Removing it could lift portfolio Sharpe proportionally to the expected return differential.

---

### 🥈 #2: `momentum_breakout/sp500` — atr_stop_mult 1.5 → 0.61

**The Issue:** The live config has `atr_stop_mult=1.5` (wide stops). research_best (Apr 27, fresh) found `atr_stop_mult=0.61` (tight stops) as optimal. This is a **59% reduction in stop distance**.

Implications:
- Tight stops → lower per-trade loss on failed breakouts → better risk/reward on the dominant trade type
- research_best also shows `lookback_days=14` (vs 15 in config) and `atr_period=18` (vs 20) and `trend_ma_period=27` (vs 20)

| Param | Config | research_best | Direction |
|---|---|---|---|
| atr_stop_mult | 1.5 | **0.61** | Much tighter |
| lookback_days | 15 | 14 | Slightly shorter |
| atr_period | 20 | 18 | Slightly shorter |
| trend_ma_period | 20 | **27** | Longer trend filter |

momentum_breakout has the largest live trade count (15 trades) and the highest total PnL (+$266). It is also the highest-weight live strategy (0.25). Getting its parameters right has the largest dollar impact.

**Impact estimate:** Research improvement from 0.6505 → 1.0234 (sweeper best kept) suggests ~57% Sharpe improvement is achievable with correct params.

---

### 🥉 #3: `mean_reversion/sp500` — Deploy Sweeper Params (rsi_oversold 35→39, zscore_entry -2.0→-0.9)

**The Issue:** Active config still has gen-0 params. research_best (combined portfolio) = 0.2691. But sweeper experiments found solo Sharpe = **0.9831** (kept status) with:

| Param | Config | research_best (combined) | Sweeper best |
|---|---|---|---|
| rsi_oversold | 35 | 39 | 39 |
| zscore_lookback | 30 | **20** | 20 |
| zscore_entry | −2.0 | **−0.9** | −0.9 |
| atr_stop_mult | 1.5 | 1.35 | 1.35 |
| max_hold_days | 20 | **25** | 25 |

The sweeper-best params use a more aggressive entry signal (zscore_entry −0.9 vs −2.0 means entering earlier in the mean reversion, with a shorter lookback). This generates more trades and a higher Sharpe in isolation (0.98 vs 0.27 combined).

**Note:** The sweeper Sharpe (0.98 solo) vs combined Sharpe (0.27) gap confirms that mean_reversion conflicts with the other strategies in the portfolio mix — they are taking similar trades, causing dilution. Deploying sweeper params will increase trade frequency for mean_reversion, which may further dilute connors_rsi2 and opening_gap signal quality.

---

## Section 7e — Top 3 Universes with Best Activation ROI

### 🥇 #1: commodity_etfs (Already Live — Expand Confidence)

**Status:** Already live. Strongest evidence base in the entire non-sp500 universe.

| Metric | Value |
|---|---|
| Best backtest Sharpe | 1.2393 (momentum_breakout, 519 trades) |
| Secondary Sharpe | 1.0748 (mean_reversion, 495 trades) |
| Both results fresh | Apr 27 (0.4 days stale) |
| Capital concentration | 3 open positions (GLD, CCJ, SLV) |
| Correlation with sp500 | LOW (asset class diversification) |
| Research depth | 106+132 experiments for the two strategies |

**Activation ROI:** Near-zero marginal cost (already running). Nightly sweeps are active and improving params continuously. The only near-term action is to monitor live performance as trade count accumulates.

**Concern:** All 3 closed momentum_breakout trades are SLV reconciler duplicates. Cannot validate live performance until clean trades accumulate. Target: 10 clean live trades before drawing conclusions.

---

### 🥈 #2: gold_etfs (Paper → Monitor → Live)

**Status:** Passive. connors_rsi2 has the second-best validated ETF result (Sharpe 0.5735, 528 trades, Apr 2).

| Metric | Value |
|---|---|
| Best validated Sharpe | 0.5735 (connors_rsi2, 528 trades) |
| Correlation with sp500 | LOW-NEGATIVE (safe-haven hedge) |
| Max positions | 2 (minimal capital) |
| Capital at risk | ~$200–400 at current AUM |
| Research timer | Active (01:00 AEST nightly) |
| Cron prerequisite | execute_approved fix needed |

**Activation ROI:** High relative to effort. Switching mode from passive to live in gold_etfs.json is a 2-line config change once the execute_approved cron is fixed. The connors_rsi2 result has adequate backtest depth. Portfolio benefit is genuine (negative correlation hedge).

**Risk:** mean_reversion params for gold_etfs are CLONED (53 trades, invalid). Only activate connors_rsi2 initially; disable mean_reversion in gold_etfs config until re-swept.

---

### 🥉 #3: treasury_etfs (Re-Sweep First, Then Paper)

**Status:** Passive. Research evidence is insufficient as-is but the theoretical diversification case is the strongest of any dormant universe.

| Metric | Value |
|---|---|
| Best backtest Sharpe (current) | 0.9122 (MR, 53 trades — CLONE, invalid) |
| Expected Sharpe post re-sweep | Unknown (50–80% of commodity_etfs estimate) |
| Correlation with sp500 | NEGATIVE in risk-off periods |
| Capital constraint | 3 positions max; minimal |
| Research timer | Active (02:00 AEST nightly) |

**Activation ROI:** Requires re-sweep (~1 research window, ~48h) to get credible params. If sweep returns Sharpe >0.5 with >200 trades, activating paper would add portfolio-level Sharpe improvement through negative correlation. Treasury ETFs represent the "free lunch" of portfolio theory when equity exposure is high.

**Prerequisite sequence:**
1. Set `treasury_etfs` research window to run extended sweep (target 200+ trades)
2. Monitor research_best for 1 week
3. If Sharpe ≥ 0.5 and trades ≥ 200: switch to paper mode
4. After 10+ paper signals: assess live activation

---

## Appendix: SQL Queries Used

```sql
-- Section 6b: research_best inventory
SELECT universe, strategy, sharpe, trades, max_dd_pct, updated_at,
       json_extract(params, '$.weight') as weight
FROM research_best ORDER BY universe, sharpe DESC;

-- Section 6b: experiment depth per universe/strategy
SELECT universe, strategy, status, COUNT(*) as n, 
       MAX(CASE WHEN sharpe IS NOT NULL THEN sharpe END) as max_sharpe
FROM research_experiments
WHERE universe='sp500' AND strategy IN ('mean_reversion','momentum_breakout',...)
GROUP BY universe, strategy, status ORDER BY strategy, status;

-- Section 6c: live P&L per non-sp500 universe
SELECT universe, strategy, COUNT(*) as n, 
       ROUND(SUM(pnl),2) as total_pnl, 
       ROUND(AVG(pnl_pct)*100,4) as avg_pnl_pct_pct,
       SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins
FROM trades WHERE universe != 'sp500' AND status='closed' 
GROUP BY universe, strategy ORDER BY universe, strategy;

-- Section 6d: daily P&L per universe for correlation
SELECT universe, DATE(exit_date) as edate, SUM(pnl) as daily_pnl
FROM trades WHERE status='closed' AND exit_date IS NOT NULL
GROUP BY universe, DATE(exit_date) ORDER BY universe, edate;

-- Section 7a: staleness audit
SELECT strategy, universe, sharpe, trades, max_dd_pct, updated_at,
       ROUND(julianday('now') - julianday(updated_at), 1) as days_stale
FROM research_best ORDER BY days_stale DESC;

-- Section 7b: live vs research gap
WITH live_stats AS (
  SELECT strategy, universe, COUNT(*) as n, AVG(pnl_pct) as avg_pnl_pct, 
         (CASE WHEN COUNT(*)>1 THEN 
           (AVG(pnl_pct*pnl_pct) - AVG(pnl_pct)*AVG(pnl_pct)) 
         ELSE 0 END) as var_pnl,
         SUM(pnl) as total_pnl,
         SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins
  FROM trades WHERE status='closed' GROUP BY strategy, universe
)
SELECT l.strategy, l.universe, l.n, ROUND(l.avg_pnl_pct*100,4) as avg_pnl_pct, 
       ROUND(l.total_pnl,2) as total_pnl, l.wins,
       rb.sharpe as research_sharpe, rb.trades as research_n,
       ROUND(rb.max_dd_pct,2) as rb_max_dd
FROM live_stats l LEFT JOIN research_best rb 
  ON l.strategy=rb.strategy AND l.universe=rb.universe
ORDER BY l.n DESC;
```

---

## Summary Matrix

| Priority | Item | Type | Effort | Expected Impact |
|---|---|---|---|---|
| 🔴 P0 | sector_rotation weight 0.25→0.05 | Config change | 1 min | Immediate portfolio Sharpe improvement |
| 🔴 P0 | momentum_breakout atr_stop_mult 1.5→0.61 | Config change | 1 min | Aligns live trading to research_best |
| 🔴 P0 | mean_reversion param sync to sweeper best | Config change | 5 min | Aligns to 0.98 solo Sharpe vs 0.27 current |
| 🟡 P1 | Re-sweep sector/gold/treasury/defensive ETFs (≥200 trades) | Research task | 48h | Establishes valid research foundation |
| 🟡 P1 | Fix execute_approved cron for non-sp500 universes | Infra fix | 1h | Prerequisite for any ETF live activation |
| 🟡 P1 | Activate gold_etfs paper (connors_rsi2 only) | Config change | 5 min | Portfolio hedge benefit |
| 🟢 P2 | Investigate consecutive_down_days for sp500 | Research | 1h | Sharpe 0.6986/234 trades, undeployed candidate |
| 🟢 P2 | Re-sweep sp500/connors_rsi2 (48d stale) | Research | 24h | Core active strategy needs refresh |
| 🟢 P2 | treasury_etfs extended sweep then paper | Research + config | 1 week | Best diversification ROI after gold |
| ⚪ P3 | Disable crypto/mean_reversion research sweeps | Config | 5 min | Stop wasting compute on negative-Sharpe strategy |
| ⚪ P3 | Clean up research_best clones (ETF universes) | DB housekeeping | 1h | Data integrity, prevent misleading future reports |

---

*Report generated: 2026-04-28. All data from `data/atlas.db` (read-only) and `config/active/*.json`. No code or data was modified during this analysis.*
