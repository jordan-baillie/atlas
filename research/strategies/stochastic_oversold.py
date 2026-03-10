"""
Atlas Stochastic Oversold Strategy
====================================
Mean reversion strategy using the Stochastic oscillator. Buys when both
%K and %D are oversold (< 20) in an uptrend (price > SMA-200). Exits on
overbought recovery (%K > 80), bearish %K/%D cross, time stop, or stop-loss.

Published research basis:
  - George Lane, "Lane's Stochastics" (1984)
  - Stochastic measures where the close is relative to the high-low range
  - %K = (Close - Lowest Low(N)) / (Highest High(N) - Lowest Low(N)) * 100
  - %D = SMA(%K, 3) — signal line (smoother, reduces noise)
  - Both below 20 = strong oversold confirmation (dual filter)
  - %K crossing above %D = bullish momentum returning (momentum confirmation)

Logic:
  1. %K < stoch_entry (20) AND %D < stoch_entry (20) → oversold
  2. Price > SMA-200 → long-term uptrend (optional)
  3. Stop: ATR-based below entry
  4. Exit: %K > stoch_exit (80) OR %K crosses below %D OR time/stop

Config Section: strategies.stochastic_oversold

Usage:
    from research.strategies.stochastic_oversold import StochasticOversold
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_position_size

logger = logging.getLogger(__name__)


class StochasticOversold(BaseStrategy):
    """Buy when Stochastic %K and %D are both oversold (<20) in uptrend."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("stochastic_oversold", {})

        # Stochastic parameters
        self.stoch_period = strat_cfg.get("stoch_period", 14)   # %K lookback
        self.stoch_smooth = strat_cfg.get("stoch_smooth", 3)    # %D smoothing period
        self.stoch_entry = strat_cfg.get("stoch_entry", 20)     # Oversold threshold
        self.stoch_exit = strat_cfg.get("stoch_exit", 80)       # Overbought threshold

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
            f"StochasticOversold initialized: period={self.stoch_period}, "
            f"smooth={self.stoch_smooth}, entry_thresh={self.stoch_entry}, "
            f"exit_thresh={self.stoch_exit}, sma200={'ON' if self.sma200_filter else 'OFF'}, "
            f"atr_stop_mult={self.atr_stop_mult}, max_hold={self.max_hold_days}"
        )

    @property
    def name(self) -> str:
        return "stochastic_oversold"

    def _calc_stochastic(
        self, high: pd.Series, low: pd.Series, close: pd.Series
    ) -> Tuple[pd.Series, pd.Series]:
        """Compute Stochastic %K and %D.

        %K = (Close - LowestLow(N)) / (HighestHigh(N) - LowestLow(N)) * 100
        %D = SMA(%K, smooth)

        Returns: (pct_k, pct_d) both in range [0, 100].
        """
        lowest_low = low.rolling(self.stoch_period).min()
        highest_high = high.rolling(self.stoch_period).max()
        denom = highest_high - lowest_low
        # Avoid division by zero
        pct_k = np.where(denom > 0, (close - lowest_low) / denom * 100, 50.0)
        pct_k = pd.Series(pct_k, index=close.index)
        pct_d = pct_k.rolling(self.stoch_smooth).mean()
        return pct_k, pct_d

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Scan all tickers for Stochastic oversold entry signals.

        Signal conditions:
          1. %K < stoch_entry AND %D < stoch_entry (dual oversold confirmation)
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

        # Minimum rows: SMA-200 dominates, but need enough for stochastic
        min_rows = max(
            200 + 5,
            self.stoch_period + self.stoch_smooth + 5,
            self.atr_period + 5,
        )

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

                # ── Stochastic %K and %D ──
                pct_k, pct_d = self._calc_stochastic(high, low, close)
                current_k = float(pct_k.iloc[-1])
                current_d = float(pct_d.iloc[-1])

                if np.isnan(current_k) or np.isnan(current_d):
                    continue

                # Entry: both %K and %D must be below oversold threshold
                if current_k >= self.stoch_entry or current_d >= self.stoch_entry:
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
                # Lower %K + lower %D = stronger oversold = higher confidence
                # Map both to depth below threshold, combine for 0.65..0.95
                k_depth = max(0, self.stoch_entry - current_k)  # 0..20
                d_depth = max(0, self.stoch_entry - current_d)  # 0..20
                avg_depth = (k_depth + d_depth) / 2             # 0..20
                depth_bonus = min(avg_depth / 100.0, 0.20)      # cap at +0.20
                confidence = round(min(0.95, 0.65 + depth_bonus), 3)

                sma200_str = ""
                if self.sma200_filter:
                    sma200_val = float(close.rolling(200).mean().iloc[-1])
                    sma200_str = f", SMA200={sma200_val:.2f}"

                rationale = (
                    f"{ticker}: Stochastic %K={current_k:.1f}, %D={current_d:.1f} "
                    f"(oversold threshold {self.stoch_entry}), "
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
                        "stoch_k": round(current_k, 2),
                        "stoch_d": round(current_d, 2),
                        "atr": round(atr_val, 3),
                        "stoch_period": self.stoch_period,
                    },
                    market_id=self.config.get("market", "sp500"),
                ))

            except Exception as e:
                self._logger.warning(f"{ticker}: signal generation failed: {e}")
                continue

        self._logger.info(
            f"StochasticOversold: {len(signals)} signals from {len(data)} tickers"
        )
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check exit conditions for held Stochastic positions.

        Exit rules (priority order):
          1. Stop hit: current close <= stop_price
          2. Profit target hit (if enabled)
          3. Time exit: held >= max_hold_days
          4. Overbought exit: %K > stoch_exit (80)
          5. Bearish cross: %K crosses below %D (momentum failing)
        """
        exits: List[Dict[str, Any]] = []

        for pos in positions:
            if pos.get("strategy") != self.name:
                continue

            ticker = pos.get("ticker")
            if not ticker or ticker not in data:
                continue

            df = data[ticker]
            min_rows_needed = self.stoch_period + self.stoch_smooth + 2
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

            # 4+5. Stochastic signal exits
            else:
                try:
                    pct_k, pct_d = self._calc_stochastic(df["high"], df["low"], df["close"])
                    current_k = float(pct_k.iloc[-1])
                    current_d = float(pct_d.iloc[-1])
                    prev_k = float(pct_k.iloc[-2])
                    prev_d = float(pct_d.iloc[-2])

                    # 4. Overbought recovery: %K > stoch_exit
                    if not np.isnan(current_k) and current_k > self.stoch_exit:
                        reason = "signal_exit"
                        details = (
                            f"{ticker} overbought exit: %K={current_k:.1f} > {self.stoch_exit}, "
                            f"held {days_held}d"
                        )

                    # 5. Bearish %K/%D cross: %K was above %D, now below
                    elif (
                        not np.isnan(current_k)
                        and not np.isnan(current_d)
                        and not np.isnan(prev_k)
                        and not np.isnan(prev_d)
                        and prev_k >= prev_d  # was above or equal
                        and current_k < current_d  # now crossed below
                    ):
                        reason = "signal_exit"
                        details = (
                            f"{ticker} bearish %K/%D cross: %K={current_k:.1f} < %D={current_d:.1f}, "
                            f"held {days_held}d"
                        )

                except Exception as e:
                    self._logger.warning(f"{ticker}: exit stochastic calculation failed: {e}")

            if reason:
                exits.append({
                    "ticker": ticker,
                    "reason": reason,
                    "exit_price": current_close,
                    "details": details or reason,
                })

        self._logger.debug(
            f"StochasticOversold: {len(exits)} exits from {len(positions)} positions"
        )
        return exits


# Default parameter grid for optimization
PARAM_GRID = {
    "stoch_period": [10, 14, 20],
    "stoch_smooth": [3, 5],
    "stoch_entry": [15, 20, 25],
    "stoch_exit": [75, 80, 85],
    "atr_stop_mult": [1.5, 2.0, 2.5],
    "max_hold_days": [5, 10, 15],
}
