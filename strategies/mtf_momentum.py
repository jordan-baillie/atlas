"""
Atlas-ASX Multi-Timeframe Momentum Strategy
=============================================
Identifies stocks in strong weekly uptrends that are pulling back on the
daily timeframe, providing high-probability entries in the direction of
the larger trend.

Weekly Timeframe (trend confirmation):
    - Price > 20-week SMA
    - Weekly RSI(14) > configurable minimum (default 50)
    - Optional: Positive weekly MACD histogram

Daily Timeframe (pullback entry):
    - Daily RSI(14) < configurable threshold (default 40)
    - Price within X% of daily 20-SMA (near support)
    - Volume not dried up (minimum volume ratio)

Config Section: strategies.mtf_momentum

Usage:
    from strategies.mtf_momentum import MTFMomentum
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size, calc_volume_ratio

logger = logging.getLogger(__name__)


class MTFMomentum(BaseStrategy):
    """Multi-Timeframe Momentum: enter daily pullbacks within weekly uptrends."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("mtf_momentum", {})
        # Weekly parameters
        self.weekly_sma_period = strat_cfg.get("weekly_sma_period", 20)
        self.weekly_rsi_period = strat_cfg.get("weekly_rsi_period", 14)
        self.weekly_rsi_min = strat_cfg.get("weekly_rsi_min", 50)
        # Daily parameters
        self.daily_rsi_period = strat_cfg.get("daily_rsi_period", 14)
        self.daily_rsi_max = strat_cfg.get("daily_rsi_max", 40)
        self.daily_sma_period = strat_cfg.get("daily_sma_period", 20)
        self.pullback_sma_pct = strat_cfg.get("pullback_sma_pct", 0.03)
        # Risk / exit parameters
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.5)
        self.trailing_stop_atr_mult = strat_cfg.get("trailing_stop_atr_mult", 3.0)
        self.max_hold_days = strat_cfg.get("max_hold_days", 15)
        # Filters
        self.use_macd_filter = strat_cfg.get("use_macd_filter", True)
        self.vol_min_ratio = strat_cfg.get("vol_min_ratio", 0.5)

        self._logger.info(
            "MTFMomentum initialized: weekly_sma=%d, weekly_rsi=%d(min=%d), "
            "daily_rsi=%d(max=%d), daily_sma=%d, pullback=%.1f%%, "
            "atr=%d, stop=%.1fx, trail=%.1fx, max_hold=%d, macd=%s",
            self.weekly_sma_period, self.weekly_rsi_period, self.weekly_rsi_min,
            self.daily_rsi_period, self.daily_rsi_max, self.daily_sma_period,
            self.pullback_sma_pct * 100,
            self.atr_period, self.atr_stop_mult, self.trailing_stop_atr_mult,
            self.max_hold_days, self.use_macd_filter,
        )

    @property
    def name(self) -> str:
        return "mtf_momentum"

    @staticmethod
    def _resample_weekly(df: pd.DataFrame) -> pd.DataFrame:
        """Resample daily OHLCV data to weekly bars.

        Args:
            df: Daily DataFrame with open, high, low, close, volume columns
                and a DatetimeIndex.

        Returns:
            Weekly DataFrame with the same columns.  Incomplete trailing
            weeks are included so the latest week is always present.
        """
        weekly = df.resample("W").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
