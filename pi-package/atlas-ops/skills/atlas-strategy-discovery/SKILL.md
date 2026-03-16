---
name: atlas-strategy-discovery
description: "Design, implement, validate, and queue new Atlas trading strategies. Covers BaseStrategy interface, working code templates, sandbox workflow, sanity checks, queue format, and common failure patterns. Use when asked to build a new strategy, screen an experimental strategy, or add experiments to the research queue."
---

# Atlas Strategy Discovery

Use this skill to build new trading strategies from scratch, validate experimental implementations, and add them to the research queue for systematic backtesting.

---

## Quick Start

```python
import sys; sys.path.insert(0, '/root/atlas')

# 1. Generate a strategy scaffold
from research.strategy_factory import build_strategy
result = build_strategy(
    'donchian_breakout',
    description='Buy on 20-day high breakout, sell on 10-day low.',
    reference='Richard Donchian, Turtle Traders (1983)',
)
print(result['file_path'])      # research/strategies/donchian_breakout.py
print(result['validation'])     # {"valid": True, ...}

# 2. Validate an existing sandbox strategy
from research.strategy_factory import validate_strategy
v = validate_strategy('donchian_breakout')
print(v)   # {"valid": True, "class_name": "DonchianBreakout", ...}

# 3. Quick-screen it (<10s)
from research.loop import quick_check
result = quick_check('donchian_breakout', 'sp500')
print(result)  # {"alive": True, "signal_count": 12, "sharpe": 0.21, ...}

# 4. Add to research queue for systematic testing
python3 scripts/sanity_check.py --strategy donchian_breakout   # pre-flight
python3 scripts/sanity_check.py --queue donchian_breakout      # add to queue
```

---

## Key Principles

1. **Sandbox first.** All new code goes in `research/strategies/` — never directly to `strategies/`.
2. **No auto-promotion.** Human approval is always required before a strategy goes live.
3. **Sanity check before queuing.** Run `scripts/sanity_check.py` to catch import/logic errors before burning compute.
4. **PARAM_GRID is required for optimization.** Export it at module level or the sweeper skips the strategy.
5. **Dead ends are OK.** Mark failed strategies `dead_end` with a reason — don't delete, don't retry without a new hypothesis.

---

## Sandbox Rules

New strategies live in **`research/strategies/`** (the sandbox), not `strategies/` (production). They go through a promotion lifecycle before touching live trades.

### File locations

| Path | Purpose |
|---|---|
| `research/strategies/{name}.py` | Sandbox — experimental, not loaded by live engine |
| `strategies/{name}.py` | Production — loaded at startup, can run in live mode |
| `research/best/{name}.json` | Best-known params from autoresearch |
| `research/results/{name}.tsv` | Full experiment log (one row per backtest) |
| `config/candidates/` | Staged config files awaiting human promotion approval |

### Lifecycle stages

```
not_built → screening → solo → optimize → combined → oos → active
                                                          ↘ dead_end
```

| Stage | What it means | Gate to pass |
|---|---|---|
| `not_built` | No code yet | Write strategy file |
| `screening` | Code exists, hasn't been quick-screened | `quick_check()` passes |
| `solo` | Screened, running solo backtest | Sharpe > 0.0, trades > 10 |
| `optimize` | Promising solo results, being tuned | Sharpe > 0.2 sustained |
| `combined` | Solo optimized, testing portfolio fit | Combined Sharpe delta > -0.02 |
| `oos` | Combined passes, out-of-sample validation | OOS Sharpe within 20% of IS |
| `active` | Promoted to production config | Human approval required |
| `dead_end` | Failed any gate decisively | Document why, don't retry |

---

## Research Runner Flow

Once a strategy is queued, the research runner handles it automatically:

```
queue.json (status=queued)
    → Research runner picks up in priority order (P1 first)
    → Runs the experiment method (backtest, sweep, optimization, etc.)
    → Writes results to research/results/{strategy}.tsv
    → Updates research/best/{strategy}.json if new best found
    → Marks entry status=completed or status=failed
```

Check queue status and results:
```python
from research.models import read_queue
entries = read_queue()
for e in entries:
    print(f"{e.id:40s} {e.status:12s} {e.priority}")
```

---

## Cookbook Routing Table

Load the cookbook that matches your current task:

| I want to... | Cookbook | Load with |
|---|---|---|
| Build a new strategy from scratch | Scaffold & Implement | Load cookbook: `cookbooks/scaffold-implement.md` |
| Validate, screen, or test a strategy | Validate & Screen | Load cookbook: `cookbooks/validate-screen.md` |
| Add experiments to research queue | Queue Management | Load cookbook: `cookbooks/queue-manage.md` |
| Run parameter optimization | Optimize & Tune | Load cookbook: `cookbooks/optimize-tune.md` |
| Promote a strategy to production | Promote & Deploy | Load cookbook: `cookbooks/promote-deploy.md` |
| Debug a failing strategy | Common Failures | Load cookbook: `cookbooks/common-failures.md` |
