"""
Atlas Rsi Divergence Strategy
========================================
Price makes new low but RSI makes higher low (bullish divergence). Enter long. Exit on RSI > 60 or time.

Reference: Andrew Cardwell RSI divergence methodology
Generated: 2026-03-10T07:18:31.210300+00:00

Config Section: strategies.rsi_divergence
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size

logger = logging.getLogger(__name__)


class RsiDivergence(BaseStrategy):
    """Price makes new low but RSI makes higher low (bullish divergence)."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("rsi_divergence", {})

        # RSI parameters
        self.rsi_period = strat_cfg.get("rsi_period", 14)
        self.rsi_oversold_max = strat_cfg.get("rsi_oversold_max", 45)   # RSI must be below this (oversold)
        self.rsi_exit_level = strat_cfg.get("rsi_exit_level", 60)        # Exit when RSI recovers here

        # Divergence detection
        self.divergence_lookback = strat_cfg.get("divergence_lookback", 20)  # Bars to look back for prior low
        self.min_rsi_improvement = strat_cfg.get("min_rsi_improvement", 2.0) # Minimum RSI delta (avoid noise)

        # Trend filter
        self.sma200_filter = strat_cfg.get("sma200_filter", True)

        # Risk management
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)
        self.profit_target_atr_mult = strat_cfg.get("profit_target_atr_mult", 2.0)
        self.max_hold_days = strat_cfg.get("max_hold_days", 15)

        self._logger.info(
            f"RsiDivergence initialized: rsi_period={self.rsi_period}, "
            f"lookback={self.divergence_lookback}, oversold<{self.rsi_oversold_max}, "
            f"exit@rsi>{self.rsi_exit_level}, sma200={'ON' if self.sma200_filter else 'OFF'}"
        )

    @property
    def name(self) -> str:
        return "rsi_divergence"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Generate RSI divergence entry signals.

        Entry criteria:
          1. Current close is at or below the previous N-period low (new low in price)
          2. Current RSI is HIGHER than the RSI at the previous price low (divergence)
          3. RSI improvement is at least min_rsi_improvement points (avoids noise)
          4. Current RSI is still below rsi_oversold_max (oversold territory)
          5. Optional SMA-200 uptrend filter
        """
        signals: List[Signal] = []
        held = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0008)

        min_rows = max(
            200 if self.sma200_filter else 0,
            self.divergence_lookback + self.rsi_period + 10,
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

                # Compute RSI
                rsi = calc_rsi(close, period=self.rsi_period)
                current_rsi = float(rsi.iloc[-1])
                if pd.isna(current_rsi):
                    continue

                # RSI must be in oversold territory
                if current_rsi >= self.rsi_oversold_max:
                    continue

                # Look back divergence_lookback bars (excluding current bar)
                lookback = self.divergence_lookback
                if len(close) < lookback + 2:
                    continue

                close_window = close.iloc[-(lookback + 1):-1]
                rsi_window = rsi.iloc[-(lookback + 1):-1]

                if close_window.empty or rsi_window.empty:
                    continue

                # Find the previous price low in the lookback window
                prev_low_idx = close_window.idxmin()
                prev_low_close = float(close_window[prev_low_idx])
                prev_low_rsi = float(rsi_window[prev_low_idx])

                if pd.isna(prev_low_rsi):
                    continue

                # Condition 1: Current close at or below previous low (price making new low)
                if current_close >= prev_low_close * 1.005:
                    continue  # Price is NOT at a new low — no divergence

                # Condition 2: RSI is HIGHER than at the previous price low (diverging)
                rsi_delta = current_rsi - prev_low_rsi
                if rsi_delta < self.min_rsi_improvement:
                    continue  # RSI not recovering — no bullish divergence

                # ATR-based stop and position sizing
                atr = calc_atr(high, low, close, self.atr_period)
                atr_val = float(atr.iloc[-1])
                if pd.isna(atr_val) or atr_val <= 0:
                    continue

                entry_price = current_close
                # Stop below the recent low (lowest low in lookback window)
                recent_low = float(low.iloc[-lookback:].min())
                atr_stop = entry_price - self.atr_stop_mult * atr_val
                stop_price = min(atr_stop, recent_low * 0.99)  # Just below recent low

                if stop_price <= 0 or stop_price >= entry_price:
                    continue

                take_profit = None
                if self.profit_target_atr_mult > 0:
                    take_profit = entry_price + self.profit_target_atr_mult * atr_val

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
                # More RSI improvement = stronger divergence
                rsi_conf = min(0.15, rsi_delta / 20.0 * 0.15)
                # Deeper oversold = better setup
                oversold_conf = min(0.10, (self.rsi_oversold_max - current_rsi) / 30.0 * 0.10)
                confidence = round(min(0.95, 0.65 + rsi_conf + oversold_conf), 4)

                rationale = (
                    f"{ticker}: Bullish RSI divergence — price low={current_close:.2f} (< prev={prev_low_close:.2f}), "
                    f"RSI={current_rsi:.1f} (> prev_rsi={prev_low_rsi:.1f}, delta=+{rsi_delta:.1f}). "
                    f"Entry={entry_price:.2f}, stop={stop_price:.2f}"
                    + (f", target={take_profit:.2f}." if take_profit else ".")
                )

                signals.append(Signal(
                    ticker=ticker,
                    strategy=self.name,
                    direction="long",
                    entry_price=entry_price,
                    stop_price=round(stop_price, 4),
                    take_profit=round(take_profit, 4) if take_profit else None,
                    position_size=pos["shares"],
                    position_value=pos["position_value"],
                    risk_amount=pos["total_risk"],
                    confidence=confidence,
                    rationale=rationale,
                    features={
                        "rsi": round(current_rsi, 2),
                        "prev_low_rsi": round(prev_low_rsi, 2),
                        "rsi_delta": round(rsi_delta, 2),
                        "price_new_low": round(current_close, 4),
                        "prev_low_price": round(prev_low_close, 4),
                        "atr": round(atr_val, 4),
                        "close": round(current_close, 4),
                    },
                    timestamp=datetime.now(),
                ))

            except Exception as e:
                self._logger.error(f"{ticker}: error in rsi_divergence signal gen: {e}", exc_info=True)
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
          2. Take-profit hit
          3. Signal exit: RSI recovers above rsi_exit_level (momentum confirmed)
          4. Time exit
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

                # 2. Take-profit
                if take_profit and current_price >= take_profit:
                    exits.append({
                        "ticker": ticker,
                        "reason": "take_profit",
                        "exit_price": current_price,
                        "details": f"Price {current_price:.2f} >= target {take_profit:.2f}",
                    })
                    continue

                # 3. Signal exit: RSI recovers (momentum confirmed — we got our reversal)
                close = df["close"]
                rsi = calc_rsi(close, period=self.rsi_period)
                current_rsi = float(rsi.iloc[-1])
                if not pd.isna(current_rsi) and current_rsi >= self.rsi_exit_level:
                    exits.append({
                        "ticker": ticker,
                        "reason": "signal_exit",
                        "exit_price": current_price,
                        "details": f"RSI recovered to {current_rsi:.1f} >= exit level {self.rsi_exit_level}",
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
    "rsi_period": [10, 14],
    "divergence_lookback": [10, 15, 20],
    "rsi_oversold_max": [35, 40, 45, 50],
    "rsi_exit_level": [55, 60, 65],
    "atr_stop_mult": [1.5, 2.0, 2.5],
    "profit_target_atr_mult": [0.0, 1.5, 2.0],
    "max_hold_days": [10, 15, 20],
}
