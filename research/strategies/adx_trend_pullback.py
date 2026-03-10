"""
Atlas Adx Trend Pullback Strategy
========================================
ADX > 25 (strong trend) + pullback to 20-EMA. Enter on bounce from EMA. Exit: trailing ATR stop.

Reference: Welles Wilder 'New Concepts in Technical Trading' (1978)
Generated: 2026-03-10T07:18:31.203810+00:00

Config Section: strategies.adx_trend_pullback
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size

logger = logging.getLogger(__name__)


class AdxTrendPullback(BaseStrategy):
    """ADX > 25 (strong trend) + pullback to 20-EMA"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("adx_trend_pullback", {})

        # Core parameters
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)
        self.max_hold_days = strat_cfg.get("max_hold_days", 10)
        self.sma200_filter = strat_cfg.get("sma200_filter", True)
        # TODO: Add strategy-specific parameters from description

        self._logger.info(f"AdxTrendPullback initialized")

    @property
    def name(self) -> str:
        return "adx_trend_pullback"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Generate adx_trend_pullback entry signals."""
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
            # ADX > 25 (strong trend) + pullback to 20-EMA. Enter on bounce from EMA. Exit: trailing ATR stop.
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
