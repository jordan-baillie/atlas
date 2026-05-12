# Phase 1 Classifier Validation — 2026-04-29 15:04 UTC

**Replay window:** last 30 days
**Total errors:** 637
**Verdict:** **PASS**
**Effective IGNORE rate:** 94.03% (IGNORE 94.03% + IGNORE_PENDING_CLEAR 0.00%)
**Mandate gate:** ≥94.00% IGNORE → PASS

## Distribution

| Class | Count | Pct |
|---|---:|---:|
| IGNORE | 599 | 94.03% |
| IGNORE_PENDING_CLEAR | 0 | 0.00% |
| ESCALATE_DEFERRED | 2 | 0.31% |
| ESCALATE | 36 | 5.65% |
| ASSIST | 0 | 0.00% |
| AUTO_FIX | 0 | 0.00% |

## Top 20 rules fired

| Rule ID | Count |
|---|---:|
| `ignore_pattern:Circuit breaker tripped` | 427 |
| `ignore_pattern:Execution blocked: Plan status is` | 140 |
| `never_fix.msg:halt` | 33 |
| `ignore_pattern:Execution blocked: Not connected` | 32 |
| `never_fix.msg:broker` | 3 |
| `market_hours_defer` | 2 |

## By service

| Service | IGNORE | ESCALATE | ASSIST | DEFERRED | PENDING_CLEAR |
|---|---:|---:|---:|---:|---:|
| `live_executor` | 599 | 33 | 0 | 0 | 0 |
| `eod_settlement` | 0 | 3 | 0 | 0 | 0 |
| `strategy_health` | 0 | 0 | 0 | 2 | 0 |

## Samples by classification

### IGNORE

- [2026-04-07 14:28:03] `live_executor` — Circuit breaker tripped
    rule=`ignore_pattern:Circuit breaker tripped`, reason=Known-noise pattern: 'Circuit breaker tripped'
- [2026-04-07 14:28:19] `live_executor` — Circuit breaker tripped
    rule=`ignore_pattern:Circuit breaker tripped`, reason=Known-noise pattern: 'Circuit breaker tripped'
- [2026-04-07 14:28:19] `live_executor` — Circuit breaker tripped
    rule=`ignore_pattern:Circuit breaker tripped`, reason=Known-noise pattern: 'Circuit breaker tripped'
- [2026-04-07 14:28:19] `live_executor` — Circuit breaker tripped
    rule=`ignore_pattern:Circuit breaker tripped`, reason=Known-noise pattern: 'Circuit breaker tripped'
- [2026-04-07 14:28:19] `live_executor` — Circuit breaker tripped
    rule=`ignore_pattern:Circuit breaker tripped`, reason=Known-noise pattern: 'Circuit breaker tripped'

### ESCALATE

- [2026-04-07 15:09:50] `live_executor` — Execution blocked: HALTED: Manual emergency halt
    rule=`never_fix.msg:halt`, reason=NEVER list message pattern: 'halt'
- [2026-04-07 15:11:41] `live_executor` — Execution blocked: HALTED: Manual emergency halt
    rule=`never_fix.msg:halt`, reason=NEVER list message pattern: 'halt'
- [2026-04-08 02:16:21] `live_executor` — Execution blocked: HALTED: Manual emergency halt
    rule=`never_fix.msg:halt`, reason=NEVER list message pattern: 'halt'
- [2026-04-08 02:19:36] `live_executor` — Execution blocked: HALTED: Manual emergency halt
    rule=`never_fix.msg:halt`, reason=NEVER list message pattern: 'halt'
- [2026-04-08 02:20:54] `live_executor` — Execution blocked: HALTED: Manual emergency halt
    rule=`never_fix.msg:halt`, reason=NEVER list message pattern: 'halt'

### ESCALATE_DEFERRED

- [2026-04-07 15:09:56] `strategy_health` — mean_reversion has been DEGRADED for 4 consecutive weekly checks — immediate review needed
    rule=`market_hours_defer`, reason=Market hours active — defer classification to off-hours
- [2026-04-07 15:11:47] `strategy_health` — mean_reversion has been DEGRADED for 4 consecutive weekly checks — immediate review needed
    rule=`market_hours_defer`, reason=Market hours active — defer classification to off-hours
