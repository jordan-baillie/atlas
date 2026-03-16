# Strategy & Backtest — Incident Cookbook

Covers: strategy import errors, 0-trade backtests, config parse failures.

---

## Pattern 12: Strategy Import Error (Dormant Strategy Drift)

**Symptoms:** `ImportError`, `AttributeError`, `TypeError` when running dormant strategy.

**Root cause:** Lesson #15 — dormant strategies accumulate API drift bugs.

**Fix:**
```bash
# Test import
cd /root/atlas
python3 -c "from strategies.<name> import <ClassName>; <ClassName>({})"

# Common issues:
# - generate_signals() signature changed (missing config arg)
# - calc_atr() call pattern changed
# - Series comparison ambiguity (use .item() or .iloc[0])
# - calc_position_size returns dict, not int
```

**Verify:**
```bash
cd /root/atlas
python3 -c "
from strategies.<name> import <ClassName>
s = <ClassName>({})
print('Import OK:', type(s))
"
```

---

## Pattern 13: Backtest Returns 0 Trades

**Symptoms:** `Sharpe=NaN`, `trades=0` in backtest output.

**Root cause:** Strategy generates 0 signals (config params too restrictive, or bug in signal generation).

**Fix:**
```bash
# Quick screen to check signal generation
cd /root/atlas
python3 -c "
from research.quick_screen import screen_strategy
from utils.config import get_active_config
cfg = get_active_config('sp500')
r = screen_strategy('<strategy_name>', cfg, market='sp500')
print(r)
"
```

**Verify:**
- Screen output should show `trades > 0`
- If still 0, inspect strategy params — widen thresholds (e.g., lower `rsi_threshold`, widen `breakout_window`)

---

## Pattern 14: Config Parse Error

**Symptoms:** `json.JSONDecodeError`, `KeyError` on config access.

**Fix:**
```bash
# Validate JSON
python3 -m json.tool config/active/sp500.json > /dev/null && echo "OK" || echo "INVALID"

# Check required sections
python3 -c "
import json
c = json.load(open('config/active/sp500.json'))
for key in ['version', 'trading', 'risk', 'strategies', 'universe', 'backtest']:
    print(f'{key}: {\"present\" if key in c else \"MISSING\"}')"
```

**Verify:**
```bash
python3 -m json.tool config/active/sp500.json > /dev/null && echo "Config valid"
# All 6 required keys should show "present"
```

> If config is corrupted, restore from backup:
> `atlas_risk_restore_config_backup` (Pi tool) or `ls config/backups/` and copy manually.
