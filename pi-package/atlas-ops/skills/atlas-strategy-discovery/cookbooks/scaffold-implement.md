# Cookbook: Scaffold & Implement a Strategy

Use this cookbook when building a new Atlas strategy from scratch.

---

## 1. Generate a Scaffold

```python
import sys; sys.path.insert(0, '/root/atlas')
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

Edit `research/strategies/my_strategy.py` — implement `generate_signals()` and `check_exits()`, then add `PARAM_GRID` at the bottom.

---

## 2. BaseStrategy Interface

Every Atlas strategy inherits from `strategies.base.BaseStrategy`. Two abstract methods **must** be implemented:

```python
from strategies.base import BaseStrategy, Signal
from typing import Any, Dict, List
import pandas as pd

class MyStrategy(BaseStrategy):
    """One-sentence description."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
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

---

## 3. Signal Dataclass

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

---

## 4. BaseStrategy Helpers

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

## 5. Working Example — Donchian Breakout

Full implementation showing all patterns (trend-following: buy 20-day high breakout, stop on 10-day low):

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
        self.entry_period  = strat_cfg.get('entry_period', 20)
        self.exit_period   = strat_cfg.get('exit_period', 10)
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
                atr = calc_atr(high, low, close, period=self.atr_period)
                current_atr = atr.iloc[-1]
                if pd.isna(current_atr) or current_atr <= 0:
                    continue

                entry_price = close.iloc[-1]
                stop_price  = entry_price - self.atr_stop_mult * current_atr
                if stop_price <= 0 or stop_price >= entry_price:
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

            # 1. Donchian trailing stop (10-day low)
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

## 6. Utilities Reference

```python
from utils.helpers import (
    calc_atr,            # ATR(period) → pd.Series
    calc_rsi,            # RSI(period) → pd.Series
    calc_zscore,         # Rolling Z-score → pd.Series
    calc_position_size,  # Risk-based position sizing → dict(shares, position_value, total_risk)
    calc_volume_ratio,   # Volume / rolling avg → pd.Series
    calc_ibs,            # Intrabar strength = (close-low)/(high-low)
)

from research.strategy_factory import (
    build_strategy,              # scaffold + validate + create vault card
    validate_strategy,           # import + structure check only
    generate_strategy_file,
    create_strategy_vault_card,
)

from research.loop import (
    ResearchSession,     # Full experiment session (baseline → experiment → keep/discard)
    leaderboard,         # Ranked table of all strategies by best Sharpe
    strategy_status,     # Universe: what exists, what's been tested
    quick_check,         # <10s screen: alive? how many signals? quick Sharpe?
    combined_test,       # Portfolio impact test
    load_best,           # Load best-known params from research/best/{strategy}.json
    read_results,        # Last N rows of research/results/{strategy}.tsv
)

from research.discovery import (
    STRATEGY_UNIVERSE,        # All known strategies + status + reference
    get_unbuilt_strategies,   # Tier 1 strategies with status 'not_built'
    get_untested_existing,    # Existing strategies not yet solo-tested
)
```

---

**Next step:** Validate your implementation → Load cookbook: `cookbooks/validate-screen.md`
