# Cookbook: Optimize & Tune a Strategy

Use this cookbook to run parameter optimization and interpret sweep results.

---

## 1. PARAM_GRID Convention

Every sandbox strategy should export `PARAM_GRID` at module level. The sweeper picks it up automatically:

```python
# At the bottom of research/strategies/my_strategy.py
PARAM_GRID = {
    'rsi_period':    [5, 7, 10, 14, 21],
    'atr_stop_mult': [1.5, 2.0, 2.5, 3.0],
    'max_hold_days': [5, 10, 15, 20],
}
```

**Grid design rules:**
- 3–5 values per parameter is typical; more values = longer runtime
- Include the current default in every list (e.g. if default is `14`, include `14`)
- Avoid more than 4 parameters × 5 values — combinatorial explosion
- `full_optimization` uses coordinate descent so order doesn't matter

---

## 2. Queue a `full_optimization` Experiment

```json
{
  "id": "donchian_opt_20260311",
  "title": "Donchian Breakout — full parameter optimization",
  "category": "new_strategy",
  "market": "sp500",
  "hypothesis": "Coordinate descent will find params with Sharpe > 0.4 on IS period.",
  "method": "full_optimization",
  "acceptance_criteria": {
    "min_sharpe": 0.3,
    "min_trades": 30,
    "max_dd_pct": 20.0,
    "description": "Optimized Sharpe > 0.3 sustained across multiple coord-descent cycles."
  },
  "estimated_runtime_min": 60,
  "priority": "P3",
  "strategy_name": "donchian_breakout",
  "params_override": {
    "param_grid": {
      "entry_period":  [10, 20, 30, 55],
      "exit_period":   [5, 10, 15, 20],
      "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
      "max_hold_days": [10, 20, 30, 55]
    }
  }
}
```

⚠️ Common mistake: using `"optimize_params"` instead of `"param_grid"`.

---

## 3. Coordinate Descent Notes

`full_optimization` runs coordinate descent:
1. Start from current best params (or defaults if no prior run)
2. For each parameter in sequence: sweep all values, keep the best
3. Repeat N cycles until Sharpe improvement < threshold
4. Write best params to `research/best/{strategy}.json`

**Avoid:** queuing `full_optimization` before `single_strategy_test` — confirm the strategy is alive first.

---

## 4. ResearchSession for Manual Experiments

For interactive research, use `ResearchSession` directly:

```python
import sys; sys.path.insert(0, '/root/atlas')
from research.loop import ResearchSession, load_best, read_results

# Start a session
session = ResearchSession('donchian_breakout', market='sp500')

# Run a baseline
baseline = session.baseline()
print(f"Baseline Sharpe: {baseline['sharpe']:.3f}, Trades: {baseline['trades']}")

# Test a parameter change
result = session.experiment({'entry_period': 30, 'exit_period': 15})
print(f"Experiment Sharpe: {result['sharpe']:.3f}")

# Keep or discard
if result['sharpe'] > baseline['sharpe']:
    session.keep(result)
    print("Improvement accepted")
else:
    session.discard(result)
    print("No improvement, reverting")
```

---

## 5. Viewing Optimization Results

```python
from research.loop import leaderboard, read_results, load_best

# See all strategies ranked by best Sharpe
print(leaderboard())

# Check the full experiment log for a strategy
df = read_results('donchian_breakout', n=50)
print(df[['params', 'sharpe', 'trades', 'max_dd']].to_string())

# Load the best-known params
best = load_best('donchian_breakout')
print(best)   # {"entry_period": 20, "exit_period": 10, "atr_stop_mult": 2.0, ...}
```

---

**Next step:** If Sharpe > 0.3 sustained, test portfolio fit → queue `combined_portfolio_test`  
**Then:** Promote to production → Load cookbook: `cookbooks/promote-deploy.md`
