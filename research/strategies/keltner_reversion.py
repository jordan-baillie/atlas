"""
Atlas Keltner Reversion Strategy
========================================
Price touches lower Keltner Channel (EMA ± ATR mult) -> buy.
Exit at middle band (EMA). Uptrend filter.

Reference: Chester Keltner (1960), modernized by Linda Bradford Raschke
Config Section: strategies.keltner_reversion
"""

import logging
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_position_size

logger = logging.getLogger(__name__)


class KeltnerReversion(BaseStrategy):
    """Buy at lower Keltner Channel, exit at middle band (EMA)."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("keltner_reversion", {})

        self.ema_period = strat_cfg.get("ema_period", 20)
        self.atr_mult = strat_cfg.get("atr_mult", 2.0)
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.sma200_filter = strat_cfg.get("sma200_filter", True)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.5)
        self.max_hold_days = strat_cfg.get("max_hold_days", 10)

        self._logger.info(
            f"KeltnerReversion initialized: ema={self.ema_period}, "
            f"atr_mult={self.atr_mult}, sma200={'ON' if self.sma200_filter else 'OFF'}"
        )

    @property
    def name(self) -> str:
        return "keltner_reversion"

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
            if not self._has_sufficient_data(df, max(self.ema_period, 200) + 10):
                continue

            close = df["close"]
            high = df["high"]
            low = df["low"]

            # SMA-200 uptrend filter
            if self.sma200_filter:
                sma200 = close.rolling(200).mean()
                if pd.isna(sma200.iloc[-1]) or close.iloc[-1] <= sma200.iloc[-1]:
                    continue

            # Keltner Channel
            ema = close.ewm(span=self.ema_period, adjust=False).mean()
            atr = calc_atr(high, low, close, period=self.atr_period)
            lower_band = ema - self.atr_mult * atr

            if pd.isna(lower_band.iloc[-1]) or pd.isna(atr.iloc[-1]):
                continue
            if close.iloc[-1] >= lower_band.iloc[-1]:
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

            depth = (lower_band.iloc[-1] - close.iloc[-1]) / atr_val
            confidence = float(min(0.65 + depth * 0.10, 0.95))

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
                    f"{ticker}: close {entry_price:.2f} below lower Keltner band "
                    f"{lower_band.iloc[-1]:.2f} by {depth:.2f} ATR"
                ),
                features={
                    "ema": float(ema.iloc[-1]),
                    "lower_band": float(lower_band.iloc[-1]),
                    "atr": atr_val,
                    "depth_atr": float(depth),
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

            close = df["close"]
            ema = close.ewm(span=self.ema_period, adjust=False).mean()
            if not pd.isna(ema.iloc[-1]) and current_price > ema.iloc[-1]:
                exits.append({
                    "ticker": ticker, "reason": "ema_exit",
                    "exit_price": current_price,
                    "details": f"Price {current_price:.2f} > EMA({self.ema_period}) {ema.iloc[-1]:.2f}",
                })

        return exits


PARAM_GRID = {
    "ema_period": [10, 20, 30],
    "atr_mult": [1.5, 2.0, 2.5, 3.0],
    "atr_stop_mult": [2.0, 2.5, 3.0],
    "max_hold_days": [5, 10, 15],
}
