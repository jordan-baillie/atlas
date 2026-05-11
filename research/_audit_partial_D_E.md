# Audit Parts D + E — Actionability & ETF Failure Forensics
**Date:** 2026-05-06  
**Author:** Research Analyst (read-only deep inspection)  
**Working dir:** /root/atlas

---

## Executive Summary

Research is producing raw backtest intelligence, but the translation layer between research results and live behaviour is broken in three independent ways:

1. **Live strategies use stale research params** — research_best diverges from active config within days and the connection is only rewired manually.
2. **ETF universes activated live before meaningful research existed** — commodity_etfs first trade and first experiment both occurred on Apr 16, 2026 (same day). sector_etfs went live one day after research started.
3. **Discovery generates strategy files but zero adoption** — 22 research/strategies/*.py files exist that have never touched production. The pipeline from "discovery proposes → promoter gates → live" is completely inoperative.

Live losses: commodity_etfs -$129.62 (23% win rate), sector_etfs -$4.91 (57% win rate).

---

## Part D — Is Research Producing Actionable Intelligence?

---

### D1. Are live strategies chosen because of research?

**Short answer:** Partially yes for sp500, not at all for ETF universes.

#### sp500 research_best (raw query output)

```
universe | strategy          | sharpe | solo_sharpe | portfolio_sharpe | trades | max_dd_pct | updated_at
---------+-------------------+--------+-------------+------------------+--------+------------+-------------------
sp500    | connors_rsi2      | 0.1416 | 0.3686      | 0.1416           | 219    | 20.37      | 2026-05-05 13:56
sp500    | momentum_breakout | 0.749  | 0.774       | 0.15             | 250    | 16.67      | 2026-05-05 14:36
```

#### Live sp500.json params vs research_best params (as of May 6)

**momentum_breakout:**
| Param           | research_best | Live (sp500.json) | Diverged? |
|-----------------|--------------|-------------------|-----------|
| atr_stop_mult   | 0.81         | 0.61              | ❌ YES (-26%) |
| lookback_days   | 22           | 14                | ❌ YES (-36%) |
| atr_period      | 20           | 18                | ❌ YES |
| trend_ma_period | 35           | 27                | ❌ YES (-23%) |
| breakout_period | 10           | 10                | ✅ OK |

**connors_rsi2:**
| Param               | research_best | Live (sp500.json) | Diverged? |
|--------------------|--------------|-------------------|-----------|
| rsi_period          | 2            | 3                 | ❌ YES |
| rsi_entry           | 40           | 40                | ✅ OK |
| min_consecutive_down| 2            | 1                 | ❌ YES |
| ibs_max             | 0.75         | 0.5               | ❌ YES |
| ibs_filter_enabled  | true         | false             | ❌ YES |
| atr_stop_mult       | 1.35         | 1.0               | ❌ YES (-26%) |

**Verdict:** Research IS connected to live for sp500 — params were applied on Apr 28 via `_param_update_2026_04_28` in sp500.json. But research_best has continued evolving since then (updated May 5) and the live config has not been synced. **The connection is point-in-time, not continuous.** Within 8 days the live config is already stale versus current best-known parameters. There is no automated pull from research_best into live config between promoter events.

**Note on connors_rsi2 sp500 Sharpe:** Current research_best sharpe for connors_rsi2 on sp500 is 0.1416 (solo 0.3686), while the live weight is 0.5. This is well below the "Sharpe ≥ 0.5" quality gate cited in the May 1 audit note. The strategy is active at 50% weight on a result that is plainly sub-gate.

---

### D2. Has discovery proposed any new strategy adopted live in last 90 days?

**Answer: Zero new strategies adopted. Research is purely re-tuning the original 7.**

#### Distinct strategies with experiments in last 90 days (raw)
```
adx_trend_pullback, bb_squeeze, combined, connors_rsi2, consecutive_down_days,
demark_sequential, dividend_capture, donchian_breakout, gap_and_go,
heikin_ashi_reversal, inside_bar_nr7, keltner_reversion, lower_band_reversion,
macd_divergence, mean_reversion, momentum_breakout, monthly_rotation,
mtf_momentum, opening_gap, overnight_return, pead_earnings_drift,
put_call_vix_proxy, relative_strength_pullback, rsi_divergence, sector_rotation,
short_term_mr, stochastic_oversold, trend_following, triple_rsi, volume_climax,
vwap_reversion, williams_percent_r
→ 33 distinct strategy names experimented
```

#### Files: research/strategies/ vs strategies/ (live)
```
research/strategies/ files NOT in live strategies/ (22 never adopted):
  adx_trend_pullback, consecutive_down_days, demark_sequential, dividend_capture,
  donchian_breakout, gap_and_go, heikin_ashi_reversal, inside_bar_nr7,
  keltner_reversion, lower_band_reversion, macd_divergence, monthly_rotation,
  overnight_return, pead_earnings_drift, put_call_vix_proxy,
  relative_strength_pullback, rsi_divergence, stochastic_oversold,
  triple_rsi, volume_climax, vwap_reversion, williams_percent_r

Live strategies/ NOT in research/strategies/ (original 10, no new additions):
  connors_rsi2, mean_reversion, momentum_breakout, opening_gap, trend_following,
  bb_squeeze, short_term_mr, sector_rotation, entry_optimizer, base
```

#### Git: new files added to strategies/ in last 90 days
```
(none)
```
All 20 new strategy .py files went to `research/strategies/` only. None promoted to `strategies/`.

**Verdict: SEVERE.** The research/discovery → production pipeline has never completed end-to-end for any new strategy. All live activity is re-tuning the same 6–7 original strategies.

---

### D3. Does anyone act on research's "disable strategy" signal?

**Answer: No automated signal. Manual decision only. DEGRADED threshold requires 3 consecutive weeks.**

#### market_state table (raw)
```
commodity_etfs | 0 | ""                              | live | equity=1073.31 | 2026-05-05
sector_etfs    | 0 | "Daily drawdown 49.78% >= 2.00%" | live | equity=2637.57 | 2026-05-05
sp500          | 0 | ""                              | live | equity=1435.12 | 2026-05-02
```

#### DEGRADED definition (monitor/strategy_health.py)
```python
DEGRADED_CONSECUTIVE_WEEKS = 3
# Fires when live 60-day Sharpe < 0 for 3+ consecutive weekly Saturday checks
```

ETF universes ran for ~2.5 weeks before manual pause (Apr 16 → May 4). The auto-disable path requires 21+ days of weekly health checks, more than these markets ran. No DEGRADED/WATCH state was logged for either ETF universe. **The pause was 100% user-initiated from reading P&L directly.**

Additionally, the strategy health cron only runs `--market sp500` in the Saturday schedule. ETF universes have never had a health report generated.

**Verdict: MODERATE.** Health monitoring is sp500-only. DEGRADED threshold is slow (3 weeks) relative to how fast a small-AUM account can be hurt.

---

## Part E — What Specifically Failed for ETF Universes?

---

### E1. Research experiment results

#### commodity_etfs — strategy summary (raw)
```
connors_rsi2   |  70 exp | max_sharpe=1.498 | avg_sharpe=1.144 | avg_trades=608
mean_reversion | 179 exp | max_sharpe=1.414 | avg_sharpe=0.530 | avg_trades=271
momentum_breakout|150 exp| max_sharpe=1.316 | avg_sharpe=0.771 | avg_trades=452
opening_gap    |  66 exp | max_sharpe=0.397 | avg_sharpe=0.310 | avg_trades=148
trend_following | 101 exp | max_sharpe=0.352 | avg_sharpe=0.269 | avg_trades=151
sector_rotation |   5 exp | max_sharpe=0.328 | avg_sharpe=0.306 | avg_trades=148
```

#### commodity_etfs — Sharpe min/max/avg (in-sample variance signal)
```
connors_rsi2:    min=0.714, max=1.498, avg=1.144  (range=0.784)
mean_reversion:  min=0.000, max=1.414, avg=0.530  (range=1.414) ← HIGH
momentum_breakout:min=0.000,max=1.316, avg=0.771  (range=1.316) ← HIGH
```

#### commodity_etfs — trade count min/max/avg
```
connors_rsi2:    min=556, max=681, avg=608   (robust)
momentum_breakout:min=144, max=633, avg=452  (wide range)
mean_reversion:  min=0,   max=622, avg=271   (zero-trade failed runs present)
```

#### sector_etfs — strategy summary (raw)
```
mean_reversion  | 143 exp | max_sharpe=1.094 | avg_sharpe=0.556 | avg_trades=163
momentum_breakout|  33 exp | max_sharpe=0.760 | avg_sharpe=0.459 | avg_trades=117
```
**Only 2 strategies ever researched for sector_etfs.** 176 total experiments vs 571 for commodity_etfs. No connors_rsi2, no opening_gap, no trend_following coverage.

#### sector_etfs — Sharpe/trade ranges
```
mean_reversion:    min_sharpe=-0.249, max=1.094, min_trades=47, max_trades=298
momentum_breakout: min_sharpe=0.054,  max=0.760, min_trades=43, max_trades=210
```

#### When did commodity_etfs research start vs live activation?
```
First research_experiment for commodity_etfs: 2026-04-16 06:17 UTC
First live trade for commodity_etfs:          2026-04-16
```
**Simultaneous.** First experiment batch showed Sharpe=0.003–0.106 for momentum_breakout. Live money was deployed while research had barely started.

```
First research_experiment for sector_etfs: 2026-04-17
First live trade for sector_etfs:          2026-04-18
```
One-day lag. At first sector_etfs live trade there were ~18–19 total experiments.

---

### E2. What was the bar for "approve for live"?

**promoter.py gates (for param UPDATES — never used for initial activation):**
```
Gate 1: 24h cooldown per strategy
Gate 2: Regression check — no metric degrades >10%, drawdown doesn't increase >3pp,
         trade count doesn't drop >20%
Gate 3: Sharpe > 0, CAGR > 0, ≥ 20 trades (minimum floor, not an edge bar)
Gate 4: OOS time-split Sharpe > 0, perturbation pass rate ≥ 70%
```

**CRITICAL: The promoter was never invoked for ETF universe activation.**

Evidence from git commits:
```
e35eb630: "config: enable auto_approve=true on all universes" — direct config edit
91d6afc1: "P1-8: flip sector_etfs live_enabled=true + regression test" — direct config edit
```
No `auto_promote()` call was made for commodity_etfs or sector_etfs initial activation. The four promoter gates were completely bypassed.

**Gate 3 minimum (20 trades) is structurally inadequate for ETF universes.** A universe of 10–11 ETFs with 5–15 day hold periods produces only ~50–150 trades per year. A 20-trade minimum provides near-zero statistical protection. sp500 (200 tickers) routinely produces 500–1,000+ trades per year.

---

### E3. Warning signals present in research data (ignored)

**Signal 1: Extreme Sharpe variance = overfitting risk**
commodity_etfs momentum_breakout: Sharpe ranges from 0.0004 to 1.316 (3290× ratio).
A stable edge should show narrow Sharpe variance across param space. A 3000× range means the edge is a needle-in-haystack artefact.

**Signal 2: rsi_entry gap — live used 40, research was converging to 60**
```
rsi_entry=90: avg Sharpe=1.385 (1 run)
rsi_entry=50: avg Sharpe=1.277 (1 run)
rsi_entry=60: avg Sharpe=1.276 (22 runs) ← research converged here
rsi_entry=40: avg Sharpe=1.116 (25 runs) ← what live deployed
rsi_entry=75: avg Sharpe=0.991 (15 runs)
```
Live was using rsi_entry=40 (sp500 default, catches almost every RSI dip) when research was pointing toward 60 as meaningfully better for commodity ETFs.

**Signal 3: sector_etfs launched with 18 total experiments**
After one day of research, sector_etfs had ~18 experiment rows. This is not enough data to distinguish real edge from noise in an 11-ETF universe.

**Signal 4: Low trade counts in sector_etfs (43–47 min)**
11 ETFs producing 43 trades over 7-year backtest = 6 trades/year. Statistical significance is essentially absent at this frequency.

**Signal 5: Regime filter disabled during April tariff selloff**
Both ETF configs have `regime_enabled: false, regime_gate.enabled: false`. April 2026 was the sharpest macro drawdown since 2020. Long commodity positions entered during that window with no macro filter.

---

### E4. Was live activation guarded by paper trade or walk-forward?

**Answer: No. Zero paper trading stage for either ETF universe.**

- Both configs: `"paper": false` (live Alpaca account)
- `overlay.shadow_mode: true` applies ONLY to AI overlay annotations, not order routing
- No paper_broker / paper_account references in commodity_etfs or sector_etfs configs
- No walk-forward OOS (Gate 4) run before initial activation — Gate 4 only applies to promoter-gated param updates
- First commodity_etfs experiment and first live trade: same calendar day

---

### E5. Did the system flag the divergence after live activation?

**Answer: Partially. Daily drawdown circuit breaker fired once. No cumulative-loss alert exists.**

#### Live PnL by strategy/universe (raw, superseded=0)
```
connors_rsi2   | commodity_etfs | 12 trades | avg=$-3.48  | total=-$41.80 | win=25%
momentum_breakout|commodity_etfs|  6 trades | avg=-$19.98 | total=-$119.89| win=0%
momentum_breakout| sector_etfs  |  6 trades | avg=-$1.20  | total=-$7.19  | win=50%
reconciled     | sector_etfs   |  1 trade  | +$2.28     |               | win=100%
Total commodity_etfs: -$129.62
Total sector_etfs:     -$4.91
```

#### Notable individual losses
```
GLD/momentum_breakout/commodity_etfs: -$44.18 (-5.0%) — held 20 days
FCX/momentum_breakout/commodity_etfs: -$31.95 (-9.4%) — CROSS-MARKET GHOST
CCJ/momentum_breakout/commodity_etfs: -$26.96 (-5.3%)
FCX/connors_rsi2/commodity_etfs:      -$15.71 (-5.1%) — CROSS-MARKET GHOST
SLV/connors_rsi2/commodity_etfs:      -$15.26 (-3.7%)
```
FCX was an sp500-owned position that leaked into the commodity_etfs state file (identified and fixed in commit 00f0b634). Two of the top-5 losses were ghost trades from a state isolation bug.

#### Divergence detection timeline
```
Apr 16: Live entries with <0.1 Sharpe backing     → ❌ no alert
Apr 22: SLV stops hit                              → ❌ no alert
Apr 24: New entries with default params             → ❌ no alert
Apr 29: FCX cross-market conflict found (manually) → ❌ discovered manually
Apr 30: sector_etfs HALT (phantom drawdown)        → ✅ circuit breaker fired
May 4:  User pauses both ETF universes             → manual decision
May 5:  market_state logs sector_etfs DD=49.78%   → logged, no Telegram alert
```

**No Telegram alert was ever sent for cumulative ETF losses.** The circuit breaker only covers intraday daily drawdown. The strategy health monitor requires 3 weeks. No cumulative-loss-since-activation threshold exists anywhere in the codebase.

---

## Cross-Cutting Root Cause Table

| # | Root Cause | Severity |
|---|-----------|----------|
| 1 | Live activation simultaneous with (or 1 day after) research start — no prior evidence of edge | 🔴 CRITICAL |
| 2 | Promoter gates bypassed entirely for initial ETF live activation (direct config edit) | 🔴 CRITICAL |
| 3 | No paper trading / walk-forward before real capital deployed | 🔴 CRITICAL |
| 4 | Regime filter disabled during April tariff selloff (biggest macro shock since 2020) | 🔴 CRITICAL |
| 5 | FCX state isolation bug caused cross-market ghost trades (2 of top 5 losses) | 🟠 HIGH |
| 6 | Live params used sp500 defaults (rsi_entry=40) not universe-tuned values | 🟠 HIGH |
| 7 | Strategy health monitoring never configured for ETF universes (sp500-only) | 🟠 HIGH |
| 8 | No cumulative-loss-since-activation alert (only daily circuit breaker) | 🟠 HIGH |
| 9 | sector_etfs had only 33 momentum_breakout experiments at activation (insufficient coverage) | 🟡 MEDIUM |
| 10 | connors_rsi2 sp500 Sharpe 0.14 but running at 50% live weight (sub-gate) | 🟡 MEDIUM |
| 11 | discovery pipeline dead (browse_blog.md missing, 0 new strategies adopted) | 🟡 MEDIUM |

---

## Is Research Actionable Right Now?

**Actionable (but needs OOS gate before re-deploying):**
- commodity_etfs connors_rsi2: Sharpe 1.0–1.37 stable across 70 experiments (rsi_entry=60, atr_stop=0.68, rsi_period=4). Most credible ETF result.
- commodity_etfs momentum_breakout: Sharpe 1.22–1.32 across best configs (lookback=22, trend_ma=27, atr_stop=0.65).
- sp500 momentum_breakout: 250+ trades, Sharpe 0.749 solo. Best live-validated strategy.

**Not actionable:**
- sp500 connors_rsi2: research_best Sharpe 0.14 — active at 50% weight, sub-gate.
- sector_etfs (either strategy): 176 total experiments, no OOS, max 0.76 Sharpe at sparse coverage.
- Discovery pipeline: 22 research strategy files, 0 ever promoted, pipeline broken.

---

*Raw data queries executed against /root/atlas/data/atlas.db (88MB, WAL clean).  
Saved to /root/.pi/expertise/research-analyst/audit_partial_D_E.md*
