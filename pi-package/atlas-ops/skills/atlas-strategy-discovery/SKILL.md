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

## BaseStrategy Interface

Every Atlas strategy inherits from `strategies.base.BaseStrategy`. Two abstract methods **must** be implemented:

```python
from strategies.base import BaseStrategy, Signal
from typing import Any, Dict, List
import pandas as pd

class MyStrategy(BaseStrategy):
    """One-sentence description."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        # Read parameters from config dict
        strat_cfg = config.get('strategies', {}).get('my_strategy', {})
        self.my_param = strat_cfg.get('my_param', 14)

    @property
    def name(self) -> str:
        return 'my_strategy'    # snake_case, matches config key

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Scan all tickers and return entry signals."""
        ...

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Return exit recommendations for open positions."""
        ...
```

### `generate_signals()` contract

| Argument | Type | Description |
|---|---|---|
| `data` | `Dict[str, pd.DataFrame]` | Ticker → OHLCV DataFrame. Columns: `open high low close adj_close volume ticker`. Index: `DatetimeIndex`. |
| `equity` | `float` | Current account equity for position sizing. |
| `existing_positions` | `List[Dict]` | Open positions. Keys: `ticker strategy entry_price shares entry_date stop_price`. |

Returns `List[Signal]` — a `Signal` object for each new entry.

### `check_exits()` contract

| Argument | Type | Description |
|---|---|---|
| `data` | `Dict[str, pd.DataFrame]` | Same OHLCV data. |
| `positions` | `List[Dict]` | Open positions (same schema as above). |

Returns `List[Dict]` — one exit recommendation per position to close:

```python
{
    "ticker":      "AAPL",
    "reason":      "stop_hit",   # stop_hit | take_profit | signal_exit | time_exit | trailing_stop
    "exit_price":  182.30,
    "details":     "Price 182.30 <= stop 183.00",
}
```

### Signal dataclass

```python
from strategies.base import Signal

signal = Signal(
    ticker        = "AAPL",
    strategy      = self.name,           # must match @property name
    direction     = "long",              # only "long" supported in v1
    entry_price   = 185.00,
    stop_price    = 180.00,              # MUST be < entry_price
    take_profit   = 195.00,              # optional; MUST be > entry_price if set
    position_size = 10,                  # shares
    position_value= 1850.00,
    risk_amount   = 50.00,               # (entry - stop) * shares
    confidence    = 0.75,                # 0.0 – 1.0
    rationale     = "RSI 22 + Z=-2.4",  # human readable
    features      = {"rsi": 22, "zscore": -2.4},   # key indicators
    market_id     = "sp500",
    sector        = "Technology",
)
```

**Validation errors `Signal.__post_init__` raises:**
- `direction != "long"` → `ValueError`
- `stop_price >= entry_price` → `ValueError` (stops must be below entry for longs)
- `take_profit <= entry_price` (when set) → `ValueError`
- `position_size <= 0` → `ValueError`
- `confidence` outside `[0, 1]` → `ValueError`

### BaseStrategy helpers

```python
# Check position limits before adding a signal
if not self._can_open_position(existing_positions, sector=df.iloc[0].get('sector', '')):
    break   # respect max_open_positions and max_sector_concentration

# Skip already-held tickers
held = self._get_held_tickers(existing_positions)
if ticker in held:
    continue

# Guard against short DataFrames
min_rows = max(self.rsi_period, self.atr_period) + 10
if not self._has_sufficient_data(df, min_rows):
    continue
```

---

## Working Example — Donchian Breakout

Full implementation showing all patterns:

```python
"""
Atlas Donchian Breakout Strategy
==================================
Buy on 20-day high breakout; stop on 10-day low.

Reference: Richard Donchian, Turtle Traders (1983)
Config Section: strategies.donchian_breakout
"""
import logging
from datetime import datetime
from typing import Any, Dict, List

import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_position_size

logger = logging.getLogger(__name__)


class DonchianBreakout(BaseStrategy):
    """Trend-following strategy: buy 20-day high breakout."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get('strategies', {}).get('donchian_breakout', {})
        self.entry_period  = strat_cfg.get('entry_period', 20)   # breakout lookback
        self.exit_period   = strat_cfg.get('exit_period', 10)    # stop lookback
        self.atr_period    = strat_cfg.get('atr_period', 14)
        self.atr_stop_mult = strat_cfg.get('atr_stop_mult', 2.0)
        self.max_hold_days = strat_cfg.get('max_hold_days', 20)
        self.sma200_filter = strat_cfg.get('sma200_filter', True)
        self._logger.info('DonchianBreakout initialized: entry=%d exit=%d',
                          self.entry_period, self.exit_period)

    @property
    def name(self) -> str:
        return 'donchian_breakout'

    def generate_signals(
        self, data: Dict[str, pd.DataFrame],
        equity: float, existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        signals: List[Signal] = []
        held = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get('max_risk_per_trade_pct', 0.005)
        commission_per_trade = self.fees_config.get('commission_per_trade', 0.0)
        commission_pct = self.fees_config.get('commission_pct', 0.0)

        min_rows = max(self.entry_period, self.atr_period, 200) + 5

        for ticker, df in data.items():
            try:
                if ticker in held:
                    continue
                if not self._can_open_position(existing_positions):
                    break
                if not self._has_sufficient_data(df, min_rows):
                    continue

                close, high, low = df['close'], df['high'], df['low']

                # SMA-200 uptrend filter
                if self.sma200_filter:
                    sma200 = close.rolling(200).mean().iloc[-1]
                    if pd.isna(sma200) or close.iloc[-1] < sma200:
                        continue

                # Entry: today's close breaks above the prior N-day high
                prior_high = high.iloc[-(self.entry_period + 1):-1].max()
                if close.iloc[-1] <= prior_high:
                    continue

                # ATR stop
                from utils.helpers import calc_atr
                atr = calc_atr(high, low, close, period=self.atr_period)
                current_atr = atr.iloc[-1]
                if pd.isna(current_atr) or current_atr <= 0:
                    continue

                entry_price = close.iloc[-1]
                stop_price  = entry_price - self.atr_stop_mult * current_atr
                if stop_price <= 0:
                    continue

                pos = calc_position_size(
                    equity=equity, risk_pct=risk_pct,
                    entry_price=entry_price, stop_price=stop_price,
                    commission_per_trade=commission_per_trade,
                    commission_pct=commission_pct,
                )
                if pos['shares'] <= 0:
                    continue

                signals.append(Signal(
                    ticker=ticker, strategy=self.name, direction='long',
                    entry_price=entry_price, stop_price=round(stop_price, 4),
                    take_profit=None,
                    position_size=pos['shares'],
                    position_value=pos['position_value'],
                    risk_amount=pos['total_risk'],
                    confidence=0.65,
                    rationale=(
                        f'{ticker} broke above {self.entry_period}-day high '
                        f'({prior_high:.2f}) at {entry_price:.2f}.'
                    ),
                    features={'prior_high': round(prior_high, 4), 'atr': round(current_atr, 4)},
                    timestamp=datetime.now(),
                ))
            except Exception as e:
                self._logger.error('%s: signal error: %s', ticker, e, exc_info=True)

        self._logger.info('%s: %d signals from %d tickers', self.name, len(signals), len(data))
        return signals

    def check_exits(
        self, data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        exits = []
        for pos in positions:
            if pos.get('strategy') != self.name:
                continue
            ticker = pos.get('ticker')
            df = data.get(ticker)
            if df is None or df.empty:
                continue

            close = df['close']
            current_price = float(close.iloc[-1])
            stop_price    = pos.get('stop_price', 0)
            entry_date    = pd.Timestamp(pos['entry_date'])
            days_held     = (df.index[-1] - entry_date).days

            # 1. ATR trailing stop (10-day low = Donchian exit)
            ten_day_low = close.iloc[-self.exit_period:].min()
            if current_price <= max(stop_price, ten_day_low):
                exits.append({
                    'ticker': ticker, 'reason': 'trailing_stop',
                    'exit_price': current_price,
                    'details': f'Price {current_price:.2f} <= {self.exit_period}-day low {ten_day_low:.2f}',
                })
                continue

            # 2. Hard stop
            if stop_price and current_price <= stop_price:
                exits.append({
                    'ticker': ticker, 'reason': 'stop_hit',
                    'exit_price': current_price,
                    'details': f'Price {current_price:.2f} <= stop {stop_price:.2f}',
                })
                continue

            # 3. Time exit
            if days_held >= self.max_hold_days:
                exits.append({
                    'ticker': ticker, 'reason': 'time_exit',
                    'exit_price': current_price,
                    'details': f'Held {days_held} days >= max {self.max_hold_days}',
                })

        return exits


# Default parameter grid for autoresearch sweeper
PARAM_GRID = {
    'entry_period':  [10, 20, 30, 55],
    'exit_period':   [5, 10, 15, 20],
    'atr_stop_mult': [1.5, 2.0, 2.5, 3.0],
    'max_hold_days': [10, 20, 30, 55],
}
```

---

## Sandbox Rules

New strategies live in **`research/strategies/`** (the sandbox), not `strategies/` (the production folder). They go through a promotion lifecycle before touching live trades.

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

### Generating a scaffold

```python
from research.strategy_factory import build_strategy

result = build_strategy(
    'my_strategy',
    description='Enter when X, exit when Y. One paragraph max.',
    reference='Author, Paper Title (Year)',
)

# result['file_path']    → research/strategies/my_strategy.py
# result['vault_card']   → research/vault/Strategies/My Strategy.md
# result['validation']   → {"valid": True, "class_name": "MyStrategy", ...}
# result['success']      → True if importable and methods callable
```

### Validating before queuing

```python
from research.strategy_factory import validate_strategy

v = validate_strategy('my_strategy')
# {"valid": True, "class_name": "MyStrategy",
#  "has_signals": True, "has_exits": True, "errors": []}

if not v['valid']:
    print(v['errors'])   # fix these before queuing
```

### PARAM_GRID convention

Every sandbox strategy should export `PARAM_GRID` at module level. The sweeper picks it up automatically:

```python
# At the bottom of research/strategies/my_strategy.py
PARAM_GRID = {
    'rsi_period':    [5, 7, 10, 14, 21],
    'atr_stop_mult': [1.5, 2.0, 2.5, 3.0],
    'max_hold_days': [5, 10, 15, 20],
}
```

---

## Sanity Check Usage (`scripts/sanity_check.py`)

`sanity_check.py` is the pre-flight gate before adding a strategy to the research queue. It catches structural errors, import failures, and logic bugs that would waste compute time in the full backtest.

### Usage

```bash
# Validate strategy code only
python3 scripts/sanity_check.py --strategy donchian_breakout

# Validate + run a live signal check (uses cached data, ~5s)
python3 scripts/sanity_check.py --strategy donchian_breakout --signals

# Validate + queue a solo experiment (adds to research/queue.json)
python3 scripts/sanity_check.py --strategy donchian_breakout --queue

# Validate + queue a full_optimization experiment
python3 scripts/sanity_check.py --strategy donchian_breakout --queue --method full_optimization
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

result = check_strategy('donchian_breakout', run_signals=True)
# {
#   "valid": True,
#   "checks": ["file_exists", "importable", "class_found", "instantiable",
#               "has_signals", "has_exits", "signals_valid"],
#   "warnings": ["PARAM_GRID not found — sweeper will skip this strategy"],
#   "errors": [],
# }
```

---

## Queue Format (`research/queue.json`)

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

### Method types and `params_override` schemas

#### `single_strategy_test`
Run a solo backtest. `params_override` can be null or a flat dict of param overrides:

```json
{
  "method": "single_strategy_test",
  "strategy_name": "donchian_breakout",
  "params_override": {"entry_period": 55, "exit_period": 20}
}
```

#### `param_sweep`
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

#### `filter_test`
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

#### `full_optimization`
Coordinate-descent optimization. Requires `param_grid`:

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

#### `combined_portfolio_test`
Test portfolio impact of adding the strategy to active strategies:

```json
{
  "method": "combined_portfolio_test",
  "strategy_name": "donchian_breakout",
  "params_override": null
}
```

#### `oos_validation`
Out-of-sample test. Uses held-out data period:

```json
{
  "method": "oos_validation",
  "strategy_name": "donchian_breakout",
  "params_override": null
}
```

### Priority levels

| Priority | Use for |
|---|---|
| `P1` | Degradation fixes, broken strategies, critical bugs |
| `P2` | Dormant strategy activation, known improvements from research |
| `P3` | Parameter drift correction, new filters, known Tier 1 strategies |
| `P4` | New unproven strategies, exploratory research |
| `P5` | Long-term ideas, cross-market, speculative |

### Adding to queue programmatically

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

### Via CLI (`sanity_check.py`)

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

## Common Failures & Fixes

### 1. `stop_price >= entry_price`

**Error:** `ValueError: Stop price (185.00) must be below entry price (185.00) for long positions`

**Cause:** Stop calculated from current price using ATR, but ATR is huge or price is very low.

**Fix:**
```python
stop_price = entry_price - self.atr_stop_mult * current_atr
if stop_price <= 0 or stop_price >= entry_price:
    self._logger.debug(f'{ticker}: invalid stop {stop_price:.2f}, skipping')
    continue
```

### 2. Zero signals generated

**Symptom:** `quick_check()` returns `{"alive": False, "signal_count": 0, "reason": "No signals generated"}`

**Common causes and fixes:**

| Cause | Fix |
|---|---|
| Too many position guards hit | Pass `existing_positions=[]` in screening mode |
| `min_rows` guard too strict | Lower to `max(period, 50) + 5` for screening |
| Signal condition is logically impossible | Print intermediate values; check threshold direction |
| Data has NaN in indicators | Add `if pd.isna(value): continue` |

```python
# Debug: log intermediate values
self._logger.debug(f'{ticker}: rsi={current_rsi:.1f} zscore={current_zscore:.2f}')
```

### 3. Strategy not found in registry

**Error:** `strategy_name='donchian_breakout' not found in STRATEGY_REGISTRY or sandbox`

**Cause:** File exists but has the wrong class name or is in the wrong directory.

**Fix:** Sandbox strategies must be in `research/strategies/`, not `strategies/`.
```bash
ls research/strategies/donchian_breakout.py   # ✅ correct location
ls strategies/donchian_breakout.py             # ❌ only for promoted strategies
```

### 4. `param_sweep` validation fails

**Error:** `param_sweep requires params_override.sweep_param (str) (found 'sweep_params' — use singular)`

**Fix:** Use exact keys `"sweep_param"` (str) and `"sweep_values"` (list):
```python
# ❌ wrong
params_override = {"sweep_params": ["rsi_period"], "values": [5, 7, 14]}

# ✅ correct
params_override = {"sweep_param": "rsi_period", "sweep_values": [5, 7, 14]}
```

### 5. Trades collapse (< 10)

**Symptom:** `DISCARD: Trades collapsed: 3 < 21 (70% of 30)`

**Cause:** A filter is too strict, or the entry condition is rarely true.

**Fix:** Loosen the filter condition or check if the strategy has poor signal frequency on the market:
```python
# Typical min trade guard
if e_trades < max(10, int(b_trades * 0.7)):
    # Discard — filter too aggressive
```

### 6. `full_optimization` missing `param_grid`

**Error:** `full_optimization with strategy_name requires params_override.param_grid`

**Fix:**
```python
# ❌ wrong
params_override = {"optimize_params": {...}}

# ✅ correct
params_override = {"param_grid": {"rsi_period": [5, 7, 14], "atr_stop": [1.5, 2.0, 2.5]}}
```

### 7. NaN in indicators causes silent skip

**Symptom:** Strategy is alive but generates fewer signals than expected.

**Fix:** Always guard against NaN after indicator computation:
```python
atr_val = atr.iloc[-1]
if pd.isna(atr_val) or atr_val <= 0:
    self._logger.debug(f'{ticker}: invalid ATR, skipping')
    continue
```

### 8. Drawdown explosion from no stop loss

**Symptom:** `DISCARD: Drawdown exploded: 45.2% > 30.0%`

**Fix:** All strategies must set `stop_price` below entry. Minimum ATR-based stop:
```python
stop_price = entry_price - 2.0 * current_atr   # 2 ATR below entry
```

### 9. Look-ahead bias in exit check

**Symptom:** Unrealistically high Sharpe (> 3.0) in backtest, collapses in OOS.

**Cause:** Exit check uses `df.iloc[-1]` (today) instead of `df.iloc[-2]` (yesterday's confirmed close).

**Fix:** The backtest engine feeds data up to and including the decision bar. Use yesterday's data for exit decisions on today's open:
```python
# ✅ Use yesterday's close to decide today's exit
prev_close = close.iloc[-2]   # confirmed yesterday
current_close = close.iloc[-1]  # today — use for price only, not decisions
```

### 10. Strategy generates signals but never exits

**Symptom:** All positions hit `time_exit` with negative P&L, no mean-reversion exits.

**Cause:** Exit condition check is wrong — e.g. comparing wrong column, off-by-one on mean.

**Fix:** Add a debug exit log:
```python
self._logger.debug(f'{ticker}: close={current_price:.2f} mean20={mean_20:.2f} stop={stop_price:.2f}')
```

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
   → If Sharpe > 0.3: combined_test('name', best_params)

7. Promotion (human approval required)
   → python3 scripts/research_promote.py --stage --experiment-id autoresearch --market sp500
   → NEVER auto-promote
```

---

## Promotion Rules

**Never auto-promote.** Always require human approval. A strategy must:

1. Solo Sharpe > 0.3 (sustained, not single lucky run)
2. Trades ≥ 30 in full IS period
3. Max drawdown < 20%
4. Combined portfolio test: delta Sharpe > -0.02
5. OOS validation Sharpe within 20% of IS Sharpe

**Promotion path:**
```python
# Stage candidate config (not live yet)
from utils.config import get_active_config
config = get_active_config('sp500')
config['strategies']['donchian_breakout'] = {**best_params, 'enabled': True}

from pathlib import Path
import json
candidate = Path('/root/atlas/config/candidates/sp500_donchian.json')
candidate.write_text(json.dumps(config, indent=2))
print(f'Staged: {candidate}')

# Then notify for human review — STOP HERE
```

---

## Utilities Reference

```python
from research.loop import (
    ResearchSession,     # Full experiment session (baseline → experiment → keep/discard)
    leaderboard,         # Ranked table of all strategies by best Sharpe
    strategy_status,     # Universe: what exists, what's been tested
    quick_check,         # <10s screen: alive? how many signals? quick Sharpe?
    combined_test,       # Portfolio impact test
    load_best,           # Load best-known params from research/best/{strategy}.json
    read_results,        # Last N rows of research/results/{strategy}.tsv
)

from research.strategy_factory import (
    build_strategy,      # scaffold + validate + create vault card
    validate_strategy,   # import + structure check only
    generate_strategy_file,
    create_strategy_vault_card,
)

from research.models import (
    QueueEntry,          # Queue entry dataclass
    ExperimentType,      # Method enum
    Priority,            # P1–P5 enum
    append_to_queue,     # Thread-safe append to queue.json
    read_queue,          # Read all entries
    validate_queue_entry,  # Pre-flight validation
)

from research.discovery import (
    STRATEGY_UNIVERSE,   # All known strategies + status + reference
    get_unbuilt_strategies,   # Tier 1 strategies with status 'not_built'
    get_untested_existing,    # Existing strategies not yet solo-tested
)

from utils.helpers import (
    calc_atr,            # ATR(period) → pd.Series
    calc_rsi,            # RSI(period) → pd.Series
    calc_zscore,         # Rolling Z-score → pd.Series
    calc_position_size,  # Risk-based position sizing → dict(shares, position_value, total_risk)
    calc_volume_ratio,   # Volume / rolling avg → pd.Series
    calc_ibs,            # Intrabar strength = (close-low)/(high-low)
)
```
