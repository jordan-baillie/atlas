# Commodity ETFs Deployment State Audit
**Date:** 2026-04-22  
**Investigator:** Research Analyst  
**Scope:** Is commodity_etfs ready for live, needs paper validation, or a dead end?

---

## TL;DR

commodity_etfs is **already live-trading** (2 open broker positions as of audit, 7 trades since Apr 16), but with **three critical operational gaps**: (1) `execute_approved.py` is not scheduled for commodity_etfs in crontab — plans are generated and approved but never auto-executed through the normal pipeline; (2) monitor/evaluator.py does not watch commodity_etfs; (3) sync_protective_orders.py only runs for sp500. Positions appear to be entering via the pi premarket agent directly (outside the execute_approved flow) and the reconciler backfills any gaps. The research Sharpe (0.906) is in-sample only — no OOS validation exists. Config params are 7 days stale vs. research_best.

---

## Config State

### `config/active/commodity_etfs.json` — EXISTS (6.4KB, modified Apr 14 20:47 AEST)

Key settings:
```
version: v1.0
market: commodity_etfs
trading.mode: live
trading.broker: alpaca
trading.live_enabled: true
alpaca.paper: false          ← LIVE, NOT PAPER
trading.auto_approve: false  ← manual approval required in theory
```

**Three strategies enabled:**

| Strategy | Weight | Key params in config |
|---|---|---|
| momentum_breakout | 0.50 | atr_stop_mult=**1.5**, lookback_days=15, trend_ma_period=20 |
| mean_reversion | 0.30 | rsi_oversold=35, zscore_entry=-2.0, atr_stop_mult=1.5 |
| connors_rsi2 | 0.20 | rsi_entry=40, atr_stop_mult=1.0 |

**Config vs Research_best mismatch (CONFIRMED):**  
- Config (written Apr 14): `momentum_breakout.atr_stop_mult = 1.5`  
- `research/best/momentum_breakout_commodity_etfs.json` (updated Apr 21): `atr_stop_mult = 1.0`  
- The research_best was updated 7 days after the config was frozen. Config is stale.

**Universe spec in config:** `method: static, benchmark_ticker: DBC` (no inline ticker list — delegates to `universe/definitions.py`)

---

### `config/research_priorities.json` — EXISTS (at `config/` not `research/`)

```json
"commodity_etfs": {
  "priority": "high",
  "windows_per_day": 1,
  "mode": "live",
  "notes": "Newly live. Dedicated 30-min sweep per window."
}
```

---

## "mode=live" Semantics

**INERT METADATA.** No code reads `research_priorities.json` to gate trading decisions. The file is consumed only by `services/chat_server.py` (lines 2470 and 2651) to display priority info on the dashboard and for a `/api/research/priorities` PUT endpoint. Setting `mode=live` in priorities.json has zero effect on whether trades are executed. The actual live gate is `config/active/commodity_etfs.json` → `trading.live_enabled: true`.

---

## Integration Gap Table

| Component | Status | Detail |
|---|---|---|
| Config file (`config/active/commodity_etfs.json`) | ✅ EXISTS | v1.0, live_enabled=true, paper=false |
| Universe definition | ✅ EXISTS | `universe/definitions.py`: 10 tickers (GLD, SLV, USO, XOP, CORN, DBA, DBB, UNG, CCJ, FCX) |
| Strategy code dispatch | ✅ PRESENT | All strategies are multi-universe aware; execute_approved.py has `--market` arg |
| Broker adapter integration | ✅ PRESENT | `brokers/registry.py` has alpaca for all markets; `reconcile_positions.py` includes commodity_etfs |
| Portfolio allocation slot | ✅ PRESENT | `portfolio/limits.py`: `commodity_etfs: {max_positions: 3, max_pct_equity: 0.30}` |
| Live state file | ✅ EXISTS | `brokers/state/live_commodity_etfs.json` — 3 positions tracked (GLD, UNG, XLY*) |
| **execute_approved cron entry** | ❌ **MISSING** | Crontab has `execute_approved.py -m sp500` only. No `-m commodity_etfs` scheduled. |
| **Premarket/Postclose cron** | ✅ EXISTS | `00 19 * * 1-5 pi-cron.sh premarket commodity_etfs` / `00 08 * * 2-6 pi-cron.sh postclose commodity_etfs` |
| **Monitor/risk (evaluator.py)** | ❌ **MISSING** | `monitor/evaluator.py:333`: loops `for market_id in ("sp500", "asx")` only |
| **sync_protective_orders cron** | ❌ **MISSING** | Only runs `--market sp500` in crontab |
| Dashboard row | ❌ **MISSING** | chat_server.py does not surface commodity_etfs positions separately |
| Systemd service (dedicated) | ❌ **NONE** | Research has `atlas-research-window@commodity_etfs.timer` but no trading service |
| Paper trading history | ❌ **NONE** | Went directly to live on Apr 14; zero paper validation |
| OOS validation | ❌ **NONE** | All 345 experiments are `experiment_type='sweeper'` (in-sample only) |
| healthz_hourly reconcile | ✅ PRESENT | `healthz_hourly.sh:66`: loops `for MKT in sp500 commodity_etfs` |

`*` XLY is in the live state but is NOT in the commodity_etfs universe — it belongs to sector_etfs. Universe drift from broker reconciliation backfilling an unknown position.

---

## How Positions Are Actually Entering

**Critical finding:** Positions are NOT entering via the scheduled `execute_approved.py` pipeline (that's sp500-only). They appear to enter via two routes:

1. **Pi premarket agent:** The premarket cron runs the pi agent for commodity_etfs at 19:00 AEST. Despite the prompt saying "do NOT approve or execute," the pi agent may approve and execute in the same session (or execute_approved is run manually post-plan).
2. **EOD reconciler backfills untracked broker positions.** The eod_settlement.log (Apr 22 08:00 AEST) explicitly shows:  
   `"UNTRACKED in ledger: UNG (54 shares @ $10.68) — backfilling"`  
   This means UNG existed at Alpaca broker but was not in Atlas's ledger — the reconciler detected and recorded it after the fact.

**This means commodity_etfs trades are entering Alpaca OUTSIDE the execute_approved flow.** Plans are generated (5 plan files Apr 14–21, last one APPROVED with 0 trades), but the execution mechanism is ad-hoc or manual. This is operationally fragile.

---

## Research Evidence Quality

### Experiment counts
- **Total experiments:** 345 (all type `'sweeper'`, date range Apr 16–Apr 21)
- **No OOS experiments** — every Sharpe number is full-period in-sample

### Top momentum_breakout results by Sharpe

| Sharpe | Trades | CAGR | MaxDD | Profit Factor | params_changed | status |
|---|---|---|---|---|---|---|
| **1.0923** | 500 | 18.3% | 19.5% | 1.65 | trend_ma_period=30 | **kept** |
| 1.0707 | 515 | 18.2% | 19.4% | 1.61 | lookback_days=25 | kept |
| 0.9903 | 505 | 17.0% | 19.5% | 1.55 | trend_ma_period=35 | discarded |
| 0.9587 | 351 | 14.4% | 11.5% | 1.73 | lookback_days=25 | kept |
| 0.9232 | 479 | 16.6% | 15.7% | 1.48 | trend_ma_period=30 | kept |
| **0.9060** | 451 | 14.5% | 17.5% | 1.62 | lookback_days=22 | kept → **in research_best** |
| 0.8934 | 434 | 14.1% | 17.6% | 1.58 | atr_stop_mult=1.0 | discarded |

### Clarification on the "atr_stop_mult=1.0" claim

The context stated "research says atr_stop_mult=1.0 is the best with Sharpe 0.906." This is **partially wrong**. The research_best file (`momentum_breakout_commodity_etfs.json`) stores atr_stop_mult=1.0 as part of its full param set, but:
- The highest Sharpe momentum_breakout result is **1.0923** (trend_ma_period=30, `kept`), not 0.906
- The atr_stop_mult=1.0 experiment itself was tested and **discarded** (Sharpe 0.8934 — worse than baseline)
- The research_best Sharpe 0.906 is a different experiment (lookback_days=22, kept) where atr_stop_mult happens to be 1.0 as a held param
- Config has atr_stop_mult=1.5 which was the original default; it should be updated to atr_stop_mult=1.0 per research_best, and trend_ma_period updated to 30 to capture the highest found Sharpe

### Multi-strategy vs solo Sharpe

The research_best 0.906 is a **multi-strategy portfolio result** (momentum_breakout 50% + mean_reversion 30% + connors_rsi2 20%). The `solo_sharpe` field = **0.3759** — momentum_breakout alone on commodity_etfs has a poor solo Sharpe. The combined portfolio carries connors_rsi2 (57.9% win rate on 190 trades) and mean_reversion (56.5% win rate on 62 trades) as ballast.

### Research_best full metrics (the 0.906 record)

```
CAGR:         14.48% (in-sample), 12.14% (full period CAGR)
Sharpe:       0.906 (in-sample, multi-strategy combo)
Sortino:      1.2604
Max drawdown: 17.47%
Calmar:       0.829
Win rate:     45.45%
Profit factor: 1.619
Total trades: 451 (across 3 strategies)
Edge p-value: 0.000686 ← statistically significant
MC p95 drawdown: 16.6% (non-fragile)
```

### Red flags
1. **No OOS split.** All Sharpe numbers are in-sample. The actual OOS Sharpe is unknown and could be substantially lower.
2. **10-ticker universe with 3 strategies = very low diversification.** 451 trades over ~7 years from 10 ETFs = high concentration risk.
3. **Volatile instruments.** USO, UNG, CORN, DBB are notoriously high-vol commodity ETFs. The 17.5% max drawdown is significant relative to starting equity of $5K.
4. **Multi-strategy Sharpe carries the combo.** Solo momentum_breakout Sharpe = 0.3759. The 0.906 depends on three strategies working simultaneously.
5. **No regime gating** (`regime_gate.enabled: false`). In bear commodity markets, the strategy fires without a filter.
6. **XLY universe drift** is live evidence of a ticker-assignment bug: XLY (consumer discretionary) appeared as a commodity_etfs position with strategy='unknown'. This means at some point an order was executed for a non-universe ticker.

---

## Paper Trade History

**NONE.** Zero paper trading before going live. The config was committed Apr 14 with `paper: false`. First trade (GLD, SLV) entered Apr 16. The system went directly from research → live in 2 days.

Current live trade history (commodity_etfs):
- **Total trades in DB:** 7
- **Date range:** Apr 16 – Apr 22 (6 calendar days)
- **Open positions:** GLD (since Apr 16, momentum_breakout) + UNG (since Apr 22, connors_rsi2)
- **Closed:** SLV (momentum_breakout, -$5.60 PnL × 3 records due to duplicate backfills)
- **Verdict:** Not enough live data to draw any conclusions. 6 days, 2 open positions, ~$5 in realized PnL.

Notable anomalies in the trade log:
- SLV trade id=136 entered Apr 16, then ids 157 and 178 re-entered SLV on Apr 21/22 at same price — suggests reconciler created duplicates
- UNG trades ids 150, 154 are duplicates (created Apr 21 at 09:00 and 16:00, both exited Apr 22 at same time)
- These duplicates point to reconciler backfill running multiple times on the same untracked position

---

## Three-Path Recommendation

### Path 1: Continue as Live (Viable, but fix gaps first)

**Prerequisites checklist:**

- [ ] **Add `execute_approved.py -m commodity_etfs` to crontab** at 23:15 AEST — currently plans are generated and approved but never executed through the proper pipeline. This is the #1 gap.
- [ ] **Add `sync_protective_orders.py --market commodity_etfs`** to crontab (every 15 min during US hours)
- [ ] **Fix `monitor/evaluator.py`**: add commodity_etfs to the market_id loop (line 333)
- [ ] **Update config params** to match research_best: `momentum_breakout.atr_stop_mult: 1.0`, `momentum_breakout.trend_ma_period: 30`, `momentum_breakout.lookback_days: 22`
- [ ] **Remove XLY from live state** or investigate how it got there; add universe-membership guard to reconciler backfill
- [ ] **Fix duplicate trade records** caused by reconciler running multiple backfills on same position
- [ ] **Run `validate_oos.py --market commodity_etfs`** to get a first OOS estimate

**ETA:** 1-2 days of fixes. Research is genuinely viable (Sharpe 0.906, p=0.0007, non-fragile MC). Missing infrastructure is the real blocker, not research quality.

---

### Path 2: Revert to Paper Validation First

**Setup needed:**
1. Set `config/active/commodity_etfs.json` → `alpaca.paper: true` + `trading.live_enabled: false`
2. Add an execute_approved cron for commodity_etfs pointing to paper mode
3. Run for 4–8 weeks (enough for ~20–30 commodity ETF trade cycles)
4. Compare realized PnL to backtest expectations

**Duration:** 6–8 weeks minimum for meaningful paper validation (commodity ETFs hold 5–20 days avg). Current 6-day live window is too short to judge.

**Tradeoff:** We have $5K reserved for this universe that's currently generating no risk-adjusted return waiting in paper mode. The cost of validation is opportunity cost vs. the risk of running live without proper infrastructure.

---

### Path 3: Dead End — Discontinue

**Arguments for:**
- Solo momentum_breakout Sharpe = 0.3759 — individually weak
- No OOS validation; in-sample numbers routinely deflate 30–50% OOS
- 10-ticker universe = very thin diversification
- Already generating anomalous trades (XLY, duplicate records)
- The 0.906 multi-strategy Sharpe is plausible but the infrastructure isn't ready to support it safely

**Arguments against:**
- p-value 0.000686 = statistically significant edge (not data-mined noise)
- Calmar 0.829 and Sortino 1.26 are decent risk-adjusted metrics
- The connors_rsi2 component (57.9% win rate, 190 trades) is robust
- Infrastructure gaps are fixable in 1–2 days

**Verdict on Path 3:** NOT recommended. The research quality clears a reasonable bar. The issue is operational, not fundamental.

---

### Stop nightly sweep for commodity_etfs?

**No.** The sweep is cheap (0.5h × 2 workers = 1h compute/night) and the results are improving. Top Sharpe rose from 0.906 (Apr 16) to 1.0923 (Apr 17) already. Keep it running.

---

## Default Recommendation

**Path 1 — Continue live, fix the three critical gaps first.**

Priority order:
1. **TODAY:** Add `execute_approved.py -m commodity_etfs` to crontab at 23:15 AEST. This is the most dangerous gap — plans are being approved but not executed through Atlas's controlled pipeline.
2. **TODAY:** Add `sync_protective_orders.py --market commodity_etfs` to crontab.
3. **TODAY:** Fix `monitor/evaluator.py` to include commodity_etfs in the market loop.
4. **THIS WEEK:** Update `config/active/commodity_etfs.json` momentum_breakout params to match research_best (atr_stop_mult=1.0, trend_ma_period=30, lookback_days=22).
5. **THIS WEEK:** Investigate and clean duplicate trade records; add universe-membership guard to reconciler backfill.
6. **OPTIONAL:** Run `validate_oos.py --market commodity_etfs` to establish a first OOS baseline.

**Rationale:** The strategy has genuine statistical edge (p=0.0007), the infrastructure is 90% there, and the system is already live with open positions. Reverting to paper now would require closing existing broker positions (GLD, UNG are open with stops active), which creates unnecessary friction. The operational gaps are fixable and don't require architectural changes — just cron entries and a one-line evaluator.py fix.

---

## Supporting Evidence Files

- Config: `/root/atlas/config/active/commodity_etfs.json` (Apr 14)
- Research best: `/root/atlas/research/best/momentum_breakout_commodity_etfs.json` (Apr 21)
- Live state: `/root/atlas/brokers/state/live_commodity_etfs.json` (9KB, active)
- Plans: `/root/atlas/plans/plan_commodity_etfs_2026-04-{14,15,17,20,21}.json`
- Priorities: `/root/atlas/config/research_priorities.json`
- Universe: `/root/atlas/universe/definitions.py:94` (commodity_etfs block)
- Execute_approved: `/root/atlas/scripts/execute_approved.py` (multi-market aware, just not scheduled for commodity_etfs)
- Monitor gap: `/root/atlas/monitor/evaluator.py:333`
- Portfolio limits: `/root/atlas/portfolio/limits.py:27`
- EOD settlement log showing reconcile activity: `/root/atlas/logs/eod_settlement.log`
