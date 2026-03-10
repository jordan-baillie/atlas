"""
Atlas Williams %R Strategy
===========================
Mean reversion strategy using Williams %R oscillator. Buys oversold stocks
(Williams %R < -80) in an uptrend (price > SMA-200). Exits when the oscillator
recovers to overbought territory (WR > -20), time stop, or stop-loss.

Published research basis:
  - Larry Williams, "How I Made One Million Dollars Trading Commodities" (1979)
  - %R is the inverse of the Stochastic oscillator — negative scale (-100 to 0)
  - Values near -100 indicate extreme oversold (closed near period low)
  - Values near 0 indicate extreme overbought (closed near period high)
  - Williams %R = (Highest High(N) - Close) / (Highest High(N) - Lowest Low(N)) * -100

Logic:
  1. Williams %R < wr_entry (-80) → strongly oversold
  2. Price > SMA-200 → long-term uptrend (avoids buying falling knives)
  3. Stop: ATR-based (atr_stop_mult * ATR below entry)
  4. Exit when WR > wr_exit (-20) (recovered), time stop, or stop hit

Config Section: strategies.williams_percent_r

Usage:
    from research.strategies.williams_percent_r import WilliamsPercentR
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_position_size

logger = logging.getLogger(__name__)


class WilliamsPercentR(BaseStrategy):
    """Buy oversold stocks (Williams %R < -80) in uptrend, exit on recovery (WR > -20)."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("williams_percent_r", {})

        # Williams %R parameters
        self.wr_period = strat_cfg.get("wr_period", 14)
        self.wr_entry = strat_cfg.get("wr_entry", -80)    # Buy when WR < this (oversold)
        self.wr_exit = strat_cfg.get("wr_exit", -20)      # Exit when WR > this (overbought)

        # Trend filter
        self.sma200_filter = strat_cfg.get("sma200_filter", True)

        # Risk management
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)

        # Exit parameters
        self.max_hold_days = strat_cfg.get("max_hold_days", 10)

        # Profit target (0 = disabled)
        self.profit_target_atr_mult = strat_cfg.get("profit_target_atr_mult", 0.0)

        self._logger.info(
            f"WilliamsPercentR initialized: wr_period={self.wr_period}, "
            f"wr_entry={self.wr_entry}, wr_exit={self.wr_exit}, "
            f"sma200={'ON' if self.sma200_filter else 'OFF'}, "
            f"atr_stop_mult={self.atr_stop_mult}, max_hold={self.max_hold_days}"
        )

    @property
    def name(self) -> str:
        return "williams_percent_r"

    def _calc_williams_r(self, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
        """Compute Williams %R.

        WR = (Highest High(N) - Close) / (Highest High(N) - Lowest Low(N)) * -100

        Returns values in range [-100, 0].
          -100 = closed at period low (extreme oversold)
            0  = closed at period high (extreme overbought)
        """
        highest_high = high.rolling(self.wr_period).max()
        lowest_low = low.rolling(self.wr_period).min()
        denom = highest_high - lowest_low
        # Avoid division by zero (flat market)
        wr = np.where(denom > 0, (highest_high - close) / denom * -100, -50.0)
        return pd.Series(wr, index=close.index)

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Scan all tickers for Williams %R oversold entry signals.

        Signal conditions:
          1. Williams %R < wr_entry (deeply oversold)
          2. Price > SMA-200 (uptrend filter, if enabled)
          3. Position limit not exceeded
        """
        signals: List[Signal] = []
        held_tickers = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.01)
        commission_per_trade = self.fees_config.get("commission_per_trade", 0.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0)
        min_position_value = self.fees_config.get("min_position_value", 0.0)
        max_position_value = self.config.get("trading", {}).get(
            "live_safety", {}
        ).get("max_order_value", 0.0)

        # Minimum rows needed
        min_rows = max(200 + 5, self.wr_period + 5, self.atr_period + 5)

        for ticker, df in data.items():
            try:
                if ticker in held_tickers:
                    continue
                if not self._can_open_position(existing_positions):
                    self._logger.debug("Max positions reached, stopping scan")
                    break
                if not self._has_sufficient_data(df, min_rows):
                    continue

                close = df["close"]
                high = df["high"]
                low = df["low"]

                # ── Williams %R ──
                wr = self._calc_williams_r(high, low, close)
                current_wr = float(wr.iloc[-1])

                if np.isnan(current_wr):
                    continue

                # Entry condition: oversold (WR < entry threshold, e.g. -80)
                if current_wr >= self.wr_entry:
                    continue

                # ── SMA-200 trend filter ──
                if self.sma200_filter:
                    sma200 = close.rolling(200).mean()
                    if close.iloc[-1] <= sma200.iloc[-1]:
                        continue

                # ── ATR and position sizing ──
                entry_price = float(close.iloc[-1])
                atr = calc_atr(high, low, close, self.atr_period)
                atr_val = float(atr.iloc[-1])

                if atr_val <= 0 or np.isnan(atr_val):
                    continue

                stop_price = entry_price - self.atr_stop_mult * atr_val
                if stop_price <= 0 or stop_price >= entry_price:
                    continue

                take_profit = None
                if self.profit_target_atr_mult > 0:
                    take_profit = entry_price + self.profit_target_atr_mult * atr_val

                pos_result = calc_position_size(
                    entry_price=entry_price,
                    stop_price=stop_price,
                    equity=equity,
                    risk_pct=risk_pct,
                    commission_per_trade=commission_per_trade,
                    commission_pct=commission_pct,
                    min_position_value=min_position_value,
                    max_position_value=max_position_value,
                )
                shares = pos_result["shares"]
                if shares <= 0:
                    continue

                # ── Confidence score ──
                # Deeper oversold (more negative) = higher confidence
                # WR range: wr_entry (-80) to -100 → depth 0..20
                depth = abs(current_wr - self.wr_entry)   # e.g. 0..20
                depth_bonus = min(depth / 100.0, 0.20)    # cap at +0.20
                confidence = round(min(0.95, 0.65 + depth_bonus), 3)

                sma200_str = ""
                if self.sma200_filter:
                    sma200_val = float(close.rolling(200).mean().iloc[-1])
                    sma200_str = f", SMA200={sma200_val:.2f}"

                rationale = (
                    f"{ticker}: Williams %R={current_wr:.1f} (threshold {self.wr_entry}), "
                    f"entry={entry_price:.2f}, stop={stop_price:.2f}, "
                    f"ATR={atr_val:.2f}{sma200_str}"
                )

                signals.append(Signal(
                    ticker=ticker,
                    strategy=self.name,
                    direction="long",
                    entry_price=entry_price,
                    stop_price=round(stop_price, 2),
                    take_profit=round(take_profit, 2) if take_profit else None,
                    position_size=shares,
                    position_value=round(shares * entry_price, 2),
                    risk_amount=round(pos_result["total_risk"], 2),
                    confidence=confidence,
                    rationale=rationale,
                    features={
                        "williams_r": round(current_wr, 2),
                        "atr": round(atr_val, 3),
                        "wr_period": self.wr_period,
                    },
                    market_id=self.config.get("market", "sp500"),
                ))

            except Exception as e:
                self._logger.warning(f"{ticker}: signal generation failed: {e}")
                continue

        self._logger.info(
            f"WilliamsPercentR: {len(signals)} signals from {len(data)} tickers"
        )
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check exit conditions for held Williams %R positions.

        Exit rules (priority order):
          1. Stop hit: current close <= stop_price
          2. Profit target hit (if enabled)
          3. Time exit: held >= max_hold_days
          4. Signal exit: Williams %R recovered above wr_exit (-20)
        """
        exits: List[Dict[str, Any]] = []

        for pos in positions:
            if pos.get("strategy") != self.name:
                continue

            ticker = pos.get("ticker")
            if not ticker or ticker not in data:
                continue

            df = data[ticker]
            if not self._has_sufficient_data(df, self.wr_period + 2):
                continue

            current_close = float(df["close"].iloc[-1])
            stop_price = pos.get("stop_price", 0.0)
            take_profit = pos.get("take_profit")
            entry_date = pos.get("entry_date")

            # Days held
            days_held = 0
            if entry_date:
                if isinstance(entry_date, str):
                    entry_date = datetime.fromisoformat(entry_date)
                days_held = (df.index[-1] - pd.Timestamp(entry_date)).days

            reason = None
            details = None

            # 1. Stop loss
            if current_close <= stop_price:
                reason = "stop_hit"
                details = (
                    f"{ticker} stop hit: close {current_close:.2f} <= stop {stop_price:.2f}, "
                    f"held {days_held}d"
                )

            # 2. Profit target
            elif take_profit is not None and current_close >= take_profit:
                reason = "take_profit"
                details = (
                    f"{ticker} profit target: close {current_close:.2f} >= target {take_profit:.2f}, "
                    f"held {days_held}d"
                )

            # 3. Time exit
            elif days_held >= self.max_hold_days:
                reason = "time_exit"
                details = f"{ticker} time exit: held {days_held}d >= max {self.max_hold_days}d"

            # 4. Williams %R signal exit (recovered to overbought)
            else:
                try:
                    wr = self._calc_williams_r(df["high"], df["low"], df["close"])
                    current_wr = float(wr.iloc[-1])
                    if not np.isnan(current_wr) and current_wr > self.wr_exit:
                        reason = "signal_exit"
                        details = (
                            f"{ticker} WR exit: %R={current_wr:.1f} > exit threshold {self.wr_exit}, "
                            f"held {days_held}d"
                        )
                except Exception as e:
                    self._logger.warning(f"{ticker}: exit WR calculation failed: {e}")

            if reason:
                exits.append({
                    "ticker": ticker,
                    "reason": reason,
                    "exit_price": current_close,
                    "details": details or reason,
                })

        self._logger.debug(
            f"WilliamsPercentR: {len(exits)} exits from {len(positions)} positions"
        )
        return exits


# Default parameter grid for optimization
PARAM_GRID = {
    "wr_period": [10, 14, 20],
    "wr_entry": [-75, -80, -85],
    "wr_exit": [-15, -20, -25],
    "atr_stop_mult": [1.5, 2.0, 2.5],
    "max_hold_days": [5, 10, 15],
}
