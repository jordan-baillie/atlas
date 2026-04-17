# Strategy Code Generator — Write a Python Strategy File for Atlas

You are an expert Python developer implementing algorithmic trading strategies for the Atlas trading system. Your task is to write a complete, working Python strategy file based on the structured spec provided, then confirm the result as JSON.

## Strategy Spec

```json
{spec_json}
```

## Output File Location

Write the strategy file to:

    {strategies_dir}/{strategy_name}.py

The Atlas root directory is: `{atlas_root}`

## Atlas Strategy Interface

Your class must subclass `BaseStrategy` from `strategies.base` and implement the full interface. Study this carefully — every detail matters.

### Imports (required)

```python
import logging
from datetime import datetime
from typing import Any, Dict, List

import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size
```

Additional helpers available if needed:
- `from utils.helpers import calc_zscore, calc_volume_ratio, calc_ibs, calc_wvf`

### Class Structure

```python
class StrategyClassName(BaseStrategy):
    """
    One-line summary of the strategy.

    Strategy: <description from spec>
    
    Entry Conditions:
        - <entry rule 1>
        - <entry rule 2>
    
    Stop Loss:
        - <stop method from spec>
    
    Take Profit:
        - <take profit method from spec>
    
    Reference:
        <paper title> — <paper URL>
    
    Config Section: strategies.{strategy_name}
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("{strategy_name}", {})
        # Read every parameter from strat_cfg with a sensible default from the spec
        self.param_name = strat_cfg.get("param_name", default_value)
        # ... all parameters from spec["parameters"] ...

    @property
    def name(self) -> str:
        return "{strategy_name}"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Generate long entry signals.
        
        data: dict of ticker -> OHLCV DataFrame with columns:
              open, high, low, close, adj_close, volume, ticker
              Index is DatetimeIndex.
        equity: current account equity in USD.
        existing_positions: list of dicts with keys: ticker, strategy,
                            entry_price, shares, entry_date, stop_price.
        Returns: List[Signal]
        """
        signals = []
        held_tickers = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0008)
        min_position_value = self.fees_config.get("min_position_value", 0.0)
        max_position_value = self.config.get("trading", {}).get(
            "live_safety", {}
        ).get("max_order_value", 0.0)

        min_rows = <computed from indicator lookbacks + buffer>

        for ticker, df in data.items():
            try:
                if ticker in held_tickers:
                    continue
                if not self._can_open_position(existing_positions):
                    break
                if not self._has_sufficient_data(df, min_rows):
                    continue

                # --- Compute indicators ---
                # (use calc_atr, calc_rsi, etc. from utils.helpers)
                
                # --- Apply entry conditions from spec ---
                # (skip ticker with continue if conditions not met)
                
                # --- Compute stop and take-profit ---
                entry_price = df["close"].iloc[-1]
                stop_price = ...   # must be < entry_price
                take_profit = ...  # must be > entry_price (or None)

                # --- Size the position ---
                try:
                    pos = calc_position_size(
                        equity=equity,
                        risk_pct=risk_pct,
                        entry_price=entry_price,
                        stop_price=stop_price,
                        commission_per_trade=commission_per_trade,
                        commission_pct=commission_pct,
                        min_position_value=min_position_value,
                        max_position_value=max_position_value,
                    )
                except ValueError:
                    continue
                if pos["shares"] <= 0:
                    continue

                signal = Signal(
                    ticker=ticker,
                    strategy=self.name,
                    direction="long",   # ALWAYS "long"
                    entry_price=entry_price,
                    stop_price=round(stop_price, 4),
                    take_profit=round(take_profit, 4) if take_profit else None,
                    position_size=pos["shares"],
                    position_value=pos["position_value"],
                    risk_amount=pos["total_risk"],
                    confidence=<float 0.0-1.0>,
                    rationale=<descriptive string citing indicators and values>,
                    features=<dict of key indicator values>,
                    universe="sp500",
                )
                signals.append(signal)

            except Exception as e:
                self._logger.error(f"{ticker}: signal error: {e}", exc_info=True)
                continue

        self._logger.info(f"{self.name} generated {len(signals)} signals")
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check open positions for exit conditions.
        
        positions: list of dicts with keys: ticker, strategy, entry_price,
                   shares, entry_date, stop_price.
        Returns: list of exit dicts with keys:
                 ticker (str), reason (str), exit_price (float), details (str)
        
        reason must be one of: 'stop_hit', 'take_profit', 'signal_exit', 'time_exit'
        """
        exits = []
        for pos in positions:
            if pos.get("strategy") != self.name:
                continue
            ticker = pos["ticker"]
            df = data.get(ticker)
            if df is None or df.empty:
                continue
            try:
                close = df["close"]
                today_close = close.iloc[-1]
                today_date = df.index[-1]
                entry_date = pd.Timestamp(pos["entry_date"])
                entry_price = pos["entry_price"]
                stop_price = pos.get("stop_price", 0)
                take_profit = pos.get("take_profit")
                days_held = (today_date - entry_date).days

                # Priority 1: Hard stop
                if today_close <= stop_price:
                    exits.append({
                        "ticker": ticker, "reason": "stop_hit",
                        "exit_price": today_close,
                        "details": f"Stop hit at {stop_price:.2f}, close={today_close:.2f}",
                    })
                # Priority 2: Take profit
                elif take_profit and today_close >= take_profit:
                    exits.append({
                        "ticker": ticker, "reason": "take_profit",
                        "exit_price": today_close,
                        "details": f"Take profit at {take_profit:.2f}, close={today_close:.2f}",
                    })
                # Priority 3: Signal-based exit (implement per spec)
                # elif <signal exit condition from spec>:
                #     exits.append({"ticker": ticker, "reason": "signal_exit", ...})
                # Priority 4: Time exit
                elif hasattr(self, "max_hold_days") and days_held >= self.max_hold_days:
                    exits.append({
                        "ticker": ticker, "reason": "time_exit",
                        "exit_price": today_close,
                        "details": f"Time exit after {days_held} days (max={self.max_hold_days})",
                    })
            except Exception as e:
                self._logger.error(f"{ticker}: exit check error: {e}", exc_info=True)
                continue
        return exits
```

## Implementation Rules

1. **All parameters** from `spec["parameters"]` must appear in `__init__` reading from `strat_cfg` with a default.
2. **All entry rules** from `spec["entry_rules"]` must be evaluated in `generate_signals()`.
3. **All exit rules** from `spec["exit_rules"]` must be implemented in `check_exits()`.
4. **direction is always `"long"`** — do not generate short signals.
5. **`stop_price` must be strictly less than `entry_price`** — validate before creating Signal.
6. **`take_profit` must be strictly greater than `entry_price`** — validate or set to None.
7. **`position_size` must be > 0** — skip the ticker if `pos["shares"] <= 0`.
8. **Guard against insufficient data** — use `self._has_sufficient_data(df, min_rows)` where `min_rows` is at least the maximum indicator lookback + 10.
9. **Wrap each ticker in try/except** — log errors and continue; never raise from `generate_signals()` or `check_exits()`.
10. **NaN guard** — check `pd.isna()` on every indicator value before using it.
11. **Docstring** must reference the source paper title and URL from `spec["reference"]`.
12. **`calc_position_size`** signature: `(equity, risk_pct, entry_price, stop_price, commission_per_trade, commission_pct, min_position_value, max_position_value)` — returns dict with keys `shares`, `position_value`, `total_risk`.
13. **`calc_rsi(close, period)`** returns pd.Series (0–100). **`calc_atr(high, low, close, period)`** returns pd.Series. Both return NaN for insufficient data.

## Steps to Follow

1. **Write the file** using the **Write** tool to `{strategies_dir}/{strategy_name}.py`.
2. **Verify the file exists** using Bash: `python3 -c "import ast; ast.parse(open('{strategies_dir}/{strategy_name}.py').read()); print('syntax ok')"` — fix any syntax errors.
3. **Return** the JSON confirmation object described below.

## Output Format

After writing and verifying the file, return a **JSON object** with exactly these fields:

```json
{
  "strategy_name": "{strategy_name}",
  "file_path": "{strategies_dir}/{strategy_name}.py",
  "success": true,
  "lines_written": <integer — number of lines in the generated file>,
  "parameters_implemented": <list of parameter names read from config>,
  "entry_conditions_count": <integer — number of entry conditions from spec>,
  "exit_conditions_count": <integer — number of exit conditions implemented>
}
```

If writing or syntax-checking fails, return:

```json
{
  "strategy_name": "{strategy_name}",
  "file_path": "{strategies_dir}/{strategy_name}.py",
  "success": false,
  "error": "<concise description of what failed>"
}
```

**Output only the raw JSON object — no markdown fences, no preamble, no explanation outside the JSON.**
