"""
Atlas Volume Climax Strategy
========================================
Extreme volume spike (>3x avg) on a down day in uptrend = capitulation selling.
Buy reversal. Exit: first up close or time stop.

Reference: Quantified Strategies volume research, Wyckoff method
Config Section: strategies.volume_climax
"""

import logging
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_position_size, calc_volume_ratio

logger = logging.getLogger(__name__)


class VolumeClimax(BaseStrategy):
    """Buy after capitulation volume spike on a down day in uptrend."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("volume_climax", {})

        self.volume_mult = strat_cfg.get("volume_mult", 3.0)
        self.volume_lookback = strat_cfg.get("volume_lookback", 20)
        self.sma200_filter = strat_cfg.get("sma200_filter", True)
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)
        self.max_hold_days = strat_cfg.get("max_hold_days", 5)

        self._logger.info(
            f"VolumeClimax initialized: vol_mult={self.volume_mult}, "
            f"lookback={self.volume_lookback}, sma200={'ON' if self.sma200_filter else 'OFF'}"
        )

    @property
    def name(self) -> str:
        return "volume_climax"

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
            if not self._has_sufficient_data(df, max(self.volume_lookback, 200) + 10):
                continue

            close = df["close"]
            open_ = df["open"]
            high = df["high"]
            low = df["low"]
            volume = df["volume"]

            if self.sma200_filter:
                sma200 = close.rolling(200).mean()
                if pd.isna(sma200.iloc[-1]) or close.iloc[-1] <= sma200.iloc[-1]:
                    continue

            vol_ratio = calc_volume_ratio(volume, lookback=self.volume_lookback)
            if pd.isna(vol_ratio.iloc[-1]):
                continue

            is_down_day = close.iloc[-1] < open_.iloc[-1]
            is_volume_spike = vol_ratio.iloc[-1] >= self.volume_mult

            if not (is_down_day and is_volume_spike):
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

            vol_ratio_val = float(vol_ratio.iloc[-1])
            # Confidence: stronger volume spike = higher confidence in capitulation
            confidence = float(min(0.65 + (vol_ratio_val / self.volume_mult - 1.0) * 0.10, 0.95))
            down_pct = float((open_.iloc[-1] - close.iloc[-1]) / open_.iloc[-1] * 100)

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
                    f"{ticker}: volume climax {vol_ratio_val:.1f}x avg on down day "
                    f"({down_pct:.1f}% drop), potential capitulation"
                ),
                features={
                    "volume_ratio": vol_ratio_val,
                    "atr": atr_val,
                    "down_pct": down_pct,
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

            # Strength exit: first up close after capitulation
            if df["close"].iloc[-1] > df["open"].iloc[-1]:
                exits.append({
                    "ticker": ticker, "reason": "strength_exit",
                    "exit_price": current_price,
                    "details": f"Up close: {current_price:.2f} > open {df["open"].iloc[-1]:.2f}",
                })

        return exits


PARAM_GRID = {
    "volume_mult": [2.0, 2.5, 3.0, 4.0],
    "atr_stop_mult": [1.5, 2.0, 2.5],
    "max_hold_days": [3, 5, 7, 10],
}
