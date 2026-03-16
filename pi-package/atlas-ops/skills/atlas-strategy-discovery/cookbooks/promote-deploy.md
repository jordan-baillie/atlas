# Cookbook: Promote & Deploy a Strategy

Use this cookbook when a strategy has passed all research gates and is ready for production.

---

## Promotion Rules

**Never auto-promote.** Always require human approval. A strategy must pass ALL gates:

1. Solo Sharpe > 0.3 (sustained, not a single lucky run)
2. Trades ≥ 30 in full IS period
3. Max drawdown < 20%
4. Combined portfolio test: delta Sharpe > -0.02
5. OOS validation Sharpe within 20% of IS Sharpe

---

## Full Discovery Workflow

```
1. Research the strategy
   → Read reference paper/source
   → Understand entry condition, exit condition, parameters

2. Generate scaffold
   → build_strategy('name', description, reference)
   → Edit research/strategies/name.py
   → Implement generate_signals() and check_exits()
   → Add PARAM_GRID at bottom of file

3. Validate locally
   → validate_strategy('name')                  # import + structure check
   → quick_check('name', 'sp500')               # signal + quick backtest

4. Sanity check + queue
   → python3 scripts/sanity_check.py --strategy name --signals --queue

5. Autoresearch picks it up
   → Research runner processes queue.json in priority order
   → Sweeper + agent loop optimize parameters
   → Results written to research/results/name.tsv

6. Review results
   → from research.loop import leaderboard; print(leaderboard())
   → If Sharpe > 0.3: queue combined_portfolio_test

7. OOS validation
   → Queue oos_validation experiment
   → OOS Sharpe must be within 20% of IS Sharpe

8. Promotion (human approval required — STOP HERE, notify for review)
   → python3 scripts/research_promote.py --stage --experiment-id autoresearch --market sp500
   → NEVER auto-promote
```

---

## Stage a Candidate Config

After OOS passes, stage the candidate config for human review:

```python
import sys; sys.path.insert(0, '/root/atlas')
from utils.config import get_active_config
import json
from pathlib import Path

# Load active config and add the new strategy
config = get_active_config('sp500')
best_params = {
    'enabled': True,
    'entry_period': 20,
    'exit_period': 10,
    'atr_stop_mult': 2.0,
    'max_hold_days': 20,
    'sma200_filter': True,
}
config['strategies']['donchian_breakout'] = best_params

# Write to candidates — DO NOT copy to active_config.json
candidate = Path('/root/atlas/config/candidates/sp500_donchian.json')
candidate.write_text(json.dumps(config, indent=2))
print(f'Staged: {candidate}')

# STOP HERE — notify for human review
```

---

## Combined Portfolio Test

Before staging, verify the strategy doesn't hurt the existing portfolio:

```python
from research.loop import combined_test, load_best

best = load_best('donchian_breakout')
result = combined_test('donchian_breakout', best)

# result['delta_sharpe']     — change in portfolio Sharpe when strategy added
# result['delta_cagr']       — change in CAGR
# result['combined_sharpe']  — full portfolio Sharpe with new strategy

print(f"Delta Sharpe: {result['delta_sharpe']:+.3f}")
print(f"Delta CAGR:   {result['delta_cagr']:+.1f}%")

if result['delta_sharpe'] < -0.02:
    print("FAIL: strategy hurts portfolio — do not promote")
else:
    print("PASS: portfolio test passed — proceed to OOS validation")
```

---

## OOS Validation Gate

Queue an `oos_validation` experiment — the research runner handles the held-out period:

```python
from research.models import QueueEntry, ExperimentType, append_to_queue

entry = QueueEntry(
    id='donchian_oos_20260311',
    title='Donchian Breakout — OOS validation',
    category='new_strategy',
    market='sp500',
    hypothesis='OOS Sharpe should be within 20% of IS Sharpe (IS=0.45).',
    method=ExperimentType.OOS_VALIDATION,
    acceptance_criteria={
        'min_oos_sharpe': 0.36,                  # 80% of IS
        'max_oos_is_degradation_pct': 20.0,
        'description': 'OOS Sharpe >= 0.36 (within 20% of IS=0.45)',
    },
    estimated_runtime_min=20,
    priority='P2',
    strategy_name='donchian_breakout',
)
append_to_queue(entry)
```

---

## Promotion CLI

Once human has reviewed the staged config and approved:

```bash
# Stage (research automation — safe)
python3 scripts/research_promote.py --stage --experiment-id autoresearch --market sp500

# Promote (requires explicit human confirmation)
# Use the atlas_risk_promote_config tool — it creates backups and audit records
```

---

**Decision gate before promoting:**

| Check | Pass threshold |
|---|---|
| Solo Sharpe | > 0.3 sustained |
| Trade count (IS) | ≥ 30 |
| Max drawdown | < 20% |
| Delta Sharpe (combined) | > -0.02 |
| OOS/IS Sharpe ratio | ≥ 80% |
| Human approval | Required |
