# Connors RSI2 — Disposition Memo
**Task #340 | Date: 2026-05-18 | Author: Atlas Batch Closer (Validation)**

---

## 1. TL;DR / Disposition Recommendation

> **DECOMMISSION** — disable connors_rsi2 in the next planning cycle.

The strategy shows no statistical edge across a 7-year backtest (Sharpe −0.51, p=0.631), a marginally positive live PnL of +$65.70 over 15 trades driven largely by reconciliation exits rather than intended signal logic, and a live win rate (46.7%) substantially below the already-marginal backtest win rate (58.8%). Capital and scheduler capacity are better deployed toward strategies with demonstrated positive Sharpe (momentum_breakout/commodity_etfs: 1.03, mean_reversion/commodity_etfs: 1.19). **If approved**, disable the strategy, free its position slots, and archive its config.

---

## 2. Evidence Summary Table

| Metric | Backtest (7yr, SP500, 198 tkrs) | Live (15 trades) | Gap / Note |
|---|---|---|---|
| Sharpe ratio | **−0.5133** | n/a (too few trades) | Solidly negative |
| CAGR | **−0.11%** | n/a | Near-zero, slightly negative |
| Max drawdown | **18.75%** | n/a | Meaningful downside exposure |
| Win rate | **58.81%** | **46.7%** (7/15) | Live is **−12 pp** vs backtest |
| Profit factor | **0.9982** | ~1.08 (surface) | Live superficially positive only due to one outlier trade (BKR +$72) |
| Trades | **789** (7yr) | **15** (2 months) | Live sample far too small for significance |
| p-value | **0.631** | n/a | Not statistically significant — cannot reject null of no edge |
| Total PnL | — | **+$65.70** | Dominated by BKR trade; ex-BKR live PnL = **−$6.46** |

**Key finding**: Profit factor of 0.9982 means the strategy destroys value in aggregate across 789 backtest trades. The p-value of 0.631 confirms this is indistinguishable from noise.

---

## 3. Live Trade Audit (15 closed trades)

### Full trade log

| Ticker | PnL $ | PnL % | Entry | Exit | Exit Reason |
|---|---|---|---|---|---|
| HCA | −26.40 | −2.59% | 2026-03-18 | 2026-03-19 | trailing_stop |
| BKR | +72.16 | +12.09% | 2026-03-16 | 2026-03-20 | signal |
| ECL | +19.16 | +1.86% | 2026-03-24 | 2026-03-25 | stop_loss (intraday rebound) |
| NOC | +7.75 | +1.16% | 2026-03-24 | 2026-03-25 | stop_loss (intraday rebound) |
| MSFT | −2.89 | −0.80% | 2026-03-27 | 2026-03-30 | broker_trailing_stop |
| HCA | +2.17 | N/A | 2026-04-01 | 2026-04-02 | signal_exit |
| MSI | +14.20 | +3.33% | 2026-04-01 | 2026-04-07 | signal_exit |
| UNG | 0.00 | 0.00% | 2026-04-21 | 2026-04-22 | reconcile_phantom |
| UNG | −2.70 | −0.47% | 2026-04-22 | 2026-04-24 | reconcile_fill |
| UNG | +14.57 | +2.35% | 2026-04-24 | 2026-04-28 | reconcile_fill |
| FCX | −15.71 | −5.11% | 2026-04-24 | 2026-04-29 | reconcile_fill |
| SLV | −15.26 | −3.73% | 2026-04-24 | 2026-04-29 | reconcile_fill |
| UNG | +14.57 | N/A | 2026-04-30 | 2026-05-01 | reconciled_orphan |
| FCX | −7.95 | −2.76% | 2026-05-05 | 2026-05-06 | reconcile_fill_cached |
| SYK | −7.97 | −2.70% | 2026-05-04 | 2026-05-09 | reconcile_fill_cached |

### Narrative analysis

**Exit quality breakdown:**
- `signal` / `signal_exit`: **2 trades** — the strategy's intended exit path
- `stop_loss` / `trailing_stop` / `broker_trailing_stop`: **4 trades** — stop-based exits (expected, not primary signal path)
- `reconcile_*` exits: **9 trades** — bookkeeping/reconciliation closures, NOT strategy-driven exits

Approximately **60% of live trades were closed by reconciliation logic**, not by the connors_rsi2 signal. The strategy's core exit logic (RSI2 returning from oversold) fired in only 2 of 15 closes. **We have not meaningfully tested live execution of the intended strategy behavior.**

**Outlier dependency**: Total live PnL of +$65.70 includes BKR at +$72.16. Ex-BKR, the 14 remaining trades aggregate to **−$6.46**. The headline PnL is not representative.

**Win rate divergence**: Backtest 58.8% vs live 46.7% (−12 pp). Given only 15 trades the divergence is within sampling noise, but the direction is consistent with the strategy underperforming expectations.

---

## 4. Open Position Correction ⚠️

The Task #340 brief stated that UNG and SLV are **"currently held"** by connors_rsi2. **This is stale and incorrect.**

Verified state as of 2026-05-18:
- `trades` table (status=open, strategy=connors_rsi2): **ZERO rows**
- `brokers/state/live_sp500.json`: **1 position — CAT (strategy: momentum_breakout)**
- `brokers/state/live_commodity_etfs.json`: **EMPTY**

Both UNG and SLV were closed weeks ago:
- UNG last closed: **2026-05-01** (exit: `reconciled_orphan`)
- SLV closed: **2026-04-29** (exit: `reconcile_fill`)

**No connors_rsi2 positions are currently open.** The task brief appears to reference state from mid-April. No position closure action is needed before decommission.

---

## 5. Cross-Strategy Context

From Task #326 clean solo backtest (7yr, 2019-06-20 → 2026-05-15):

| Strategy | Sharpe | Universe | Status |
|---|---|---|---|
| mean_reversion/commodity_etfs | **+1.19** | Commodity ETFs | ✅ Best performer |
| momentum_breakout/commodity_etfs | **+1.03** | Commodity ETFs | ✅ Strong |
| opening_gap | **−0.51** | SP500 | ⚠️ Borderline (similar to connors_rsi2) |
| **connors_rsi2** | **−0.5133** | SP500 | ❌ Negative, p=0.631 |
| mean_reversion (SP500) | **−0.82** | SP500 | ❌ Negative |
| consecutive_down_days | **−1.25** | SP500 | ❌ Negative |
| short_term_mr | **−1.26** | SP500 | ❌ Worst |

Connors RSI2 is not the worst SP500 strategy in the portfolio, but it sits in the negative-Sharpe cohort. The comparison against commodity ETF strategies (Sharpe >1.0) sharpens the opportunity cost: every position slot allocated to connors_rsi2 could instead run a strategy with demonstrated positive Sharpe.

---

## 6. Disposition Decision Rationale

### Recommendation: DECOMMISSION

Three independent lines of evidence converge:

**(a) No edge in 7-year backtest.**
Profit factor 0.9982 across 789 trades — essentially random. p-value of 0.631 means we cannot reject the null hypothesis of zero edge. This is the primary, most reliable signal. 7 years and 789 trades is a large enough sample to be dispositive.

**(b) Live sample is small and execution-quality is compromised.**
15 trades over ~2 months is statistically insufficient. More critically, 9 of 15 closes were reconciliation events — the strategy's own exit logic has not been exercised meaningfully. The +$65.70 total PnL rests almost entirely on BKR +$72. This is not evidence of a working strategy; it is noise atop a broken execution record.

**(c) Capital opportunity cost is real.**
Commodity ETF strategies (mean_reversion/commodity: 1.19 Sharpe, momentum_breakout/commodity: 1.03 Sharpe) are running with demonstrated positive expectancy. Position slots and scheduler capacity used by connors_rsi2 can be redeployed productively.

**Counterargument considered and rejected**: One could argue the reconcile exits are a data quality artifact and the strategy deserves a clean live test. This is technically correct in isolation, but (a) the backtest is unambiguous across 789 trades, (b) cleaning up execution quality is costly engineering work, and (c) there are better alternatives already validated. Expected value of further investment in connors_rsi2 is negative.

---

## 7. Recommended Next Steps

*Pending planning lead approval — no live config changes are made by this memo.*

1. **Disable connors_rsi2** in the next planning cycle — remove from live strategy rotation in `config/strategies.yaml` (or equivalent).

2. **Free position slots** — reallocate the 2–3 slots previously reserved for connors_rsi2 to `momentum_breakout/commodity_etfs` (Sharpe 1.03) or `mean_reversion/commodity_etfs` (Sharpe 1.19).

3. **Archive config and backtest artifacts** — move to `archive/strategies/connors_rsi2/` to preserve the record without cluttering the active strategy space.

4. **Update task tracker** — close Task #340 as RESOLVED (DECOMMISSION). Mark connors_rsi2 as archived in Task #326 cross-strategy ranking.

5. **No immediate position action required** — confirmed zero open connors_rsi2 positions. Clean slate at decommission time.

---

## 8. Caveats

- **Small live sample**: 15 trades is statistically insufficient for confident live-performance conclusions. The decommission recommendation rests primarily on the 7-year backtest.

- **Reconcile-dominated exits**: ~60% of live closes were reconciliation events, not strategy-logic exits. We have not observed the intended mean-reversion entry→signal-exit cycle operating cleanly at scale. Live data is less informative than it appears — in either direction.

- **Market regime**: The live period (March–May 2026) included elevated volatility. However, the 7-year backtest spans multiple regimes (2020 crash, 2022 rate cycle, 2023 recovery) and remains negative throughout.

- **Commission impact**: Backtest used $0 commission. Real-world costs would further depress an already-negative profit factor of 0.9982.

- **This memo is recommendations only.** No live configuration has been modified. All recommendations are contingent on planning lead approval.

---

*Generated by Atlas Batch Closer (Validation team) | Task #340 | 2026-05-18*

## Resolution — 2026-05-18

- **Option taken:** 1A (per-market decommission on sp500 only)
- **Date:** 2026-05-18
- **Commit SHA:** 10d7c5e7391d066bf25b746b7bbcdc4dc16de678
- **What was preserved:** connors_rsi2 strategy retained at framework level (still in `research/vectorised_sweep.py` `ACTIVE_STRATEGIES` list). `config/active/commodity_etfs.json` entry untouched (market is passive). `config/active/sector_etfs.json` entry already disabled — no change needed.
- **Why preserved:** Task #252 ETF research standings flagged `gold_etfs/connors_rsi2` as STRONG promotion candidate (Sharpe 0.7588 ↑+0.888 trend over 51 research runs) — leaving the door open for a future per-universe promotion once `config/active/gold_etfs.json` scaffolding exists.
- **Note:** This section was initially written with a placeholder SHA. After commit, run: `sed -i "s/10d7c5e7391d066bf25b746b7bbcdc4dc16de678/<actual_sha>/" docs/decisions/2026-05-18-connors-rsi2-disposition.md`, then `git add docs/decisions/2026-05-18-connors-rsi2-disposition.md && git commit --amend --no-edit`.
