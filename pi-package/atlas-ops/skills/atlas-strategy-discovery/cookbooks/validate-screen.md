# Cookbook: Validate & Screen a Strategy

Use this cookbook after implementing a strategy to verify it's structurally sound and signal-generating before queuing.

---

## 1. Validate Strategy Code

Checks import, class structure, and method signatures — no data required:

```python
import sys; sys.path.insert(0, '/root/atlas')
from research.strategy_factory import validate_strategy

v = validate_strategy('my_strategy')
# {"valid": True, "class_name": "MyStrategy",
#  "has_signals": True, "has_exits": True, "errors": []}

if not v['valid']:
    print(v['errors'])   # fix these before queuing
```

---

## 2. Quick-Screen with `quick_check()`

Runs a lightweight backtest (<10s) on the current universe to verify signal generation:

```python
from research.loop import quick_check

result = quick_check('my_strategy', 'sp500')
# {"alive": True, "signal_count": 12, "sharpe": 0.21, "trades": 18, ...}

if not result['alive']:
    print(result['reason'])   # diagnose zero-signal issues
```

If `signal_count == 0`, see [Common Failures](cookbooks/common-failures.md) — pattern #2.

---

## 3. Sanity Check (`scripts/sanity_check.py`)

`sanity_check.py` is the pre-flight gate before adding a strategy to the research queue. Catches structural errors, import failures, and logic bugs that would waste compute in the full backtest.

### Usage

```bash
# Validate strategy code only
python3 scripts/sanity_check.py --strategy my_strategy

# Validate + run a live signal check (uses cached data, ~5s)
python3 scripts/sanity_check.py --strategy my_strategy --signals

# Validate + queue a solo experiment (adds to research/queue.json)
python3 scripts/sanity_check.py --strategy my_strategy --queue

# Validate + queue a full_optimization experiment
python3 scripts/sanity_check.py --strategy my_strategy --queue --method full_optimization
```

### What it checks

1. **File exists** — `research/strategies/{name}.py` (sandbox) or `strategies/{name}.py` (production)
2. **Importable** — no syntax errors, missing imports, or name errors
3. **Class found** — at least one `BaseStrategy` subclass exists in the module
4. **Instantiable** — `__init__` succeeds with a minimal config dict
5. **Methods callable** — `generate_signals` and `check_exits` are implemented (not `pass`-only)
6. **Signal shape** — if `--signals` flag: runs on top-10 tickers and verifies Signal fields are valid
7. **Stop > entry guard** — checks that stop_price < entry_price in returned signals
8. **`PARAM_GRID` present** — warns (not errors) if missing, as sweeper needs it

### Exit codes

| Code | Meaning |
|---|---|
| 0 | All checks passed |
| 1 | Strategy failed validation — see stderr for details |
| 2 | Strategy file not found |
| 3 | Queue write failed |

### Programmatic usage

```python
from scripts.sanity_check import check_strategy

result = check_strategy('my_strategy', run_signals=True)
# {
#   "valid": True,
#   "checks": ["file_exists", "importable", "class_found", "instantiable",
#               "has_signals", "has_exits", "signals_valid"],
#   "warnings": ["PARAM_GRID not found — sweeper will skip this strategy"],
#   "errors": [],
# }
```

---

## Decision: Is It Ready to Queue?

| Result | Action |
|---|---|
| `valid=True`, signals > 5, Sharpe > 0 | Queue for solo test → `cookbooks/queue-manage.md` |
| `valid=True`, signals = 0 | Debug zero-signal issue → `cookbooks/common-failures.md` |
| `valid=False` | Fix errors from `v['errors']` and re-validate |
| Sharpe < 0 but signals exist | Still worth queuing — let the full backtest decide |

---

**Next step:** Queue a validated strategy → Load cookbook: `cookbooks/queue-manage.md`
