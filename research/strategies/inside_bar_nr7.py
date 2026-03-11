"""
Atlas Inside Bar NR7 Strategy
========================================
NR7 (narrowest range of 7 days) -> breakout entry.
Enter when yesterday was NR7 day and today opens above yesterday's high.
Exit: trailing ATR stop or time-based.

Reference: Toby Crabel 'Day Trading with Short Term Price Patterns' (1990)
Config Section: strategies.inside_bar_nr7
"""

import logging
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_position_size

logger = logging.getLogger(__name__)


class InsideBarNr7(BaseStrategy):
    """Buy on breakout after NR7 (narrowest range of 7 days) pattern."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("inside_bar_nr7", {})

        self.nr_lookback = strat_cfg.get("nr_lookback", 7)
        self.sma200_filter = strat_cfg.get("sma200_filter", True)
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)
        self.max_hold_days = strat_cfg.get("max_hold_days", 5)

        self._logger.info(
            f"InsideBarNr7 initialized: lookback={self.nr_lookback}, "
            f"sma200={'ON' if self.sma200_filter else 'OFF'}"
        )

    @property
    def name(self) -> str:
        return "inside_bar_nr7"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        signals: List[Signal] = []
        held = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.01)

        for ticker, df in data.items():
            if ticker in held:
                continue
            if not self._can_open_position(existing_positions):
                break
            if not self._has_sufficient_data(df, max(self.nr_lookback, 200) + 10):
                continue

            close = df["close"]
            high = df["high"]
            low = df["low"]

            if self.sma200_filter:
                sma200 = close.rolling(200).mean()
                if pd.isna(sma200.iloc[-1]) or close.iloc[-1] <= sma200.iloc[-1]:
                    continue

            daily_range = high - low

            if len(daily_range) < self.nr_lookback + 1:
                continue

            yesterday_range = float(daily_range.iloc[-2])
            lookback_ranges = daily_range.iloc[-(self.nr_lookback + 1):-1]

            if yesterday_range <= 0:
                continue

            is_nr7 = yesterday_range <= lookback_ranges.min()
            if not is_nr7:
                continue

            yesterday_high = float(high.iloc[-2])
            if close.iloc[-1] <= yesterday_high:
                continue

            atr = calc_atr(high, low, close, period=self.atr_period)
            if pd.isna(atr.iloc[-1]):
                continue

            entry_price = float(close.iloc[-1])
            atr_val = float(atr.iloc[-1])
            stop_price = entry_price - self.atr_stop_mult * atr_val

            if stop_price <= 0 or entry_price <= stop_price:
                continue

            # FIX: calc_position_size returns dict -- extract shares from it
            pos = calc_position_size(equity, risk_pct, entry_price, stop_price)
            shares = pos["shares"]
            if shares <= 0:
                continue

            avg_range = float(lookback_ranges.mean())
            compression = avg_range / yesterday_range if yesterday_range > 0 else 1.0
            # Confidence: higher compression = stronger breakout
            confidence = float(min(0.65 + (compression - 1.0) * 0.05, 0.95))

            signals.append(Signal(
                ticker=ticker,
                strategy=self.name,
                direction="long",
                entry_price=entry_price,
                stop_price=stop_price,
                take_profit=None,
                position_size=shares,
                position_value=pos["position_value"],
                risk_amount=pos["total_risk"],
                confidence=confidence,
                rationale=(
                    f"{ticker}: NR7 breakout, yesterday range {yesterday_range:.2f} "
                    f"= narrowest of {self.nr_lookback}d, compression {compression:.2f}x"
                ),
                features={
                    "nr_range": yesterday_range,
                    "avg_range": avg_range,
                    "compression": compression,
                    "breakout_high": yesterday_high,
                    "atr": atr_val,
                },
            ))

        signals.sort(key=lambda s: s.confidence, reverse=True)
        self._logger.info(f"{self.name}: {len(signals)} signals from {len(data)} tickers")
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
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

            if stop_price and current_price <= stop_price:
                exits.append({
                    "ticker": ticker, "reason": "stop_hit",
                    "exit_price": current_price,
                    "details": f"Price {current_price:.2f} <= stop {stop_price:.2f}",
                })
                continue

            entry_date = pos.get("entry_date")
            if entry_date:
                if isinstance(entry_date, str):
                    entry_date = pd.Timestamp(entry_date)
                days_held = (df.index[-1] - entry_date).days
                if days_held >= self.max_hold_days:
                    exits.append({
                        "ticker": ticker, "reason": "time_exit",
                        "exit_price": current_price,
                        "details": f"Held {days_held}d >= max {self.max_hold_days}",
                    })
                    continue

            high = df["high"]
            low = df["low"]
            close = df["close"]
            atr = calc_atr(high, low, close, period=self.atr_period)
            if not pd.isna(atr.iloc[-1]):
                trailing_stop = current_price - self.atr_stop_mult * float(atr.iloc[-1])
                if trailing_stop > stop_price and current_price <= trailing_stop:
                    exits.append({
                        "ticker": ticker, "reason": "trailing_stop",
                        "exit_price": current_price,
                        "details": f"Trailing stop {trailing_stop:.2f} hit",
                    })

        return exits


PARAM_GRID = {
    "nr_lookback": [5, 7, 10],
    "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
    "max_hold_days": [3, 5, 7, 10],
}
