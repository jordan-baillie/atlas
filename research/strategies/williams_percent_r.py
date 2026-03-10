"""
Atlas Williams Percent R Strategy
========================================
Williams %R oversold (<-80) with trend filter. Exit on %R > -20 or time stop.

Reference: Larry Williams 'How I Made $1M' (1979)
Generated: 2026-03-10T07:18:31.201022+00:00

Config Section: strategies.williams_percent_r
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size

logger = logging.getLogger(__name__)


class WilliamsPercentR(BaseStrategy):
    """Williams %R oversold (<-80) with trend filter"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("williams_percent_r", {})

        # Core parameters
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)
        self.max_hold_days = strat_cfg.get("max_hold_days", 10)
        self.sma200_filter = strat_cfg.get("sma200_filter", True)
        # TODO: Add strategy-specific parameters from description

        self._logger.info(f"WilliamsPercentR initialized")

    @property
    def name(self) -> str:
        return "williams_percent_r"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Generate williams_percent_r entry signals."""
        signals: List[Signal] = []
        held = self._get_held_tickers(existing_positions)

        for ticker, df in data.items():
            if ticker in held:
                continue
            if not self._can_open_position(existing_positions):
                break
            if not self._has_sufficient_data(df, 252):
                continue

            # TODO: Implement entry logic
            # Williams %R oversold (<-80) with trend filter. Exit on %R > -20 or time stop.
            pass

        self._logger.info(f"{self.name}: {len(signals)} signals from {len(data)} tickers")
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check positions for exit conditions."""
        exits = []
        for pos in positions:
            if pos.get("strategy") != self.name:
                continue
            ticker = pos.get("ticker")
            if not ticker or ticker not in data:
                continue

            df = data[ticker]
            if df.empty:
                continue

            current_price = float(df["close"].iloc[-1])
            stop_price = pos.get("stop_price", 0)

            # Stop-loss
            if stop_price and current_price <= stop_price:
                exits.append({
                    "ticker": ticker,
                    "reason": "stop_hit",
                    "exit_price": current_price,
                    "details": f"Price {current_price:.2f} <= stop {stop_price:.2f}",
                })
                continue

            # Time exit
            entry_date = pos.get("entry_date")
            if entry_date:
                if isinstance(entry_date, str):
                    entry_date = pd.Timestamp(entry_date)
                days_held = (df.index[-1] - entry_date).days
                if days_held >= self.max_hold_days:
                    exits.append({
                        "ticker": ticker,
                        "reason": "time_exit",
                        "exit_price": current_price,
                        "details": f"Held {days_held} days >= max {self.max_hold_days}",
                    })
                    continue

            # TODO: Add strategy-specific exit logic

        return exits


# Default parameter grid for optimization
PARAM_GRID = {
    "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
    "max_hold_days": [5, 10, 15, 20],
}
