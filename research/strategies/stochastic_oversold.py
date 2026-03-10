"""
Atlas Stochastic Oversold Strategy
========================================
Stochastic %K < 20 and %D < 20 in uptrend (>SMA200). Exit on %K > 80 or time stop.

Reference: George Lane (1950s), quantified by Connors
Generated: 2026-03-10T07:18:31.202414+00:00

Config Section: strategies.stochastic_oversold
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size

logger = logging.getLogger(__name__)


class StochasticOversold(BaseStrategy):
    """Stochastic %K < 20 and %D < 20 in uptrend (>SMA200)"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("stochastic_oversold", {})

        # Core parameters
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)
        self.max_hold_days = strat_cfg.get("max_hold_days", 10)
        self.sma200_filter = strat_cfg.get("sma200_filter", True)
        # TODO: Add strategy-specific parameters from description

        self._logger.info(f"StochasticOversold initialized")

    @property
    def name(self) -> str:
        return "stochastic_oversold"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Generate stochastic_oversold entry signals."""
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
            # Stochastic %K < 20 and %D < 20 in uptrend (>SMA200). Exit on %K > 80 or time stop.
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
