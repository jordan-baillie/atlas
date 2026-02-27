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
