# Cookbook: Queue Management

Use this cookbook to add, inspect, and manage experiments in `research/queue.json`.

---

## Queue Format

The queue is a JSON array of `QueueEntry` objects. New entries are appended; the research runner claims and runs them in priority order.

### Minimal required fields

```json
{
  "id": "my_strat_baseline_20260311",
  "title": "My Strategy — initial solo baseline",
  "category": "new_strategy",
  "market": "sp500",
  "hypothesis": "Donchian channel breakout captures trend initiation on SP500 stocks with Sharpe > 0.3.",
  "method": "single_strategy_test",
  "acceptance_criteria": {
    "min_sharpe": 0.2,
    "min_trades": 30,
    "max_dd_pct": 25.0,
    "description": "Solo Sharpe > 0.2 with at least 30 trades and max drawdown < 25%."
  },
  "estimated_runtime_min": 15,
  "priority": "P3",
  "status": "queued",
  "strategy_name": "donchian_breakout",
  "params_override": null,
  "tags": ["new_strategy", "trend_following", "tier1"],
  "depends_on": [],
  "notes": ""
}
```

### All fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | str | ✅ | Unique — use `{strategy}_{type}_{YYYYMMDD}` convention |
| `title` | str | ✅ | Short human description |
| `category` | str | ✅ | `degradation\|dormant\|param_drift\|filter\|new_strategy\|portfolio\|cross_market` |
| `market` | str | ✅ | `sp500` |
| `hypothesis` | str | ✅ | One sentence: what you expect and why |
| `method` | str | ✅ | See method types below |
| `acceptance_criteria` | dict | ✅ | Concrete pass/fail thresholds |
| `estimated_runtime_min` | int | ✅ | Best estimate; used for scheduling |
| `priority` | str | ✅ | `P1` (critical) → `P5` (backlog) |
| `status` | str | — | Default: `queued` |
| `strategy_name` | str\|null | — | Required for most methods |
| `params_override` | dict\|null | — | Method-specific (see below) |
| `tags` | list[str] | — | For filtering and reporting |
| `depends_on` | list[str] | — | IDs of experiments that must complete first |
| `notes` | str | — | Any additional context |

---

## Method Types and `params_override` Schemas

### `single_strategy_test`
Run a solo backtest. `params_override` can be null or a flat dict of param overrides:

```json
{
  "method": "single_strategy_test",
  "strategy_name": "donchian_breakout",
  "params_override": {"entry_period": 55, "exit_period": 20}
}
```

### `param_sweep`
Sweep one parameter across multiple values. **Requires both fields:**

```json
{
  "method": "param_sweep",
  "strategy_name": "mean_reversion",
  "params_override": {
    "sweep_param": "rsi_period",
    "sweep_values": [5, 7, 10, 14, 21]
  }
}
```

⚠️ Common mistake: using `"sweep_params"` (plural) or `"values"` instead of `"sweep_param"` + `"sweep_values"`.

### `filter_test`
Test enabling/disabling a boolean filter or sweeping a threshold:

```json
{
  "method": "filter_test",
  "strategy_name": "mean_reversion",
  "params_override": {
    "filter_param": "sma200_filter",
    "variants": [
      {"name": "off (current)", "value": false},
      {"name": "on",            "value": true}
    ]
  }
}
```

### `full_optimization`
Coordinate-descent optimization over a parameter grid. See `cookbooks/optimize-tune.md` for PARAM_GRID design guidance:

```json
{
  "method": "full_optimization",
  "strategy_name": "donchian_breakout",
  "category": "new_strategy",
  "params_override": {
    "param_grid": {
      "entry_period":  [10, 20, 30, 55],
      "exit_period":   [5, 10, 15, 20],
      "atr_stop_mult": [1.5, 2.0, 2.5, 3.0]
    }
  }
}
```

### `combined_portfolio_test`
Test portfolio impact of adding the strategy to active strategies:

```json
{
  "method": "combined_portfolio_test",
  "strategy_name": "donchian_breakout",
  "params_override": null
}
```

### `oos_validation`
Out-of-sample test. Uses held-out data period:

```json
{
  "method": "oos_validation",
  "strategy_name": "donchian_breakout",
  "params_override": null
}
```

---

## Priority Levels

| Priority | Use for |
|---|---|
| `P1` | Degradation fixes, broken strategies, critical bugs |
| `P2` | Dormant strategy activation, known improvements from research |
| `P3` | Parameter drift correction, new filters, known Tier 1 strategies |
| `P4` | New unproven strategies, exploratory research |
| `P5` | Long-term ideas, cross-market, speculative |

---

## Adding to Queue Programmatically

```python
import sys; sys.path.insert(0, '/root/atlas')
from research.models import QueueEntry, ExperimentType, append_to_queue, validate_queue_entry

entry = QueueEntry(
    id='donchian_baseline_20260311',
    title='Donchian Breakout — initial solo baseline',
    category='new_strategy',
    market='sp500',
    hypothesis='Donchian channel breakout should capture trend initiation on SP500.',
    method=ExperimentType.SINGLE_STRATEGY_TEST,
    acceptance_criteria={
        'min_sharpe': 0.2,
        'min_trades': 30,
        'description': 'Solo Sharpe > 0.2 with 30+ trades.'
    },
    estimated_runtime_min=15,
    priority='P3',
    strategy_name='donchian_breakout',
    tags=['new_strategy', 'trend_following', 'tier1'],
)

# Validate BEFORE appending
errors = validate_queue_entry(entry)
if errors:
    print('Queue validation errors:')
    for e in errors:
        print(f'  - {e}')
else:
    append_to_queue(entry)
    print(f'Queued: {entry.id}')
```

## Via CLI (`sanity_check.py`)

```bash
# Validate and add single_strategy_test to queue
python3 scripts/sanity_check.py \
    --strategy donchian_breakout \
    --queue \
    --priority P3 \
    --notes "Tier 1 academic strategy, initial screening"

# Validate and add full_optimization
python3 scripts/sanity_check.py \
    --strategy donchian_breakout \
    --queue \
    --method full_optimization \
    --priority P3
```

---

**Next step:** Run parameter optimization → Load cookbook: `cookbooks/optimize-tune.md`  
**Or:** Promote to production → Load cookbook: `cookbooks/promote-deploy.md`
