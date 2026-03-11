"""
Atlas Vwap Reversion Strategy
========================================
Price > 2 std below daily VWAP in uptrending stock. Enter long. Exit at VWAP or above. Needs intraday-proxy via daily estimate.

Reference: Institutional VWAP trading, Quantified Strategies
Generated: 2026-03-10T07:18:31.221413+00:00

Config Section: strategies.vwap_reversion
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size

logger = logging.getLogger(__name__)


class VwapReversion(BaseStrategy):
    """Price significantly below daily VWAP proxy — mean reversion long entry."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("vwap_reversion", {})

        # VWAP calculation
        self.vwap_period = strat_cfg.get("vwap_period", 20)         # Rolling VWAP window (days)
        self.std_period = strat_cfg.get("std_period", 20)            # Period for std deviation

        # Entry trigger: how many std devs below VWAP
        self.std_threshold = strat_cfg.get("std_threshold", 1.0)     # std devs below VWAP to trigger

        # Optional momentum filter
        self.rsi_period = strat_cfg.get("rsi_period", 14)
        self.rsi_oversold = strat_cfg.get("rsi_oversold", 50)        # RSI must be below this

        # Trend filter
        self.sma200_filter = strat_cfg.get("sma200_filter", False)
        self.trend_sma = strat_cfg.get("trend_sma", 50)

        # Risk management
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)
        self.max_hold_days = strat_cfg.get("max_hold_days", 10)

        self._logger.info(
            f"VwapReversion initialized: vwap_period={self.vwap_period}, "
            f"std_thresh={self.std_threshold}, rsi<{self.rsi_oversold}, "
            f"sma200={'ON' if self.sma200_filter else 'OFF'}, max_hold={self.max_hold_days}d"
        )

    @property
    def name(self) -> str:
        return "vwap_reversion"

    def _calc_rolling_vwap(self, df: pd.DataFrame) -> pd.Series:
        """Calculate rolling daily VWAP proxy.

        Daily VWAP proxy = sum(typical_price * volume, last N days) / sum(volume, last N days)
        Typical price = (high + low + close) / 3

        This is a daily approximation. True intraday VWAP resets each session.
        """
        typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
        volume = df["volume"]
        tp_vol = typical_price * volume

        rolling_tp_vol = tp_vol.rolling(window=self.vwap_period).sum()
        rolling_vol = volume.rolling(window=self.vwap_period).sum()

        vwap = rolling_tp_vol / rolling_vol.replace(0, np.nan)
        return vwap

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Generate VWAP reversion entry signals.

        Entry criteria:
          1. Stock is in an uptrend (above SMA-200 and trend SMA, if enabled)
          2. Close is significantly below the rolling VWAP (std_threshold standard devs)
          3. RSI is oversold (below rsi_oversold) confirming short-term weakness
          4. ATR-based position sizing with stop below entry
        """
        signals: List[Signal] = []
        held = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0008)

        min_rows = max(
            200 if self.sma200_filter else 0,
            self.trend_sma + 5,
            self.vwap_period + self.std_period + 5,
            self.rsi_period + 5,
            self.atr_period + 5,
        )

        for ticker, df in data.items():
            try:
                if ticker in held:
                    continue
                if not self._can_open_position(existing_positions):
                    break
                if not self._has_sufficient_data(df, min_rows):
                    continue

                close = df["close"]
                high = df["high"]
                low = df["low"]

                current_close = float(close.iloc[-1])

                # SMA-200 uptrend filter
                if self.sma200_filter:
                    sma200 = float(close.rolling(200).mean().iloc[-1])
                    if pd.isna(sma200) or current_close < sma200:
                        continue

                # Calculate rolling VWAP proxy
                vwap = self._calc_rolling_vwap(df)
                current_vwap = float(vwap.iloc[-1])
                if pd.isna(current_vwap) or current_vwap <= 0:
                    continue

                # Calculate std of (close - VWAP) deviations
                deviations = close - vwap
                dev_std = float(deviations.rolling(self.std_period).std().iloc[-1])
                if pd.isna(dev_std) or dev_std <= 0:
                    continue

                # Entry condition: close is std_threshold std devs below VWAP
                current_deviation = current_close - current_vwap
                z_score = current_deviation / dev_std
                if z_score >= -self.std_threshold:
                    continue  # Not far enough below VWAP

                # RSI oversold confirmation
                rsi = calc_rsi(close, period=self.rsi_period)
                current_rsi = float(rsi.iloc[-1])
                if pd.isna(current_rsi) or current_rsi >= self.rsi_oversold:
                    continue

                # ATR-based stop and position sizing
                atr = calc_atr(high, low, close, self.atr_period)
                atr_val = float(atr.iloc[-1])
                if pd.isna(atr_val) or atr_val <= 0:
                    continue

                entry_price = current_close
                stop_price = entry_price - self.atr_stop_mult * atr_val
                # Take profit: at VWAP (mean reversion target)
                take_profit = current_vwap

                if stop_price <= 0 or stop_price >= entry_price:
                    continue
                if take_profit <= entry_price:
                    # VWAP is below current price — shouldn't happen given our entry condition
                    take_profit = entry_price * 1.02  # Fallback: 2% profit target

                try:
                    pos = calc_position_size(
                        equity=equity,
                        risk_pct=risk_pct,
                        entry_price=entry_price,
                        stop_price=stop_price,
                        commission_per_trade=commission_per_trade,
                        commission_pct=commission_pct,
                    )
                except ValueError:
                    continue

                if pos["shares"] <= 0:
                    continue

                # Confidence scoring
                # Deeper below VWAP = stronger reversion signal
                depth_conf = min(0.15, abs(z_score + self.std_threshold) / 2.0 * 0.15)
                # Deeper oversold RSI = stronger confirmation
                rsi_conf = min(0.10, (self.rsi_oversold - current_rsi) / 30.0 * 0.10)
                confidence = round(min(0.95, 0.65 + depth_conf + rsi_conf), 4)

                rationale = (
                    f"{ticker}: VWAP reversion — close={current_close:.2f} is {z_score:.1f}std "
                    f"below VWAP={current_vwap:.2f} (dev={current_deviation:.2f}, std={dev_std:.2f}), "
                    f"RSI={current_rsi:.1f}. Entry={entry_price:.2f}, stop={stop_price:.2f}, "
                    f"target=VWAP@{take_profit:.2f}."
                )

                signals.append(Signal(
                    ticker=ticker,
                    strategy=self.name,
                    direction="long",
                    entry_price=entry_price,
                    stop_price=round(stop_price, 4),
                    take_profit=round(take_profit, 4),
                    position_size=pos["shares"],
                    position_value=pos["position_value"],
                    risk_amount=pos["total_risk"],
                    confidence=confidence,
                    rationale=rationale,
                    features={
                        "vwap": round(current_vwap, 4),
                        "z_score": round(z_score, 2),
                        "deviation": round(current_deviation, 4),
                        "dev_std": round(dev_std, 4),
                        "rsi": round(current_rsi, 2),
                        "atr": round(atr_val, 4),
                        "close": round(current_close, 4),
                    },
                    timestamp=datetime.now(),
                ))

            except Exception as e:
                self._logger.error(f"{ticker}: error in vwap_reversion signal gen: {e}", exc_info=True)
                continue

        self._logger.info(f"{self.name}: {len(signals)} signals from {len(data)} tickers")
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check positions for exit conditions.

        Exit priority:
          1. Stop-loss hit
          2. Price reverts to VWAP or above (take-profit / signal exit)
          3. Time exit
        """
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

            try:
                current_price = float(df["close"].iloc[-1])
                stop_price = pos.get("stop_price", 0)
                take_profit = pos.get("take_profit")

                # 1. Stop-loss
                if stop_price and current_price <= stop_price:
                    exits.append({
                        "ticker": ticker,
                        "reason": "stop_hit",
                        "exit_price": current_price,
                        "details": f"Price {current_price:.2f} <= stop {stop_price:.2f}",
                    })
                    continue

                # 2. Take-profit (price reached VWAP = mean reversion complete)
                if take_profit and current_price >= take_profit:
                    exits.append({
                        "ticker": ticker,
                        "reason": "take_profit",
                        "exit_price": current_price,
                        "details": f"Price {current_price:.2f} >= VWAP target {take_profit:.2f}",
                    })
                    continue

                # 3. Signal exit: price reverts to above rolling VWAP
                vwap = self._calc_rolling_vwap(df)
                current_vwap = float(vwap.iloc[-1])
                if not pd.isna(current_vwap) and current_price >= current_vwap:
                    exits.append({
                        "ticker": ticker,
                        "reason": "signal_exit",
                        "exit_price": current_price,
                        "details": f"Price {current_price:.2f} reverted to/above VWAP {current_vwap:.2f}",
                    })
                    continue

                # 4. Time exit
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

            except Exception as e:
                self._logger.error(f"{ticker}: exit check error: {e}", exc_info=True)

        return exits


# Default parameter grid for optimization
PARAM_GRID = {
    "vwap_period": [10, 20, 30],
    "std_threshold": [1.5, 2.0, 2.5, 3.0],
    "rsi_oversold": [35, 40, 45, 50],
    "atr_stop_mult": [1.5, 2.0, 2.5],
    "max_hold_days": [5, 10, 15],
}
