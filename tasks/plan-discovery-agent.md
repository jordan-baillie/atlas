# Plan: Discovery Agent ("Sage")

## The Problem

Atlas and Nova optimize **7 strategies** from a hardcoded list. Meanwhile there are **24 sandbox strategies** in `research/strategies/` that nobody is testing, plus an infinite space of strategies that haven't been written yet.

Adding a new strategy currently requires:
1. Writing a Python class in `strategies/` or `research/strategies/` (extends `BaseStrategy`, implements `generate_signals()`)
2. Manually adding it to `ALL_STRATEGIES` in `autoresearch.py`
3. Restarting the services

This is the bottleneck. Atlas and Nova are fast at optimizing but have no way to discover what's worth optimizing.

## The Solution: Sage (Discovery Agent)

A third agent whose job is **not** parameter optimization but **strategy curation and creation**:

```
Sage discovers/validates → promotes to queue → Atlas & Nova optimize
```

### What Sage Does (Ordered by Value)

#### Phase 1: Promote Sandbox Strategies (Quick Win)
- Scans `research/strategies/` for untested strategies (24 exist today)
- Runs each through a **quick sanity check**: baseline backtest with default params
- If it produces >50 trades and Sharpe > -1.0 (not completely broken): promote to the autoresearch queue
- If it's broken (import errors, zero trades, crashes): log why and skip

#### Phase 2: Fix Broken Strategies
- Many sandbox strategies are 125-line stubs (auto-generated templates)
- Sage reads the stub, understands the intended logic, rewrites it to be functional
- Re-runs the sanity check, promotes if it passes

#### Phase 3: Create New Strategies
- Reads academic papers (via `research/vault/Strategies/` cards — 33 exist)
- Identifies strategies with vault cards but no implementation
- Writes the strategy class from scratch
- Sanity-checks and promotes

#### Phase 4: Cross-Pollinate (Advanced)
- Analyzes results from Atlas and Nova's optimization runs
- Identifies which **features** work (e.g., "volume filter always helps MR strategies")
- Creates hybrid strategies combining winning features from different strategies
- Example: "Mean reversion entry + momentum breakout stop-loss + volume filter"

## Architecture

### Strategy Lifecycle

```
┌─────────────┐    ┌──────────────┐    ┌──────────────────┐    ┌──────────┐
│  Sandbox     │    │  Candidate   │    │  Autoresearch    │    │  Live    │
│  (untested)  │───▶│  (validated) │───▶│  (optimizing)    │───▶│  Config  │
│  24 strats   │    │  passed QC   │    │  Atlas & Nova    │    │  3 now   │
└─────────────┘    └──────────────┘    └──────────────────┘    └──────────┘
      ▲                                        │
      │            Sage creates/fixes           │
      └────────────────────────────────────────┘
                   Findings feed back
```

### Dynamic Strategy Loading (No Restart Required)

Instead of hardcoding `ALL_STRATEGIES`, Atlas and Nova read from a **strategy queue file**:

```
/root/atlas/research/strategy_queue.json
```

```json
{
  "active": [
    {"name": "mean_reversion", "added_by": "manual", "since": "2026-02-26"},
    {"name": "trend_following", "added_by": "manual", "since": "2026-02-26"},
    {"name": "stochastic_oversold", "added_by": "sage", "since": "2026-03-11",
     "sanity": {"sharpe": 0.15, "trades": 280, "baseline_time_s": 45}}
  ],
  "candidates": [
    {"name": "donchian_breakout", "status": "sanity_passed", "sharpe": 0.08, "trades": 190}
  ],
  "rejected": [
    {"name": "dividend_capture", "reason": "import_error", "error": "missing yfinance"}
  ]
}
```

When Atlas/Nova start a new cycle, they re-read this file. No restarts needed.

### Sage's Service Architecture

```
/etc/systemd/system/atlas-sage.service
ExecStart=/usr/bin/python3 scripts/sage.py
```

Sage runs on a **longer cycle** (every 4-6 hours, not continuously):
- It's LLM-heavy (writing/fixing code) and doesn't need CPU for sweeps
- Doesn't compete with Atlas/Nova for CPU
- Uses the same `pi --print --skill` pattern

### Sage's Skill

A new skill: `pi-package/atlas-ops/skills/atlas-strategy-discovery/SKILL.md`

The skill teaches the agent:
- How `BaseStrategy` works (interface, `generate_signals()`, `PARAM_GRID`)
- Where sandbox strategies live
- How to run a quick sanity backtest
- How to write `strategy_queue.json` to promote candidates
- How to read existing research results for cross-pollination ideas

### Sage on the Dashboard

- **Character**: Blue/teal shirt, grey hair (the wise elder)
- **Type**: `sage`
- **Speech bubble**: "Evaluating Donchian Breakout..." / "Writing Keltner Hybrid..."
- **Whiteboard**: Show Sage's pipeline: X sandbox → Y candidates → Z promoted

### Resource Budget

| Resource | Sage's Usage | Impact |
|----------|-------------|--------|
| CPU | Near zero (LLM-bound, no sweeps) | No competition with Atlas/Nova |
| API cost | ~$2-5/day (Sonnet for code writing) | Moderate |
| Disk | Strategy files + sanity results | Negligible |
| Time | 4-6hr cycle, runs during off-peak | No interference |

### File Ownership

| File | Owner |
|------|-------|
| `scripts/sage.py` | New |
| `research/strategy_queue.json` | Sage writes, Atlas/Nova read |
| `research/strategies/*.py` | Sage writes new/fixes broken |
| `scripts/autoresearch.py` | Modify: read from strategy_queue.json |
| `dashboard/generate_data.py` | Add sage agent detection |
| `dashboard/templates/index.html` | Add sage color palette |
| `pi-package/atlas-ops/skills/atlas-strategy-discovery/SKILL.md` | New |
| `/tmp/sage-heartbeat.json` | Sage heartbeat |

## Implementation Order

### Step 1: Dynamic Strategy Queue (30 min)
- Create `research/strategy_queue.json` with current 7 strategies
- Modify `autoresearch.py` to read from queue file instead of hardcoded list
- Modify `sweep.py` STRATEGY_ORDER to read from queue too
- Test: add a strategy to queue, verify Atlas/Nova pick it up next cycle

### Step 2: Sanity Check Script (30 min)
- `scripts/sanity_check.py --strategy <name>` — runs one baseline backtest
- Returns: trade count, Sharpe, max DD, runtime
- Used by both Sage and humans to validate strategies quickly

### Step 3: Sage Script + Service (1 hour)
- `scripts/sage.py` — main loop:
  1. Scan `research/strategies/` for strategies not in queue
  2. Run sanity check on each
  3. Promote passing strategies to `strategy_queue.json`
  4. For broken stubs: call LLM to fix, re-check
- Heartbeat: `/tmp/sage-heartbeat.json`
- Systemd service: `atlas-sage.service`

### Step 4: Dashboard Integration (30 min)
- New color palette for Sage character
- Read sage heartbeat
- Update whiteboard: show candidate pipeline

### Step 5: Strategy Creation Skill (1 hour)
- Skill that teaches the LLM agent how to write new strategies
- Includes BaseStrategy interface, examples, param grid patterns
- Sage uses this for Phase 3 (creating from vault cards)

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Sage writes broken code that crashes backtester | Sanity check runs in subprocess with timeout; crashes = rejected |
| Sage floods queue with garbage strategies | Rate limit: max 2 promotions per cycle |
| Atlas/Nova waste time on bad strategies | Minimum sanity threshold: Sharpe > -0.5 AND trades > 30 |
| Strategy queue file corruption | Atomic writes (write to tmp, rename) |
| Sage conflicts with Atlas/Nova on shared files | Sage only writes to `research/strategies/` and `strategy_queue.json`; Atlas/Nova only read these |

## Expected Impact

- **Immediate**: 24 sandbox strategies get triaged automatically
- **Week 1**: 5-10 new strategies in the optimization pipeline
- **Month 1**: Sage has written 10+ novel strategies from vault cards
- **Ongoing**: Self-expanding strategy universe — Sage discovers, Atlas & Nova optimize, best get promoted to live

## Decision Needed

- **Should Sage auto-promote to active queue?** Or should it propose candidates that require human approval?
  - Recommendation: Auto-promote to active with guardrails (sanity thresholds). The whole point is autonomy.
- **Should Atlas/Nova rebalance partitions when queue grows?** e.g., 14 strategies → 7 each?
  - Recommendation: Yes, auto-rebalance. Each reads queue, takes their partition slice.
