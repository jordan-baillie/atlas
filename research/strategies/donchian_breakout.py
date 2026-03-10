"""
Atlas Donchian Breakout Strategy
==================================
Classic trend-following strategy based on the Turtle Trading system. Buys when
the close breaks above the highest high of the last N days (20). Exits when the
close falls below the lowest low of the last M days (10), trailing ATR stop, or
time stop.

Published research basis:
  - Richard Donchian, "Commodity Channel Methods" (1960s)
  - Dennis & Eckhardt: The Turtle Traders experiment (1983)
  - Covel "Complete TurtleTrader" (2007): Breakout systems outperform buy-and-hold
    in trend environments with proper position sizing
  - Faber (2007): Simple moving average + breakout systems beat S&P 500 on
    risk-adjusted basis

Core logic:
  1. Entry: Close > highest high of last entry_period (20) days — new high breakout
  2. SMA-200 filter (optional): only take breakouts in confirmed uptrends
  3. Stop: ATR-based trailing stop (atr_stop_mult * ATR below entry)
  4. Exit: Close < lowest low of last exit_period (10) days (channel breakdown)
         OR trailing ATR stop
         OR time-based exit after max_hold_days

Key properties:
  - Trend following: profits from sustained price moves
  - ATR position sizing: risk-adjusted entries
  - Clear mechanical rules: low discretion, low overfit risk

Config Section: strategies.donchian_breakout

Usage:
    from research.strategies.donchian_breakout import DonchianBreakout
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_position_size

logger = logging.getLogger(__name__)


class DonchianBreakout(BaseStrategy):
    """Buy on N-day high breakout (Donchian channel), exit on M-day low breakdown."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("donchian_breakout", {})

        # Channel parameters
        self.entry_period = strat_cfg.get("entry_period", 20)   # Breakout lookback (days)
        self.exit_period = strat_cfg.get("exit_period", 10)     # Breakdown lookback (days)

        # Trend filter
        self.sma200_filter = strat_cfg.get("sma200_filter", True)

        # Risk management
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 3.0)  # Turtle used 2x ATR

        # Exit parameters
        self.max_hold_days = strat_cfg.get("max_hold_days", 20)

        # Profit target (0 = disabled — Turtles use channel exit, not fixed target)
        self.profit_target_atr_mult = strat_cfg.get("profit_target_atr_mult", 0.0)

        self._logger.info(
            f"DonchianBreakout initialized: entry_period={self.entry_period}, "
            f"exit_period={self.exit_period}, sma200={'ON' if self.sma200_filter else 'OFF'}, "
            f"atr_stop_mult={self.atr_stop_mult}, max_hold={self.max_hold_days}"
        )

    @property
    def name(self) -> str:
        return "donchian_breakout"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Scan all tickers for Donchian channel breakout entry signals.

        Signal conditions:
          1. Close > highest high of last entry_period days (breakout)
          2. Yesterday was NOT already above the channel (fresh breakout only)
          3. Price > SMA-200 (uptrend filter, if enabled)
          4. Position limit not exceeded
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

        # Minimum rows: SMA-200 + entry_period + buffer
        min_rows = max(200 + self.entry_period + 5, self.atr_period + 10)

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

                # ── Donchian upper channel: highest high over entry_period ──
                # Use shift(1) so today's bar is not included in the channel
                # (avoids looking ahead: breakout is vs. yesterday's channel)
                channel_high = high.shift(1).rolling(self.entry_period).max()

                current_close = float(close.iloc[-1])
                channel_high_val = float(channel_high.iloc[-1])

                if np.isnan(channel_high_val):
                    continue

                # Entry: today's close > channel high (breakout)
                if current_close <= channel_high_val:
                    continue

                # Fresh breakout check: previous close should NOT have been above
                # prior channel to avoid chasing extended moves
                prev_close = float(close.iloc[-2])
                prev_channel_high = float(channel_high.iloc[-2])
                if not np.isnan(prev_channel_high) and prev_close > prev_channel_high:
                    # Already in a breakout — skip (not a fresh entry)
                    continue

                # ── SMA-200 trend filter ──
                if self.sma200_filter:
                    sma200 = close.rolling(200).mean()
                    if close.iloc[-1] <= sma200.iloc[-1]:
                        continue

                # ── ATR and position sizing ──
                entry_price = current_close
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
                # Breakout strength: how far above channel did close land?
                breakout_pct = (current_close - channel_high_val) / channel_high_val
                breakout_bonus = min(breakout_pct * 5, 0.20)  # +0.20 max for 4% breakout
                confidence = round(min(0.95, 0.65 + breakout_bonus), 3)

                sma200_str = ""
                if self.sma200_filter:
                    sma200_val = float(close.rolling(200).mean().iloc[-1])
                    sma200_str = f", SMA200={sma200_val:.2f}"

                rationale = (
                    f"{ticker}: Donchian breakout — close={current_close:.2f} > "
                    f"{self.entry_period}d high={channel_high_val:.2f} "
                    f"({breakout_pct*100:.1f}% above), "
                    f"ATR={atr_val:.2f}, stop={stop_price:.2f}{sma200_str}"
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
                        "channel_high": round(channel_high_val, 2),
                        "breakout_pct": round(breakout_pct * 100, 2),
                        "atr": round(atr_val, 3),
                        "entry_period": self.entry_period,
                    },
                    market_id=self.config.get("market", "sp500"),
                ))

            except Exception as e:
                self._logger.warning(f"{ticker}: signal generation failed: {e}")
                continue

        self._logger.info(
            f"DonchianBreakout: {len(signals)} signals from {len(data)} tickers"
        )
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check exit conditions for held Donchian breakout positions.

        Exit rules (priority order):
          1. ATR trailing stop hit: close <= stop_price
          2. Profit target hit (if enabled)
          3. Time exit: held >= max_hold_days
          4. Channel breakdown: close < lowest low of last exit_period days
        """
        exits: List[Dict[str, Any]] = []

        for pos in positions:
            if pos.get("strategy") != self.name:
                continue

            ticker = pos.get("ticker")
            if not ticker or ticker not in data:
                continue

            df = data[ticker]
            min_rows_needed = max(self.exit_period + 2, self.atr_period + 2)
            if not self._has_sufficient_data(df, min_rows_needed):
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

            # 1. ATR trailing stop hit
            if stop_price and current_close <= stop_price:
                reason = "stop_hit"
                details = (
                    f"{ticker} ATR stop hit: close {current_close:.2f} <= stop {stop_price:.2f}, "
                    f"held {days_held}d"
                )

            # 2. Profit target hit (if enabled)
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

            # 4. Donchian channel breakdown: close < lowest low of exit_period
            else:
                try:
                    low = df["low"]
                    # Use shift(1) to exclude today's bar from channel calculation
                    channel_low = low.shift(1).rolling(self.exit_period).min()
                    channel_low_val = float(channel_low.iloc[-1])

                    if not np.isnan(channel_low_val) and current_close < channel_low_val:
                        reason = "signal_exit"
                        details = (
                            f"{ticker} channel breakdown: close {current_close:.2f} < "
                            f"{self.exit_period}d low={channel_low_val:.2f}, held {days_held}d"
                        )
                except Exception as e:
                    self._logger.warning(f"{ticker}: exit channel calculation failed: {e}")

            if reason:
                exits.append({
                    "ticker": ticker,
                    "reason": reason,
                    "exit_price": current_close,
                    "details": details or reason,
                })

        self._logger.debug(
            f"DonchianBreakout: {len(exits)} exits from {len(positions)} positions"
        )
        return exits


# Default parameter grid for optimization
PARAM_GRID = {
    "entry_period": [15, 20, 25, 30],
    "exit_period": [5, 10, 15],
    "atr_stop_mult": [2.0, 2.5, 3.0, 3.5],
    "max_hold_days": [10, 20, 30],
    "sma200_filter": [True, False],
}
