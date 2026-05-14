# Atlas Research System

Continuous experiment pipeline for testing hypotheses, evaluating strategies, and promoting improvements to the active trading configuration.

## Directory Structure

```
research/
├── README.md              # This file
├── models.py              # Data models, file-locked I/O, queue/journal operations
├── __init__.py            # Public API
├── queue.json             # Prioritized experiment queue (shared state)
├── journal.json           # Append-only experiment results log
├── experiments/           # Per-experiment self-contained envelopes
│   └── exp-{id}.json     # Full inputs + outputs + metadata
├── strategies/            # Sandbox for candidate strategy code
│   └── *.py              # Strategies under test (not yet promoted)
└── candidates/            # (symlink idea) → config/candidates/
```

## Experiment Lifecycle

```
QUEUED → CLAIMED → RUNNING → EVALUATING → PASSED/FAILED/PARTIAL
                                              │
                                         PROMOTED / REJECTED / DEFERRED
```

| Status     | Meaning |
|------------|---------|
| queued     | Waiting for a worker to pick up |
| claimed    | Worker has reserved this experiment (prevents double-pickup) |
| running    | Backtest/optimization actively executing |
| evaluating | Results being analyzed by analyst role |
| passed     | Met acceptance criteria |
| failed     | Did not meet acceptance criteria |
| partial    | Some criteria met, interesting but not actionable yet |
| promoted   | Config promoted to active (after human approval) |
| rejected   | Human rejected the promotion request |
| deferred   | Parked for future reconsideration |

## Queue Categories (Priority Order)

1. **degradation** (P1) — Fix active strategy performance drops
2. **dormant** (P2) — Activate coded-but-unused strategies
3. **param_drift** (P3) — Re-optimize drifted parameters
4. **filter** (P3) — Test new signal filters (VIX, volume, SMA)
5. **new_strategy** (P4) — Develop entirely new strategies
6. **portfolio** (P4) — Portfolio-level construction improvements
7. **cross_market** (P5) — Cross-market correlation signals

## File Ownership Boundaries

| Role       | Reads                          | Writes                              |
|------------|--------------------------------|-------------------------------------|
| Researcher | journal.json, perf data, queue | queue.json (append new entries)     |
| Backtester | queue.json                     | experiments/*.json, queue.json (status updates) |
| Analyst    | experiments/*.json             | journal.json (append), experiments/*.json (annotate verdict) |
| Risk       | experiments/*.json, journal    | config/candidates/*.json, promotion requests |

## Key Design Patterns

### Multi-Agent Ready
- All inter-role communication via JSON files, not in-memory state
- File locking on all writes (`fcntl.flock()`)
- `claimed_by` + `claimed_at` fields for future multi-agent claims
- Append-only journal (no edits, only appends)
- Experiment envelopes are fully self-contained

### Experiment Envelope
Each `exp-{id}.json` contains everything needed to understand the experiment:
- `queue_entry` — snapshot of the original hypothesis
- `config_snapshot` — exact config used
- `inputs` — strategy params, market, data range
- `outputs` — metrics, trade list summary
- `verdict` — pass/fail/partial with rationale
- `learnings` — what was learned (even from failures)

### Queue Entry Schema
```json
{
    "id": "20260227_150000_abc123",
    "title": "Test momentum_breakout on SP500",
    "category": "dormant",
    "market": "sp500",
    "hypothesis": "Momentum breakout captures trend initiations that TF misses",
    "method": "single_strategy_test",
    "acceptance_criteria": {"min_sharpe": 0.3, "min_trades": 15, "max_dd_pct": 10},
    "estimated_runtime_min": 30,
    "priority": "P2",
    "status": "queued",
    "strategy_name": "momentum_breakout",
    "params_override": null,
    "claimed_by": null,
    "claimed_at": null,
    "created_at": "2026-02-27T05:00:00+00:00",
    "updated_at": "2026-02-27T05:00:00+00:00"
}
```

## Scripts

| Script | Role | Purpose |
|--------|------|---------|
| `scripts/strategy_evaluator.py` | Backtester | Single strategy evaluation on any market |
| `scripts/research_runner.py` | Backtester | Experiment execution engine (reads queue, dispatches) |
| `scripts/reoptimize_parallel.py` | Backtester | Full parameter optimization |
| `scripts/validate_oos.py` | Analyst | Out-of-sample validation suite |

## Pi Skills

| Skill | Description |
|-------|-------------|
| `atlas-research-loop` | Daily research cycle (researcher → backtester → analyst → risk) |
| `atlas-research` | Ad-hoc research and validation |
| `atlas-reoptimize` | Optimization and config promotion |

---

## Before Going Live: Pre-Live Validation Gates

Before any strategy enters PAPER (let alone LIVE), it must pass several checks.
The canonical promotion state machine lives in `monitor/strategy_lifecycle.py`;
lifecycle API endpoints are in `services/api/lifecycle.py`.

### 1. Research-best contamination check

The `research_best` SQLite table is the canonical source of best-known parameters
per `strategy × universe`. Before considering a candidate for promotion, check:

- Run `scripts/data_integrity_monitor.py` — flags cross-universe identical-metric
  patterns. Any flagged candidate is contaminated; do NOT promote.
- Confirm the row's `updated_at` is **after 2026-04-22** (P1.1 universe isolation
  fix). Rows from before that date may carry contaminated baselines.
- Check `is_solo` field in `research/best/<strategy>.json`; `is_solo=False` rows
  are blocked by `_run_promotion_sweep()` in `research/autoresearch_nightly.py`.

### 2. Paper validation period

After RESEARCH → PAPER transition (`monitor/strategy_lifecycle.transition()`),
the strategy must run on the Alpaca paper broker for ≥ 30 trading days with:

- Paper trades count ≥ 30
- Paper Sharpe ≥ 0.3 (gate C threshold in `scripts/auto_promote_paper_to_live.py`)
- OOS Sharpe ≥ 0.3, OOS trades ≥ 30, OOS CAGR ≥ 5% (gates G/H/I)
- No divergence alert active for ≥ 7 consecutive days (gate J)

Paper fills are written to the `paper_trades` table (mirroring `trades`).
Execution routing: `scripts/execute_approved.py` splits plan entries by
`monitor.strategy_lifecycle.is_paper()` — PAPER-state strategies route to the
Alpaca paper broker regardless of universe `trading.mode`.

### 3. LIVE promotion (gates A–J)

PAPER → LIVE auto-promotion runs via `scripts/auto_promote_paper_to_live.py`
(cron: weekly, Mon 22:00 UTC). It evaluates all ten gates (see
`docs/ARCHITECTURE.md` § Strategy Lifecycle for the full gate table). On
all-pass:

1. Writes entry to `data/promotion_log.json`
2. Calls `monitor.strategy_lifecycle.transition(strategy, universe, 'LIVE')`
3. Sends Telegram notification

Manual promotion override: `POST /api/strategy-lifecycle/promote-paper`
(`services/api/lifecycle.py`), requires operator credentials.

After LIVE promotion, the divergence monitor (`scripts/check_live_research_divergence.py`,
`run_divergence_check()`) runs continuously. If live-equivalent PnL diverges from
research-best over a rolling window, `process_rollbacks()` fires LIVE → force-to-watch
health escalation (operator must act) or PAPER → RESEARCH auto-rollback.

### Promotion mechanism

State transitions use `monitor.strategy_lifecycle.transition(strategy, universe, new_state)`.
Never edit `config/active/*.json` directly to enable a strategy — the pre-commit hook
(lifecycle 1.6 guard) will block commits that bypass the promotion audit trail.
Use `BYPASS_RESEARCH_GATE="<reason>" git commit` only when you have a documented
operational reason.
