"""
Atlas Multi-Timeframe Momentum Strategy
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
        }).dropna()

        return weekly

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Generate MTF momentum signals.

        Full implementation: weekly trend confirmation
        with daily pullback entry logic.
        """
        signals: List[Signal] = []

        for ticker, df in data.items():
            try:
                if len(df) < max(self.weekly_sma_period * 5, 100):
                    continue

                # Ensure DatetimeIndex
                if not isinstance(df.index, pd.DatetimeIndex):
                    continue

                # Weekly trend check
                weekly = self._resample_weekly(df)
                if len(weekly) < self.weekly_sma_period:
                    continue

                weekly_sma = weekly["close"].rolling(self.weekly_sma_period).mean()
                weekly_rsi = calc_rsi(weekly["close"], self.weekly_rsi_period)

                if weekly["close"].iloc[-1] <= weekly_sma.iloc[-1]:
                    continue
                if weekly_rsi.iloc[-1] < self.weekly_rsi_min:
                    continue

                # Daily pullback check
                daily_rsi = calc_rsi(df["close"], self.daily_rsi_period)
                daily_sma = df["close"].rolling(self.daily_sma_period).mean()
                atr_series = calc_atr(df["high"], df["low"], df["close"], self.atr_period)
                current_atr = float(atr_series.iloc[-1])

                if daily_rsi.iloc[-1] >= self.daily_rsi_max:
                    continue

                today_close = df["close"].iloc[-1]
                sma_val = daily_sma.iloc[-1]
                if pd.isna(sma_val) or pd.isna(current_atr):
                    continue

                pct_from_sma = abs(today_close - sma_val) / sma_val
                if pct_from_sma > self.pullback_sma_pct:
                    continue

                vol_ratio = calc_volume_ratio(df["volume"])
                if vol_ratio.iloc[-1] < self.vol_min_ratio:
                    continue

                entry_price = today_close
                stop_price = entry_price - self.atr_stop_mult * current_atr
                take_profit = entry_price + 3.0 * current_atr

                risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
                commission_per_trade = self.fees_config.get("commission_per_trade", 1.1)
                commission_pct = self.fees_config.get("commission_pct", 0.001)
                pos = calc_position_size(
                    equity=equity,
                    risk_pct=risk_pct,
                    entry_price=entry_price,
                    stop_price=stop_price,
                    commission_per_trade=commission_per_trade,
                    commission_pct=commission_pct,
                )
                if pos["shares"] < 1:
                    continue

                confidence = 0.5
                rationale = (
                    f"{ticker} MTF momentum: weekly uptrend confirmed "
                    f"(RSI={weekly_rsi.iloc[-1]:.1f}), daily pullback "
                    f"(RSI={daily_rsi.iloc[-1]:.1f}), near SMA support."
                )

                signal = Signal(
                    ticker=ticker,
                    strategy=self.name,
                    direction="long",
                    entry_price=entry_price,
                    stop_price=round(stop_price, 4),
                    take_profit=round(take_profit, 4),
                    position_size=pos["shares"],
                    position_value=pos["position_value"],
                    risk_amount=pos["total_risk"],
                    confidence=round(confidence, 4),
                    rationale=rationale,
                    features={
                        "weekly_rsi": round(weekly_rsi.iloc[-1], 2),
                        "daily_rsi": round(daily_rsi.iloc[-1], 2),
                        "pct_from_sma": round(pct_from_sma, 4),
                        "atr": round(current_atr, 4),
                        "close": round(today_close, 4),
                    },
                    timestamp=datetime.now(),
                )
                signals.append(signal)
                self._logger.info(f"SIGNAL: {signal}")

            except Exception as e:
                self._logger.error(
                    f"{ticker}: signal generation error: {e}", exc_info=True
                )
                continue

        return signals

    def check_exits(
        self, data: Dict[str, pd.DataFrame], positions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Check exit conditions for MTF momentum positions."""
        exits: List[Dict[str, Any]] = []

        for pos in positions:
            if pos.get("strategy") != self.name:
                continue

            ticker = pos["ticker"]
            try:
                df = data.get(ticker)
                if df is None or len(df) < self.atr_period + 1:
                    continue

                today_close = df["close"].iloc[-1]
                entry_price = pos.get("entry_price", today_close)
                entry_date = pd.Timestamp(pos.get("entry_date", df.index[-1]))
                days_held = (df.index[-1] - entry_date).days

                atr_series = calc_atr(df["high"], df["low"], df["close"], self.atr_period)
                current_atr = float(atr_series.iloc[-1])
                if pd.isna(current_atr):
                    continue

                stop_price = pos.get("stop_price", entry_price - self.atr_stop_mult * current_atr)

                # 1. Stop loss
                if today_close <= stop_price:
                    exits.append({
                        "ticker": ticker,
                        "reason": "stop_loss",
                        "exit_price": today_close,
                        "details": f"{ticker} hit stop at ${today_close:.2f} (stop=${stop_price:.2f})",
                    })

                # 2. Trailing stop
                elif days_held >= 3:
                    # Audit H3: trail from highest high since entry, not from today_close
                    entry_ts = pd.Timestamp(entry_date)
                    mask_since_entry = df.index >= entry_ts
                    if mask_since_entry.any():
                        highest = float(df.loc[mask_since_entry, "high"].max())
                        trail_stop = highest - self.trailing_stop_atr_mult * current_atr
                        if today_close <= trail_stop:
                            exits.append({
                                "ticker": ticker,
                                "reason": "trailing_stop",
                                "exit_price": today_close,
                                "details": f"{ticker} trailing stop at ${today_close:.2f}",
                            })

                # 3. Take profit
                elif pos.get("take_profit") and today_close >= pos["take_profit"]:
                    exits.append({
                        "ticker": ticker,
                        "reason": "take_profit",
                        "exit_price": today_close,
                        "details": f"{ticker} take profit at ${today_close:.2f}",
                    })

                # 4. Time exit
                elif days_held >= self.max_hold_days:
                    exits.append({
                        "ticker": ticker,
                        "reason": "time_exit",
                        "exit_price": today_close,
                        "details": f"{ticker} time exit after {days_held} days",
                    })

            except Exception as e:
                self._logger.error(
                    f"{ticker}: exit check error: {e}", exc_info=True
                )
                continue

        return exits
