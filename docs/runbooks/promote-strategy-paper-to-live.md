# Runbook: Promote Strategy Paper → Live

**Status**: FUTURE WORKFLOW. Paper executor not yet implemented.  
See `tasks/strategy_lifecycle_remaining.md` for implementation spec.

This runbook describes:
1. How to manually interact with promotion states TODAY (before paper executor ships)
2. The FUTURE automated workflow once paper executor is built

---

## Current State: Manual Operations

The `strategy_lifecycle` table is seeded and tracked. Automated paper-trading
is deferred. Until the paper executor ships, use these manual procedures.

### Check current promotion state

```sql
-- All strategies and their states
SELECT strategy, universe, state, entered_state_at, transition_reason
FROM strategy_lifecycle
ORDER BY universe, strategy;

-- All LIVE strategies
SELECT strategy, universe, entered_state_at
FROM strategy_lifecycle
WHERE state = 'LIVE';

-- History for a specific strategy
SELECT from_state, to_state, transitioned_at, reason, operator
FROM strategy_lifecycle_history
WHERE strategy = 'momentum_breakout' AND universe = 'sp500'
ORDER BY transitioned_at;
```

Via Python REPL:

```python
import sys; sys.path.insert(0, '/root/atlas')
from monitor.strategy_lifecycle import get_state, list_state, PromotionState

# Check one strategy
print(get_state("momentum_breakout", "sp500"))

# All in RESEARCH
for row in list_state(PromotionState.RESEARCH):
    print(row['strategy'], row['universe'], row['entered_state_at'])
```

### Manually transition a strategy (operator override)

> **Always provide a reason.** The reason is stored in history and is the primary audit trail.

```python
import sys; sys.path.insert(0, '/root/atlas')
from monitor.strategy_lifecycle import transition, PromotionState

# Example: manually advance to LIVE (bypasses graph — logs a warning)
transition(
    strategy="short_term_mr",
    universe="sp500",
    new_state=PromotionState.LIVE,
    reason="Manual promotion: OOS Sharpe 1.27, 294 trades, reviewed by Alice 2026-06-01",
    operator="alice",  # Any non-'system' value bypasses graph with warning
)

# Example: decommission a strategy
transition(
    strategy="trend_following",
    universe="sp500",
    new_state=PromotionState.RETIRED,
    reason="Consistently SUSPENDED in health lifecycle for 90 days — decommissioned",
    operator="manual",
)
```

### Emergency: revert to previous state

There is no automatic revert. To undo a transition:

```python
# Check history first
from db.atlas_db import get_db
with get_db() as db:
    rows = db.execute(
        "SELECT * FROM strategy_lifecycle_history "
        "WHERE strategy=? AND universe=? ORDER BY transitioned_at DESC LIMIT 5",
        ("momentum_breakout", "sp500"),
    ).fetchall()
    for r in rows:
        print(dict(r))

# Then apply the corrective transition
from monitor.strategy_lifecycle import transition, PromotionState
transition(
    "momentum_breakout", "sp500",
    PromotionState.RESEARCH,
    reason="Reverting erroneous LIVE promotion — data error in OOS validation",
    operator="alice",
)
```

---

## Future Automated Workflow (once paper executor ships)

> Implementation spec: `tasks/strategy_lifecycle_remaining.md`

### Stage 1: RESEARCH → PAPER (automatic, via auto_promote)

**Trigger**: `scripts/auto_promote_paper_to_live.py` (or `research/autoresearch_nightly.py`)

**Gates** (same as current live promotion gates + paper-phase requirement):
- IS Sharpe ≥ 0.5 (from `research_best`)
- OOS Sharpe ≥ 0.3 (from `_run_oos_validation`)
- OOS trades ≥ 30
- CAGR ≥ 5%
- DSR pass
- Universe has ≥ 500 experiments across ≥ 90 days

**What happens**:
1. `transition(strategy, universe, PromotionState.PAPER, reason="auto_promote_gates_passed")`
2. Universe config `mode` set to `paper` in `config/active/<universe>.json`
3. `paper_start_date` recorded in `strategy_lifecycle`
4. Cron begins routing signals to Alpaca paper account

### Stage 2: PAPER → LIVE (automatic, after 30 days)

**Trigger**: `scripts/auto_promote_paper_to_live.py` (daily cron, runs ~08:30 AEST)

**Gates** (paper-phase specific):
- ≥ 30 calendar days in PAPER state
- Paper Sharpe gap vs research Sharpe < 0.5 (for all 30 days)
- Paper trade count ≥ 10
- No consecutive-divergence alert in last 7 days

**What happens**:
1. `transition(strategy, universe, PromotionState.LIVE, reason="auto_promote_paper_to_live")`
2. Universe config `mode` changed from `paper` to `live`
3. `paper_end_date` recorded in `strategy_lifecycle`
4. Telegram alert: "✅ {strategy}/{universe} graduated to LIVE after {N}d paper phase"

### Stage 3: PAPER → RESEARCH (auto-rollback)

**Trigger**: `scripts/check_live_research_divergence.py` (Rec 4 divergence monitor)

**Condition**: Sharpe gap > 0.5 for 5 consecutive days while in PAPER state

**What happens**:
1. `transition(strategy, universe, PromotionState.RESEARCH, reason="paper_phase_divergence_rollback")`
2. `config/active/<universe>.json` reverts `mode` to `passive`
3. Telegram alert: "⚠️ {strategy}/{universe} rolled back to RESEARCH — paper divergence exceeded threshold"

### Stage 4: LIVE → PAPER (soft rollback, manual or via health system)

A LIVE strategy that enters SUSPENDED health state for N consecutive reports
may be soft-rolled-back to PAPER (trades paused, paper-only mode re-enabled)
rather than fully RETIRED.

```python
transition(
    "momentum_breakout", "commodity_etfs",
    PromotionState.PAPER,
    reason="Soft rollback: 4 consecutive SUSPENDED health reports — re-entering paper validation",
    operator="health_monitor",
)
```

---

## Monitoring

### Check lifecycle dashboard (future)

> Dashboard Controls tab will show promotion state badges. Deferred.

### Check via SQL today

```sql
-- Strategies approaching 30-day paper phase completion (future: when PAPER exists)
SELECT strategy, universe, paper_start_date,
       CAST(julianday('now') - julianday(paper_start_date) AS INTEGER) AS days_in_paper
FROM strategy_lifecycle
WHERE state = 'PAPER'
ORDER BY paper_start_date;

-- All LIVE strategies with their last transition date
SELECT strategy, universe, entered_state_at,
       CAST(julianday('now') - julianday(entered_state_at) AS INTEGER) AS days_live
FROM strategy_lifecycle
WHERE state = 'LIVE'
ORDER BY entered_state_at;
```

### Alerts

Once paper executor ships, Telegram alerts fire on:
- RESEARCH → PAPER (auto_promote): "📄 {strategy}/{universe} entering paper phase"
- PAPER → LIVE: "✅ {strategy}/{universe} graduated to LIVE"
- PAPER → RESEARCH: "⚠️ {strategy}/{universe} paper rollback — divergence"
- LIVE → RETIRED: "🛑 {strategy}/{universe} decommissioned"

---

## Related

- `docs/architecture/strategy-lifecycle.md` — state machine design
- `tasks/strategy_lifecycle_remaining.md` — deferred implementation spec
- `monitor/strategy_lifecycle.py` — Python API
- `monitor/lifecycle.py` — health lifecycle (separate concern)
