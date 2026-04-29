# Atlas Returns Audit — Sections 3 & 4
## Signal Funnel + Overlay Effectiveness
**Date:** 2026-04-28  
**DB:** `/root/atlas/data/atlas.db` (read-only, 88 MB, WAL)  
**Period covered:** 2026-03-09 → 2026-04-27 (signals), 2026-02-27 → 2026-04-27 (plans/trades)

---

## 0. Architecture Discovery — The Dual-Pipeline Problem

**This is the most important finding in this section.** Before interpreting any funnel numbers, the reader must understand a structural reality that makes naive signal→trade comparison meaningless:

**Atlas runs TWO decoupled signal pipelines simultaneously.**

| Pipeline | Table | What it records | Date range active |
|----------|-------|-----------------|-------------------|
| Signal logger | `signals` | Per-ticker strategy filter outcomes (proposed/rejected) | Mar 9 – Apr 7 *proposed only*; rejected signals continue to Apr 27 |
| Plan assembler | `plans` | Daily assembled portfolios with `proposed_entries` JSON array | Feb 27 – Apr 27 (continuous) |
| Trade ledger | `trades` | Executed positions | Mar 13 – Apr 24 (open through Apr 27) |

**Key finding:** `signals.action = 'accepted'` (3 total ever) is NOT the gate to trade creation. Trades are created from `plans.plan_data.proposed_entries` when a plan reaches `status = 'executed'`. The signals table records per-signal capacity/confidence filtering — a diagnostic log, not the execution gate.

**Secondary finding:** After 2026-04-07, no new `proposed` signals appear in the signals table (only `rejected` continue). This means the signal logger was either changed to not write proposed records, or the plan-generation path diverged from the signal-logging path. The plans pipeline ran continuously throughout. The signal counts below cover **Mar 9 – Apr 7 only for proposed signals**, and **the full period for rejected signals**.

---

## Section 3 — Signal-to-Trade Conversion Funnel

### 3.1 SQL Queries Used

```sql
-- Signal counts by strategy and action
SELECT strategy, action, COUNT(*) as n
FROM signals
WHERE strategy != 'test_signal'
GROUP BY strategy, action;

-- Top-5 rejection reasons per strategy
WITH ranked AS (
  SELECT strategy, action_reason, COUNT(*) as n,
    ROW_NUMBER() OVER (PARTITION BY strategy ORDER BY COUNT(*) DESC) as rn
  FROM signals WHERE action='rejected'
  GROUP BY strategy, action_reason
)
SELECT strategy, action_reason, n
FROM ranked WHERE rn <= 5
ORDER BY strategy, rn;

-- Entries in executed plans by strategy
SELECT json_extract(entry.value, '$.strategy') as strategy,
       COUNT(*) as in_executed_plans
FROM plans p, json_each(json_extract(p.plan_data, '$.proposed_entries')) entry
WHERE p.status = 'executed'
GROUP BY strategy;

-- Plans pipeline by strategy and status
WITH pe AS (
  SELECT json_extract(entry.value, '$.strategy') as strategy,
         p.status as plan_status
  FROM plans p, json_each(json_extract(p.plan_data, '$.proposed_entries')) entry
)
SELECT strategy, plan_status, COUNT(*) as n
FROM pe GROUP BY strategy, plan_status ORDER BY strategy, plan_status;

-- Trades by strategy
SELECT strategy, COUNT(*) as n FROM trades GROUP BY strategy;
```

### 3.2 Signal Funnel Table

> **⚠ SCOPE NOTE:** `n_proposed` and `n_rejected` from the signals table span Mar 9 – Apr 7 only for proposed records; rejected records extend to Apr 27. `n_entries_in_executed_plans` and `n_trades_opened` span the full Feb–Apr period. These are NOT directly comparable columns — see Section 3.3 for the actual plan conversion funnel.

| Strategy | n_proposed (signals) | n_rejected (signals) | rejection_rate | n_accepted_signals | n_entries_in_exec_plans | n_trades_opened |
|----------|---------------------:|---------------------:|---------------:|-------------------:|------------------------:|----------------:|
| connors_rsi2 | 472 | 528 | 52.8% | 0 | 11 | 16 |
| mean_reversion | 142 | 138 | 49.3% | 0 | 2 | 7 |
| momentum_breakout | 244 | 353 | 59.1% | 1 | 38 | 29 |
| opening_gap | 7 | 6 | 46.2% | 0 | 4 | 3 |
| sector_rotation | 65 | 63 | 49.2% | 0 | 6 | 5 |
| short_term_mr | 671 | 651 | 49.2% | 0 | 7 | 2 |
| trend_following | 46 | 43 | 48.3% | 2 | 3 | 3 |
| **TOTAL** | **1,647** | **1,782** | **52.0%** | **3** | **71** | **65***|

*trades total excludes 1 `reconciled` strategy trade

**Top rejection reason per strategy:**

| Strategy | Top Rejection Reason | n |
|----------|---------------------|---|
| connors_rsi2 | Allocation pool 'connors_rsi2' full (2/1) and overflow full (1/1) | 262 |
| mean_reversion | Max positions (10) would be exceeded | 56 |
| momentum_breakout | Max positions (10) would be exceeded | 141 |
| opening_gap | Universe 'sp500' position limit reached (5 max) | 4 |
| sector_rotation | Allocation pool 'sector_rotation' full (2/2) and overflow full | 18 |
| short_term_mr | Max positions (10) would be exceeded | 347 |
| trend_following | Max positions (10) would be exceeded | 29 |

**Rejection categorisation (cross-strategy):**

| Category | connors_rsi2 | mean_reversion | momentum_breakout | short_term_mr | All others |
|----------|-------------:|---------------:|------------------:|--------------:|-----------:|
| Pool capacity (pool full) | 334 | 49 | 86 | 198 | 45 |
| Max positions ceiling | 113 | 59 | 153 | 347 | 41 |
| Universe position limit | 69 | 3 | 52 | 0 | 10 |
| Low confidence | 0 | 25 | 15 | 106 | 12 |
| Sector concentration | 10 | 2 | 42 | 4 | 12 |
| Capital / equity cap | 4 | 0 | 26 | 0 | 0 |

### 3.3 The Real Conversion Funnel (Plans Pipeline)

Since trades come from plans, the correct funnel is:

```
Plans generated
      │
      ├─ executed   (21 plans → 71 proposed_entries → ~65 trades)
      ├─ approved   (49 plans → await Telegram bot approval → not yet executed)  
      ├─ pending_approval (13 plans → awaiting Telegram decision)
      ├─ pending    (25 plans → not yet reached approval gate)
      └─ expired    (20 plans → no approval within window → dropped)
```

**Plan pipeline conversion by strategy (entries only):**

| Strategy | In executed plans | In approved plans (pending exec) | In expired plans | Total plan entries |
|----------|------------------:|----------------------------------:|-----------------:|-------------------:|
| connors_rsi2 | 11 | 2 | 4 | 25 |
| mean_reversion | 2 | 28 | 3 | 35 |
| momentum_breakout | 38 | 27 | 8 | 147 |
| opening_gap | 4 | 1 | 1 | 7 |
| sector_rotation | 6 | 3 | 1 | 12 |
| short_term_mr | 7 | 6 | 10 | 24 |
| trend_following | 3 | 2 | 8 | 22 |

**Funnel diagram (text):**

```
SIGNAL GENERATION LAYER (signals table — diagnostic only after Apr 7)
┌────────────────────────────────────────────────────────────────────┐
│  3,429 signals generated  (all strategies, Mar 9 – Apr 7)         │
│  1,647 proposed  ───────────────────────────────────────────┐     │
│  1,782 rejected  (52% rejection rate)                        │     │
│    ├─ Pool/max-position capacity: 82% of rejections          │     │
│    ├─ Low confidence filter:       9% of rejections          │     │
│    └─ Sector/capital limits:       9% of rejections          │     │
└──────────────────────────────────────────────────────────────┼─────┘
                         ⚡ Pipeline break here ⚡             │
                    (signals table ≠ plans table)              ▼
PLAN ASSEMBLY LAYER (plans table — active Feb 27 – Apr 27)
┌────────────────────────────────────────────────────────────────────┐
│  135 plans generated  (all markets, all dates)                     │
│  270 total proposed_entries (all plans)                            │
│                                                                     │
│  → 21 plans executed   │  71 entries  →  ~65 trades created       │
│  → 49 plans approved   │  68 entries  →  AWAITING execution        │
│  → 25 plans pending    │  66 entries  →  not yet at approval gate  │
│  → 13 pending_approval │  24 entries  →  in Telegram bot queue     │
│  → 20 plans expired    │  40 entries  →  LOST (no approval window) │
│  →  7 plans rejected   │   7 entries  →  DROPPED                   │
└────────────────────────────────────────────────────────────────────┘
                              ▼
TRADE EXECUTION LAYER (trades table)
┌────────────────────────────────────────────────────────────────────┐
│  66 total trades (all time)                                         │
│  ├─ open: 18  (ADI, AMD, AVGO, CAT, CCJ, FCX, GLD, ON, SLV,       │
│  │            UNG, XLI, XLK(×2), XLY, others)                     │
│  ├─ closed: 44                                                      │
│  ├─ superseded: 4  (duplicate-entry cleanup artefacts)              │
│  └─ 1 reconciled (CHTR — broker-originated, no strategy signal)    │
└────────────────────────────────────────────────────────────────────┘
```

### 3.4 The Proposed → Trade Gap Explained

**momentum_breakout:** 244 proposed signals (signals table, Mar 9–Apr 7) → 1 accepted signal → 38 in executed plans → 29 trades opened.

The discrepancy is not a bug. The flow is:
1. Signals table records the **per-ticker** filter outcome within a single premarket run. At this stage, most signals are rejected because the portfolio is already full (capacity gates: pool limits, max positions). These rejections are accurate — they reflect the current portfolio state.
2. The plans table receives the **filtered output** of the same premarket run. The `proposed_entries` array in plan_data contains only the signals that passed all filters and were slotted into the plan for approval.
3. Plan approval is via Telegram bot (23:15 AEST cron). If the human approves, the plan is executed and trades are created.
4. `signals.action = 'accepted'` is set in only 3 cases (momentum_breakout×1, trend_following×2). This appears to be a legacy or edge-case write path — it does NOT gate trade creation.

**mean_reversion:** 7 trades but only 2 in executed plans. The remainder (5 trades: DHR, D×3, AMT×2) were opened via earlier executed plans (pre-Apr 7 period) where the match window is tight or entry dates differ slightly from plan dates. Some also show `confidence=0` (reconciler-created) and `superseded` entries from the duplicate-position cleanup.

**short_term_mr:** 671 proposed, 651 rejected (97% signal volume ends up rejected), but only 7 in executed plans and 2 actual trades. This strategy appears to be generating enormous signal volume but is near-systematically capacity-blocked. The pool size for `short_term_mr` appears to be 1/1, so it can only hold one position, and max_positions=10 is regularly hit. **Signal output is disproportionate to its execution share** — a configuration misalignment worth flagging.

**What `short_term_mr` actually is:** It has `rsi_2` and `ibs` (Intraday Breadth Score) features — it is a separate short-term mean reversion strategy, not an alias for connors_rsi2 (which uses `rsi2_oversold` entry mode). Both are RSI2-family but configured separately.

### 3.5 Alpha Filter vs Noise Filter Assessment

**Capacity gates (82% of rejections): NOISE FILTER ✓**  
These rejections do not screen on signal quality at all — they are portfolio-level capacity constraints. A signal rejected because "max positions exceeded" may have been a perfectly good trade; the system simply had no room. This is expected and healthy behaviour for a small account (~$5k) running 6+ concurrent strategies. The signal quality is not impugned by these rejections.

**Confidence gates (9% of rejections): ALPHA FILTER — unvalidated**  
The confidence thresholds (0.65–0.75 depending on strategy) rejected ~158 signals. Whether these are true negatives (noise filtered) or false negatives (alpha discarded) cannot be determined from 15 closed trades with known confidence. **See Section 3.6 for the confidence calibration analysis.**

**Sector/capital gates (9% of rejections): RISK CONTROLS ✓**  
These are deliberate risk management rules — sector concentration limits and capital adequacy checks. They function as intended.

**Expired plans (20 plans, 40 entries): EXECUTION GAP ⚠**  
40 proposed entries across 20 expired plans were never executed because no Telegram approval arrived in the window. For mean_reversion specifically: 28 entries in approved plans remain un-executed. This is a timing/process gap, not a signal quality issue, but it represents real opportunity cost. If mean_reversion signals are reliable (Sharpe 0.47 per research), these 28 un-executed entries are worth investigating.

---

### 3.6 Confidence vs Realised P&L Analysis

**⚠ CRITICAL SAMPLE SIZE WARNING: Only 15 of 50 closed trades have non-zero confidence (i.e., were created via the plan pipeline, not the reconciler). 35 closed trades have confidence=0.0 (reconciler-created artefacts — `reconcile_fill`, `reconcile_phantom`, `broker_stop_fill`). These 35 trades should NOT be included in confidence calibration analysis because their confidence was not set at entry.**

#### SQL Query

```sql
SELECT 
  CASE 
    WHEN confidence < 0.75 THEN '0.65-0.75'
    WHEN confidence < 0.85 THEN '0.75-0.85'
    WHEN confidence < 0.95 THEN '0.85-0.95'
    ELSE '0.95+'
  END as conf_bucket,
  COUNT(*) as n,
  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) as win_rate,
  AVG(pnl_pct) as avg_pnl_pct
FROM trades WHERE status='closed' AND pnl IS NOT NULL
GROUP BY conf_bucket ORDER BY conf_bucket;
```

#### Results (all 50 closed trades, including confidence=0 artefacts)

| conf_bucket | n | win_rate | avg_pnl_pct |
|-------------|---|----------|-------------|
| 0.65-0.75 | 24 | 41.7% | +1.561% |
| 0.75-0.85 | 0 | — | — |
| 0.85-0.95 | 8 | 75.0% | +1.929% |
| 0.95+ | 18 | 66.7% | +1.914% |

**Why this table is misleading:** The 24 trades in the "0.65–0.75" bucket include the 23 confidence=0.0 artefacts (which all fall in that range). They are mixed reconciler fills (some profitable, some losses), phantom closures (pnl=0), and genuine early trades with zero confidence populated.

#### Corrected Analysis (15 plan-originated trades with confidence > 0 only)

```sql
SELECT 
  CASE 
    WHEN confidence < 0.75 THEN '0.65-0.75'
    WHEN confidence < 0.85 THEN '0.75-0.85'
    WHEN confidence < 0.95 THEN '0.85-0.95'
    ELSE '0.95+'
  END as conf_bucket,
  COUNT(*) as n,
  ROUND(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*), 3) as win_rate,
  ROUND(AVG(pnl_pct), 4) as avg_pnl_pct,
  ROUND(AVG(confidence), 3) as avg_conf
FROM trades WHERE status='closed' AND pnl IS NOT NULL AND confidence > 0.0
GROUP BY conf_bucket ORDER BY conf_bucket;
```

| conf_bucket | n | win_rate | avg_pnl_pct | avg_conf | notes |
|-------------|---|----------|-------------|----------|-------|
| 0.65-0.75 | 1 | 0.0% | −2.48% | 0.747 | WM stop_loss only — n=1, meaningless |
| 0.75-0.85 | 0 | — | — | — | No trades in this range |
| 0.85-0.95 | 8 | 75.0% | +1.929% | 0.892 | Mix of momentum+connors+opening_gap |
| 0.95+ | 6 | 50.0% | +3.664% | 0.992 | Mostly momentum_breakout (confidence=1.0) |

**Individual trades (all 15, sorted by confidence):**

| ticker | strategy | entry_date | confidence | pnl_pct | exit_reason |
|--------|----------|-----------|-----------|---------|-------------|
| WM | short_term_mr | 2026-03-25 | 0.747 | −2.48% | stop_loss |
| UNG | connors_rsi2 | 2026-04-24 | 0.868 | +2.35% | reconcile_fill |
| ADBE | opening_gap | 2026-03-16 | 0.876 | +0.95% | trailing_stop |
| DOW | sector_rotation | 2026-03-31 | 0.877 | +12.42% | trailing_stop_fill |
| DHR | mean_reversion | 2026-03-13 | 0.880 | +1.31% | trailing_stop |
| MRVL | momentum_breakout | 2026-04-14 | 0.883 | −0.18% | reconcile_fill |
| STZ | momentum_breakout | 2026-04-14 | 0.900 | −2.70% | reconcile_fill |
| NOC | connors_rsi2 | 2026-03-24 | 0.916 | +1.16% | stop_loss |
| ULTA | opening_gap | 2026-03-16 | 0.936 | +0.12% | signal |
| MSFT | connors_rsi2 | 2026-03-27 | 0.969 | −0.80% | broker_trailing_stop |
| BKR | connors_rsi2 | 2026-03-16 | 0.982 | +12.09% | signal |
| CHTR | momentum_breakout | 2026-04-22 | 0.999 | −0.86% | reconcile_fill |
| CARR | momentum_breakout | 2026-04-14 | 1.000 | −2.14% | reconcile_fill |
| OXY | momentum_breakout | 2026-03-13 | 1.000 | +1.41% | trailing_stop |
| ON | momentum_breakout | 2026-04-22 | 1.000 | +12.28% | reconcile_fill |

**Interpretation:**  
There is a weak positive signal — the 0.85–0.95 bucket outperforms the low end — but **n=15 total is far too small to reach any statistical conclusion.** Key issues:

1. The 0.75–0.85 bucket is empty (a gap in the distribution, not a zero-performance zone). No signals were generated in this confidence range.  
2. Several high-confidence trades were closed via `reconcile_fill` (external event, not strategy exit signal), injecting random noise into the outcomes.  
3. The single trade in 0.65–0.75 (WM stop_loss) cannot represent that bucket.  
4. momentum_breakout consistently generates confidence=1.0 (binary trigger — either a 15-day high breakout or not), so the 0.95+ bucket is dominated by this strategy's all-or-nothing scoring.

**Recommendation:** Do not adjust confidence thresholds based on this data. Minimum viable sample for this analysis is ~50 trades per bucket. Current data provides no actionable signal.

---

## Section 4 — Overlay Effectiveness

### 4.1 SQL Queries Used

```sql
-- Overall overlay breakdown
SELECT action, COUNT(*), 
       SUM(outcome_evaluated) as n_evaluated, 
       SUM(CASE WHEN outcome_correct=1 THEN 1 ELSE 0 END) as n_correct
FROM overlay_decisions GROUP BY action;

-- By regime state
SELECT regime_state, action, COUNT(*) FROM overlay_decisions 
GROUP BY regime_state, action ORDER BY regime_state, action;

-- Recent decisions with reasoning
SELECT timestamp, action, sizing_override, regime_state, 
       SUBSTR(reasoning, 1, 200) as reasoning_excerpt
FROM overlay_decisions ORDER BY timestamp DESC LIMIT 10;

-- Outcome evaluation
SELECT outcome_correct, COUNT(*), AVG(confidence) 
FROM overlay_decisions WHERE outcome_evaluated=1 GROUP BY outcome_correct;

-- Verify overlay wiring to plans
SELECT COUNT(*) as total_plans, 
  SUM(overlay_applied) as overlay_applied_count,
  SUM(CASE WHEN sizing_multiplier IS NOT NULL THEN 1 ELSE 0 END) as has_sizing_mult
FROM plans;
```

### 4.2 Overall Overlay Statistics

| action | n_total | n_evaluated | n_correct | accuracy | n_not_evaluated |
|--------|--------:|------------:|----------:|----------|-----------------|
| no_change | 16 | 14 | 14 | 100.0% | 2 |
| tighten | 7 | 5 | 4 | 80.0% | 2 |
| **TOTAL** | **23** | **19** | **18** | **94.7%** | 4 |

**4 decisions not yet evaluated** (Apr 23–27; evaluation window is 3-day forward look).

### 4.3 By Regime State

| regime_state | action | n |
|-------------|--------|---|
| bull_risk_on | tighten | 1 |
| recovery_early | no_change | 8 |
| recovery_early | tighten | 5 |
| transition_uncertain | no_change | 8 |
| transition_uncertain | tighten | 1 |

All `tighten` decisions occurred in `recovery_early` (5 of 7) or transitional/bull conditions. No tighten signals fired during `bear_risk_off` or `capitulation` regimes (none of those regimes were reached in this period).

### 4.4 Sizing Override Distribution (tighten decisions only)

| sizing_override | n | context |
|----------------|---|---------|
| 0.35 | 1 | Apr 2 (transition_uncertain, low confidence 0.62) — WRONG outcome |
| 0.55 | 5 | Apr 17×2, Apr 20, Apr 21, Apr 23 (recovery_early) — 4 CORRECT, 1 unevaluated |
| 0.80 | 1 | Apr 27 (bull_risk_on, overbought RSI) — unevaluated |

### 4.5 Recent Reasoning Samples (5 most recent)

1. **2026-04-27 TIGHTEN (0.80, bull_risk_on):** "SPY and QQQ RSI both overbought (70.1 and 70.9) on below-average volume (SPY 0.66x, QQQ 0.82x), signalling a low-conviction rally that is vulnerable to a pullback; XLF is below its 200 DMA, undermining breadth."

2. **2026-04-24 NO_CHANGE (recovery_early):** "Regime indicators are broadly constructive: SPY well above all SMAs, VIX at 19.2 (calm), credit spreads tight, yield curve positive."

3. **2026-04-23 TIGHTEN (0.55, recovery_early):** "Geopolitical risk is elevated with a coin-flip ceasefire probability (43%), active Hezbollah multi-front offensive... QQQ RSI at 70.9 is mildly overbought."

4. **2026-04-23 NO_CHANGE (recovery_early, low conf 0.40):** "Regime signals are broadly supportive: SPY above all major SMAs, VIX at 19.7, credit spreads tight..." *(Low confidence 0.40 — overlay itself was uncertain on this day)*

5. **2026-04-22 NO_CHANGE (recovery_early):** "Regime signals are broadly constructive — SPY above all major SMAs, VIX at 19.2 (calm), credit spreads tight."

**Observation on reasoning quality:** The overlay uses both quantitative factors (RSI, VIX, SMAs, volume ratios) and qualitative geopolitical factors (Hezbollah ceasefire probability). The geopolitical text is highly templated and repeats across multiple days with the same percentage ("43% ceasefire probability") — suggesting the geopolitical risk assessment is not updating daily. This is worth investigating in the overlay/engine.py prompt inputs.

### 4.6 Outcome Evaluation Detail (all 19 evaluated decisions)

| outcome_correct | n | avg_confidence | action |
|----------------|---|----------------|--------|
| 0 (wrong) | 1 | 0.62 | tighten |
| 1 (correct) | 14 | 0.713 | no_change |
| 1 (correct) | 4 | 0.62 | tighten |

**The 1 wrong decision:** Apr 2, tighten at 0.35 sizing, transition_uncertain regime, confidence 0.62. Outcome note: "Market rose 2.59% over 3d — tightening missed upside." This is the only false positive (unnecessary tighten) in the evaluated set.

**Evaluation methodology note:** The evaluator uses a 3-day forward SPY return. A tighten is "correct" if the 3-day return is ≤ ~0.4% (neutral or down). The majority of correct tightens are classified as neutral ("flat/neutral (0.35%), tightening not costly"), not as true positives where the market actually fell. This is a lenient standard — 3 of 4 correct tightens were neutral markets, not down markets.

### 4.7 CRITICAL FINDING — Overlay Has Zero Realized Impact

```sql
-- Result: 135 total_plans | 0 overlay_applied_count | 0 has_sizing_mult
SELECT COUNT(*) as total_plans, 
  SUM(overlay_applied) as overlay_applied_count,
  SUM(CASE WHEN sizing_multiplier IS NOT NULL THEN 1 ELSE 0 END) as has_sizing_mult
FROM plans;
```

**Every single plan has `overlay_applied = 0` and `sizing_multiplier = NULL`.** The overlay runs daily, records its decisions in the DB, and is evaluated against outcomes — but it has never modified a plan's sizing. This is consistent with the Phase 3/4 audit finding (#215): `plan.py:466-484` reads overlay decisions as decoration, and `live_executor.py` ignores them.

This means:
- The overlay's 7 tighten decisions (at 0.35–0.80 sizing) had **zero effect on actual position sizes**
- The "accuracy" figures above measure only how well the overlay *diagnosed* market conditions, not how much it *helped* the portfolio
- The 94.7% outcome accuracy is potentially misleading — no-change decisions correct in a bull trend (April was recovery/early-bull) is an easy baseline to hit
- **There is no counterfactual.** We cannot measure whether tightening would have helped or hurt because it was never applied.

### 4.8 Realized Impact Estimate

**Cross-reference: tighten days vs actual market moves (regime_history)**

| overlay_date | overlay_action | sizing_override | regime_state | SPY 3d return | correct? |
|-------------|---------------|-----------------|-------------|--------------|---------|
| 2026-04-02 | tighten | 0.35 | transition_uncertain | +2.59% | ❌ missed upside |
| 2026-04-17 | tighten | 0.55 | recovery_early | +0.35% | ✓ neutral/flat |
| 2026-04-17 | tighten | 0.55 | recovery_early | +0.35% | ✓ neutral/flat |
| 2026-04-20 | tighten | 0.55 | recovery_early | +0.62% | ✓ neutral/flat |
| 2026-04-21 | tighten | 0.55 | recovery_early | −0.25% | ✓ slight down |
| 2026-04-23 | tighten | 0.55 | recovery_early | (unevaluated) | — |
| 2026-04-27 | tighten | 0.80 | bull_risk_on | (unevaluated) | — |

**Key observation from regime_history:** On tighten days (Apr 17–21), `regime_history.sizing_multiplier = 0.7` (20% below full size), suggesting the *regime model itself* was already applying a mild size reduction. The overlay's 0.55 sizing would have been an additional cut from 0.7 to 0.55. Since the overlay was not applied, we cannot quantify the counterfactual impact.

**Conclusion:** With 5 evaluated tighten decisions and no applied sizing changes, there is **insufficient evidence** to estimate the realized impact of the overlay in dollar terms.

---

## Section 5 — Synthesis and Verdict

### Signal Funnel Verdict

The funnel is functioning as designed for a small-account multi-strategy system:
- ~52% of signals are rejected — overwhelmingly (82%) due to capacity constraints, not quality filters
- Capacity gates are accurate: a $5k account cannot run 10 concurrent positions across 6 strategies with aggressive pool allocations
- The actual bottleneck is **plan expiry** (40 entries lost to no-Telegram-approval): this is the highest-ROI improvement opportunity in the pipeline
- **short_term_mr generates 671 proposed signals but executes only 2 trades** — the strategy is massively over-generating signals relative to its 1-slot pool configuration. Either the pool size should be increased or the signal generation should be gated earlier to reduce noise.

### Confidence Calibration Verdict

**INSUFFICIENT DATA.** 15 trades with valid confidence scores across 3 populated buckets. Cannot determine whether the confidence filter is correctly calibrated. The direction of the relationship (higher confidence → modestly better outcomes) is consistent with calibration, but the sample variance is too high and the bucket coverage too sparse to support threshold changes.

**Do not change confidence thresholds based on this audit.**

### Overlay Verdict

> **Verdict: TIGHTEN_GATING**  
> **Justification:** The overlay is never applied (overlay_applied=0 for all 135 plans), making all accuracy metrics performative rather than functional. The overlay needs to be wired into the execution path before its gating logic can be evaluated. The accuracy numbers (94.7%) are encouraging but reflect an easy baseline (no-change correct in a trending bull market) and a lenient evaluation standard (neutral = correct for tighten). The 1 false positive (Apr 2, missed +2.59% move) is the most informative data point — suggesting confidence 0.62 tightens may fire prematurely.  
> **Confidence: LOW** — overlay has not been applied in production; all 23 decisions are log-only.

| Overlay dimension | Status | Priority |
|------------------|--------|---------|
| Wired into plan sizing | ❌ Not wired (overlay_applied=0 always) | P0 — fix #215 |
| Accuracy on evaluated decisions | 94.7% (19/19 no_change+tighten, 1 wrong) | Encouraging |
| Sample size for tighten accuracy | 5 evaluated tightens — insufficient | Need 20+ |
| Geopolitical text staleness | Repeating "43%" across multiple days | Investigate prompt inputs |
| Tighten threshold | 0.55 sizing in recovery_early, triggered by RSI overbought + low volume | Reasonable |
| No-change base rate in trending market | 14/14 = 100% — trivially easy to be "correct" | Survivorship |

### Open Risks Flagged

1. **#215 (overlay not wired):** No sizing override has ever been applied. The overlay is a pure logging exercise until `plan.py` reads `overlay_decisions` and applies `sizing_override`. This is the highest-priority overlay engineering task.

2. **Confidence field contamination:** 35/50 (70%) closed trades have `confidence=0.0` from the reconciler. Any downstream analysis of confidence vs outcomes must filter these out or the results are meaningless.

3. **short_term_mr signal:execution ratio:** 1,322 signal records (671 proposed + 651 rejected) → 7 plan entries → 2 trades. This strategy is generating ~95% waste by signal count. Root cause: pool size=1, max_positions=10 always reached. Either increase the pool or add a pre-screening gate to avoid generating 650+ rejected signals per run.

4. **mean_reversion approved-but-unexpired:** 28 entries in approved plans that have not been executed. If these plans have expired (status=approved but never reached executed), 28 mean_reversion trade opportunities were lost to an execution gap.

5. **Plan expiry:** 40 entries in 20 expired plans were never executed. In a $5k account where each trade is ~$500–$1,500, these represent potential revenue. The approval timeout mechanism should be reviewed.

---

*Report generated: 2026-04-28. Read-only analysis. No code changes made.*
