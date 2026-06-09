# Lifecycle Retirement Audit — 2026-05-14

## Summary

6 orphan LIVE entries retired from `strategy_lifecycle` table.
1 LIVE entry retained (open trade present, universe actively trading).

All retirements performed by operator `system` via `monitor.strategy_lifecycle.transition()`.
Transition records written to `strategy_lifecycle_history` (ids 33–38).

---

## Pre-Retirement Investigation

**All LIVE rows as of 2026-05-14T04:22 UTC** (7 total):

| strategy | universe | state | entered_state_at | prev_state |
|---|---|---|---|---|
| connors_rsi2 | commodity_etfs | LIVE | 2026-05-06T04:28:26 | NULL (migration seed) |
| mean_reversion | commodity_etfs | LIVE | 2026-05-06T04:28:26 | NULL |
| momentum_breakout | commodity_etfs | LIVE | 2026-05-06T04:28:26 | NULL |
| connors_rsi2 | gold_etfs | LIVE | 2026-05-06T04:28:26 | NULL |
| mean_reversion | sector_etfs | LIVE | 2026-05-06T04:28:26 | NULL |
| momentum_breakout | sector_etfs | LIVE | 2026-05-06T04:28:26 | NULL |
| momentum_breakout | sp500 | LIVE | 2026-05-06T04:28:26 | NULL |

**All 7 were seeded by the 2026-05-06 lifecycle-rollout migration** as "Migration: pre-existing live strategy at lifecycle rollout 2026-05-06".

---

## Decision Table

| strategy | universe | live_enabled in config? | open trades? | Decision | Rationale |
|---|---|---|---|---|---|
| connors_rsi2 | commodity_etfs | `False` (passive, mode=passive) | 0 | **RETIRE** | Universe disabled since #297 consolidation 2026-05-07. No active trading. |
| mean_reversion | commodity_etfs | `False` (passive, mode=passive) | 0 | **RETIRE** | Same as above. |
| momentum_breakout | commodity_etfs | `False` (passive, mode=passive) | 0 | **RETIRE** | Same as above. |
| connors_rsi2 | gold_etfs | CONFIG FILE NOT FOUND | 0 | **RETIRE** | No `config/active/gold_etfs.json` — universe is fully passive. Seeded as LIVE at rollout was an artifact. |
| mean_reversion | sector_etfs | `False` (passive, mode=passive) | 0 | **RETIRE** | Universe disabled since #297 consolidation 2026-05-07. No active trading. |
| momentum_breakout | sector_etfs | `False` (passive, mode=passive) | 0 | **RETIRE** | Same as above. |
| momentum_breakout | sp500 | `True` (live_enabled=True) | 1 (CAT, trade id=187) | **KEEP LIVE** | sp500 is live; open CAT position active; LIVE state correct. |

**Decision logic applied:**
- `enabled=False` AND `live_enabled=False` AND `open_trades=0` → RETIRE
- `open_trades > 0` → DO NOT RETIRE
- `enabled=True` AND `live_enabled=True` → keep LIVE

---

## Retirements Performed

All 6 transitions: `LIVE → RETIRED` via `monitor.strategy_lifecycle.transition()`,
operator=`system`, reason written to `strategy_lifecycle_history`.

| # | strategy | universe | history_id | transitioned_at | reason |
|---|---|---|---|---|---|
| 1 | connors_rsi2 | commodity_etfs | 33 | 2026-05-14T04:22:14 | passive universe (live_enabled=False since #297 consolidation 2026-05-07); 0 open trades; no active config strategies enabled |
| 2 | mean_reversion | commodity_etfs | 34 | 2026-05-14T04:22:14 | passive universe (live_enabled=False since #297 consolidation 2026-05-07); 0 open trades; no active config strategies enabled |
| 3 | momentum_breakout | commodity_etfs | 35 | 2026-05-14T04:22:14 | passive universe (live_enabled=False since #297 consolidation 2026-05-07); 0 open trades; no active config strategies enabled |
| 4 | connors_rsi2 | gold_etfs | 36 | 2026-05-14T04:22:14 | no active config file for gold_etfs (passive); 0 open trades; migrated-LIVE at lifecycle rollout was an artifact |
| 5 | mean_reversion | sector_etfs | 37 | 2026-05-14T04:22:14 | passive universe (live_enabled=False since #297 consolidation 2026-05-07); 0 open trades; no active config strategies enabled |
| 6 | momentum_breakout | sector_etfs | 38 | 2026-05-14T04:22:14 | passive universe (live_enabled=False since #297 consolidation 2026-05-07); 0 open trades; no active config strategies enabled |

---

## Post-Retirement Verification

Query run after all transitions:
```
SELECT strategy, universe, state FROM strategy_lifecycle WHERE state='LIVE';
```

Result: **1 row — momentum_breakout / sp500 / LIVE** (correct; open trade CAT id=187).

All 6 retired entries now show `state='RETIRED'` with `entered_state_at='2026-05-14T04:22:14'`.

---

## Root Cause of Orphan Entries

All 6 orphan entries share the same migration timestamp `2026-05-06T04:28:26` and reason
"Migration: pre-existing live strategy at lifecycle rollout 2026-05-06". This batch seeded
7 strategies as LIVE based on the at-the-time active universe configs. However, the
2026-05-07 consolidation (#297) subsequently disabled `commodity_etfs` and `sector_etfs`
without triggering lifecycle transitions for those universes. The gap was not caught
because the lifecycle system was new (launched 2026-05-06) and #297 ran the day after.

**Prevention**: Task #348 / lifecycle audit should be run after any universe mode change.
The lifecycle API (`/api/strategy-lifecycle`) should be reviewed as part of any
`live_enabled: false` config change.

---

## Not Affected

- `connors_rsi2/sp500` → already demoted to PAPER (commit `636d3c8d`, 2026-05-14)
- `mean_reversion/sp500` → PAPER (promoted from RESEARCH, 2026-05-14)
- `short_term_mr/sp500` → PAPER (dogfood since 2026-05-06)
- All `RESEARCH` entries → unchanged
