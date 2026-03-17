---
name: atlas-research-loop
description: "Drive the daily Atlas research cycle: hypothesis generation, experiment execution, result analysis, and promotion gating. Single agent wearing 4 hats (researcher, backtester, analyst, risk), structured for future multi-agent split."
type: agent
---

# Atlas Autoresearch Loop

Run autonomous trading strategy research using a tight keep/discard loop.
Inspired by karpathy/autoresearch — the LLM (you) drives the loop.

## Quick Start

```python
import sys; sys.path.insert(0, '/root/atlas')
from research.loop import ResearchSession, leaderboard, strategy_status, quick_check, combined_test

# 1. See what's available
print(strategy_status())
print(leaderboard())

# 2. Pick a strategy, start a session
s = ResearchSession('mean_reversion', 'sp500')
s.baseline()    # always first — establishes the bar

# 3. Run experiments
r = s.experiment({'rsi_period': 7}, 'shorter RSI period')
s.keep()    # if recommendation is 'keep'
# or
s.discard() # if recommendation is 'discard'

# 4. Check progress
print(s.history())
print(s.summary())
```

## Read First

Before starting, read the research program:
```
/root/atlas/research/program.md
```

This contains:
- Keep/discard criteria
- Simplicity rules
- Strategy priority tiers
- Tactics for when you're stuck
- What you can and cannot modify

## The Loop

You run this loop **forever** (until manually stopped):

### Phase 1: Survey (5 min)
```python
# What strategies exist and their current state
print(strategy_status())

# Current rankings
print(leaderboard())

# Pick the highest-value target
# Tier 1 (active) > Tier 2 (dormant) > Tier 3 (sandbox)
```

### Phase 2: Baseline (2-5 min)
```python
s = ResearchSession('mean_reversion', 'sp500')
metrics = s.baseline()
# This sets the bar. All experiments compared to this.
```

### Phase 3: Experiment Loop (most of your time)

For each strategy, sweep parameters one at a time:

```python
# Try each parameter individually
r = s.experiment({'rsi_period': 7}, 'RSI period 14→7: faster signals')
if r['recommendation'] == 'keep':
    s.keep()    # baseline advances — next experiment compared to THIS
else:
    s.discard() # baseline stays the same

r = s.experiment({'atr_stop_mult': 2.5}, 'wider stops: 2.0→2.5')
if r['recommendation'] == 'keep':
    s.keep()
else:
    s.discard()

# After individual sweeps, try combinations
r = s.experiment({'rsi_period': 7, 'rsi_oversold': 25}, 'RSI 7 + oversold 25')
# ...
```

**Stop rules for a strategy:**
- 5+ consecutive discards → move on (diminishing returns)
- Sharpe > 0.5 → good enough, try next strategy
- Sharpe < 0.0 after 10+ experiments → this strategy is dead, move on

### Phase 4: Combined Test (when a strategy is well-optimized)
```python
result = combined_test('mean_reversion', s.best()['params'])
print(f"Portfolio Sharpe change: {result['delta']['sharpe']:+.4f}")
# If delta >= -0.02: strategy is portfolio-compatible
```

### Phase 5: Move to Next Strategy
```python
print(s.summary())  # record what happened
# Pick next strategy from leaderboard/status
s = ResearchSession('trend_following', 'sp500')
s.baseline()
# ... repeat Phase 3 ...
```

## Screening New Strategies

For sandbox strategies that haven't been tested:

```python
# Step 1: Quick signal check (<10s)
result = quick_check('consecutive_down_days', 'sp500')
if not result['alive']:
    print(f"Dead: {result['reason']}")
    # Skip this strategy
else:
    # Step 2: Full baseline
    s = ResearchSession('consecutive_down_days', 'sp500')
    s.baseline()
    # Step 3: If Sharpe > 0.2, enter the optimization loop
    # Step 4: If optimized Sharpe > 0.3, run combined_test()
```

## Output Format

Each experiment prints:
```
--- Experiment: shorter RSI (mean_reversion) ---
sharpe:           0.4800
trades:           38
max_dd_pct:       7.20
profit_factor:    1.4500
cagr_pct:         12.30
win_rate_pct:     55.2
sortino:          0.6200
runtime_s:        3.1
recommendation:   KEEP (Sharpe +0.0600, trades -4, DD -1.3%)
rationale:        KEEP: Sharpe +0.0600, trades -4, DD -1.3%
---
```

## Results Storage

- **TSV per strategy**: `research/results/{strategy}.tsv` — full experiment log
- **Best params**: `research/best/{strategy}.json` — current champion config
- **Journal**: `research/journal.json` — appended for dashboard compatibility

## Promotion Flow

When a strategy is well-optimized (Sharpe > 0.3, combined test passes):

1. Stage candidate config:
```python
import json
from pathlib import Path
from utils.config import get_active_config

config = get_active_config('sp500')
config['strategies']['mean_reversion'].update(s.best()['params'])
config['strategies']['mean_reversion']['enabled'] = True

candidate_path = Path('/root/atlas/config/candidates/sp500_autoresearch.json')
candidate_path.write_text(json.dumps(config, indent=2))
```

2. Send promotion request via Telegram (requires human approval):
```bash
cd /root/atlas && python3 scripts/research_promote.py \
    --stage --experiment-id autoresearch --market sp500
```

**NEVER auto-promote. Always require human approval.**

## Parallel Execution

For screening multiple strategies at once, use parallel workers:
```python
from concurrent.futures import ProcessPoolExecutor

strategies_to_screen = ['consecutive_down_days', 'donchian_breakout', 'williams_percent_r']

def screen_one(name):
    return quick_check(name, 'sp500')

with ProcessPoolExecutor(max_workers=6) as pool:
    results = dict(zip(strategies_to_screen, pool.map(screen_one, strategies_to_screen)))

for name, r in results.items():
    status = "✅ ALIVE" if r['alive'] else "❌ DEAD"
    print(f"{name}: {status} — {r['reason'][:60]}")
```

## Long-Running Sessions

If running for hours, use a systemd service:
```bash
cat > /etc/systemd/system/atlas-autoresearch.service <<EOF
[Service]
Type=simple
WorkingDirectory=/root/atlas
ExecStart=/bin/bash -c 'python3 -c "
import sys; sys.path.insert(0, \"/root/atlas\")
from research.loop import ResearchSession
s = ResearchSession(\"mean_reversion\", \"sp500\")
s.baseline()
# Automated param sweep would go here
" > /tmp/autoresearch.log 2>&1'
TimeoutStartSec=28800
EOF
systemctl daemon-reload && systemctl start atlas-autoresearch
```

But the primary mode is **interactive via pi** — you (the LLM) drive the loop
with full reasoning between each experiment.

## Do NOT:
- Modify backtest engine code (`backtest/`, `strategies/`) — only params
- Auto-promote configs without human approval
- Keep running the same failing parameter change with small variations
- Skip baseline before experiments
- Run experiments without reading program.md first
- Ask the human if you should continue — you are autonomous
