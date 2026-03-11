"""Atlas Ex-Dividend Franking Credit Capture Strategy
========================================================
Exploits the unique Australian dividend imputation (franking credit)
system. Fully franked dividends carry a 30% tax credit making the total
pre-tax value significantly higher than the cash dividend alone.

Academic research (Beggs & Skeels 2006) shows stocks don't fall by the
full grossed-up dividend value on ex-date, creating a systematic positive
return for holding through ex-dividend.

Logic:
  1. Screen for upcoming ex-dividend dates with high franking (>75%)
  2. Enter N days BEFORE ex-date (capture pre-dividend run-up)
  3. Hold through ex-date to capture dividend + franking credit
  4. Exit M days after ex-date (capture partial recovery)
  5. Use wider ATR stops (event-driven, expect volatility)

Config Section: strategies.dividend_capture

Usage:
    from strategies.dividend_capture import DividendCapture
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_position_size, calc_rsi, calc_volume_ratio
from utils.dividends import (
    fetch_dividend_calendar,
    estimate_franking_pct,
    calc_grossed_up_yield,
    get_sector_for_ticker,
)

logger = logging.getLogger(__name__)


class DividendCapture(BaseStrategy):
    """Ex-dividend franking credit capture: enter before ex-date, hold through for dividend + recovery."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("dividend_capture", {})

        # Entry timing
        self.days_before_ex = strat_cfg.get("days_before_ex", 5)
        self.days_after_ex = strat_cfg.get("days_after_ex", 5)

        # Dividend filters
        self.min_franking_pct = strat_cfg.get("min_franking_pct", 75) / 100.0
        self.min_grossed_up_yield = strat_cfg.get("min_grossed_up_yield", 0.015)

        # Direction / quality filters
        self.require_uptrend = strat_cfg.get("require_uptrend", False)
        self.rsi_max = strat_cfg.get("rsi_max", 70)
        self.sma_trend_period = strat_cfg.get("sma_trend_period", 50)

        # Stops and holds
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 3.0)
        self.max_hold_days = strat_cfg.get("max_hold_days", 20)

        # Internal caches
        self._div_cache: Dict[str, List[Dict]] = {}
        self._sector_cache: Dict[str, str] = {}
