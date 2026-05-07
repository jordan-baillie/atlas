# Strategy Lifecycle — Promotion State Machine

**Last updated**: 2026-05-06  
**Owner**: Monitor / DB layers  
**Status**: Foundations shipped; paper-trading executor deferred (see [deferred spec](../../tasks/strategy_lifecycle_remaining.md))

---

## Why Two State Machines?

Atlas has **two orthogonal lifecycle concepts** for strategies. Conflating them causes bugs.

| Dimension | Module | States | Question answered |
|-----------|--------|--------|-------------------|
| **Promotion lifecycle** | `monitor/strategy_lifecycle.py` | `RESEARCH → PAPER → LIVE → RETIRED` | *Where in the activation pipeline is this (strategy, universe) combo?* |
| **Health lifecycle** | `monitor/lifecycle.py` | `RAMP_UP → ACTIVE → WATCH → PROBATION → SUSPENDED` | *Is this LIVE combo performing within acceptable bounds right now?* |

A strategy is first tracked by the **promotion** lifecycle. Only once it reaches `LIVE` does the **health** lifecycle become relevant. Both machines can be consulted simultaneously for the dashboard.

Example: `momentum_breakout/sp500` could simultaneously be:
- Promotion state: `LIVE` (it's trading real capital)
- Health state: `WATCH` (its Sharpe dropped below 50% of backtest)

---

## Promotion Lifecycle States

```
         ┌─────────────────────────────────────────────────────────┐
         │                                                         │
    ┌────▼────┐   auto_promote()   ┌───────┐  paper gates pass  ┌──────┐
    │RESEARCH │──────────────────▶│ PAPER │──────────────────▶ │ LIVE │
    └────┬────┘                   └───┬───┘                    └──┬───┘
         │                           │ paper gates fail          │
         │                           └──────────────────────────▶│
         │                                    (rollback)         │
         │                                                        │ manual
         │                                                        │ decom
         │                                                     ┌──▼──────┐
         └────────────────────────────────────────────────────▶│ RETIRED │
                                   (or via LIVE→RETIRED)       └──┬──────┘
                                                                   │ revival
                                                                   └──────▶ RESEARCH
```

### State Descriptions

| State | Meaning | Who enters | Who exits |
|-------|---------|-----------|-----------|
| `RESEARCH` | Discovered by research engine. Has a `research_best` row. Not yet validated in live market conditions. | Initial seed; auto-rollback from PAPER | auto_promote() → PAPER |
| `PAPER` | Trading on Alpaca paper account. Real market conditions, no capital at risk. 30-day minimum validation phase. **DEFERRED — not yet implemented.** | auto_promote() | Paper-promotion gate → LIVE; or divergence monitor → RESEARCH |
| `LIVE` | Trading real capital. Subject to health lifecycle monitoring. | Paper-promotion gate; legacy seed (pre-existing live strategies) | Manual decommission → RETIRED; soft rollback → PAPER |
| `RETIRED` | Decommissioned. No longer trading. Config set to `enabled: false`. | Manual decision | Revival → RESEARCH |

---

## Allowed System Transitions

Only `operator='system'` transitions are graph-enforced. `operator='manual'` bypasses the graph with a warning logged.

```python
ALLOWED_TRANSITIONS = {
    None:                  {RESEARCH, LIVE},           # initial seed only
    RESEARCH:              {PAPER, RETIRED},
    PAPER:                 {LIVE, RESEARCH, RETIRED},
    LIVE:                  {RETIRED, PAPER},            # PAPER = soft rollback
    RETIRED:               {RESEARCH},                  # revival path
}
```

---

## How the Three Sources Relate

```
research_best (SQLite)
  └─▶  strategy_lifecycle (SQLite) ◀──── config/active/*.json
                 │
                 ▼
         strategy_lifecycle_history (SQLite)
                 │
                 ▼
          (future) paper_trades table
                 │
                 ▼
           trades table (LIVE state only)
```

### Seeding rules (at lifecycle rollout — 2026-05-06)

1. **`config/active/<universe>.json` strategy with `enabled: true`** → seeded as `LIVE`.  
   These are pre-existing live strategies that were running before the lifecycle system existed.

2. **`research_best` row with `sharpe > 0`** (not already LIVE) → seeded as `RESEARCH`.  
   These strategies have been researched but have never traded real capital.

3. **`research_best` row with `sharpe ≤ 0`** → NOT seeded.  
   Negative-Sharpe strategies have no path to activation until re-researched.

**The migration is idempotent** — re-running does not overwrite existing rows.

---

## Persistence Layer

### `strategy_lifecycle` table

Primary key: `(strategy, universe)`. One row per active combo.

| Column | Purpose |
|--------|---------|
| `state` | Current promotion state (RESEARCH/PAPER/LIVE/RETIRED) |
| `entered_state_at` | ISO datetime of last state change |
| `prev_state` | State before this transition |
| `transition_reason` | Human-readable reason |
| `paper_start_date` | Set when entering PAPER state |
| `paper_end_date` | Set when leaving PAPER state |
| `auto_promotion_id` | Links to auto_promote audit trail |

### `strategy_lifecycle_history` table

Append-only audit log. One row per `transition()` call.

| Column | Purpose |
|--------|---------|
| `from_state` | NULL on initial seed |
| `to_state` | New state |
| `transitioned_at` | ISO datetime |
| `operator` | `'system'` for automated; username or `'manual'` for overrides |

---

## Python API

```python
from monitor.strategy_lifecycle import (
    PromotionState,
    get_state,
    transition,
    is_live,
    is_paper,
    list_state,
)

# Read current state
state = get_state("momentum_breakout", "sp500")  # → PromotionState.LIVE

# Transition (system — graph-enforced)
transition("new_strategy", "sp500", PromotionState.RESEARCH, reason="new discovery")
transition("new_strategy", "sp500", PromotionState.PAPER, reason="passed auto_promote gates")

# Manual override (bypasses graph — logged as warning)
transition("mr", "sp500", PromotionState.LIVE, reason="emergency", operator="alice")

# Convenience
is_live("momentum_breakout", "sp500")   # → True
is_paper("new_strategy", "sp500")       # → False

# List all strategies in a state
paper_combos = list_state(PromotionState.PAPER)
```

---

## Future Paper-Trading Workflow

> Paper executor is **DEFERRED**. See `tasks/strategy_lifecycle_remaining.md` for the full spec.

Once the paper executor is built, the promotion flow will be:

1. `auto_promote()` passes all gates (IS Sharpe ≥ 0.5, OOS Sharpe ≥ 0.3, OOS trades ≥ 30, CAGR ≥ 5%, DSR pass) → `transition(..., PAPER)`
2. Universe config `mode` set to `paper` — broker routes to Alpaca paper API
3. After 30 days: `scripts/auto_promote_paper_to_live.py` checks paper Sharpe vs research Sharpe (gap < 0.5) + absolute floor (≥ 0.3) → `transition(..., LIVE)`
4. Divergence monitor watches PAPER state: if gap > 0.5 for 5 consecutive days → `transition(..., RESEARCH)` (auto-rollback)

---

## Broker Routing Policy

The **broker routing policy** is the (mode, live_enabled, market_id, lifecycle) decision layer that routes execution and DB writes between live and paper paths. It lives at `brokers/routing_policy.py` as `BrokerRoutingPolicy`.

### Glossary

- **Broker routing policy** — the (mode, live_enabled, market_id, lifecycle) decisions that route execution and DB writes between live and paper paths. Encapsulated by `brokers/routing_policy.py::BrokerRoutingPolicy`.
- **Live pass** — execution against the real-money broker (`mode=live`).
- **Paper pass** — execution against the Alpaca paper account (`mode=paper`), used for PAPER-lifecycle strategies running alongside LIVE strategies in the same universe.
- **Lifecycle split** — partitioning plan entries by the originating strategy's promotion state (PAPER → paper executor; LIVE/RESEARCH/RETIRED/unknown → live executor).
- **Skip gate** — the universe-level check that bails out before any broker connection (`mode=passive` or `mode=live AND live_enabled=False`).

### Where it's used

| Caller | Purpose |
|--------|---------|
| `scripts/execute_approved.py` | Skip-gate, lifecycle split, paper-config |
| `scripts/sync_protective_orders.py` | Skip-gate, paper-pass detection, paper-config |
| `scripts/intraday_monitor.py` | Skip-gate, paper-pass detection (3 sites), paper-config |
| `scripts/eod_settlement.py` | Skip-gate, paper-pass detection, paper-config |
| `scripts/reconcile_ledger.py` | Mode-override patch, paper-pass detection |
| `brokers/live_executor.py` | Per-write-site paper/live discrimination, dedup-table selection |

### Key methods

- `policy.should_skip()` → bool — universe-level bail-out
- `policy.needs_paper_pass()` → bool — DB-backed, memoized
- `policy.split_entries_by_lifecycle(entries)` → `(live, paper)` — delegates to `monitor.strategy_lifecycle.split_trades_by_lifecycle`
- `policy.paper_config` → dict — paper-mode config patch (no mutation)
- `policy.for_paper()` → new `BrokerRoutingPolicy` — immutable mode transition
- `policy.is_paper` / `policy.is_live` / `policy.is_passive` → bool
- `policy.trade_table()` → `"paper_trades"` or `"trades"`
- `policy.protective_table()` → paper or live protective-orders table

Spec: [`docs/specs/broker-routing-policy.md`](../specs/broker-routing-policy.md)

---

## Related Files

| File | Role |
|------|------|
| `monitor/strategy_lifecycle.py` | Promotion state machine (this) |
| `monitor/lifecycle.py` | Health state machine (operational performance) |
| `db/atlas_db.py` | `get_lifecycle_state`, `set_lifecycle_state`, `list_lifecycle_states` |
| `db/schema.sql` | `strategy_lifecycle` + `strategy_lifecycle_history` DDL |
| `scripts/migrations/2026-05-06-seed-strategy-lifecycle.py` | Initial seeding migration |
| `tasks/strategy_lifecycle_remaining.md` | Deferred spec (sub-phases 1.2–1.5) |
| `docs/runbooks/promote-strategy-paper-to-live.md` | Operator runbook |
