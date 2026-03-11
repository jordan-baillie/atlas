"""
Atlas Trend Following Strategy (Phase 7A)
====================================
Generates long signals when price pulls back within an established uptrend.

Entry Conditions:
    - Fast MA (default 10) > Slow MA (default 30) — confirms uptrend regime
    - Price has pulled back pullback_pct (default 2%) from recent high within trend

Stop Loss:
    - Entry price - atr_stop_mult * ATR(atr_period)

Exit Conditions:
    - Fast MA crosses below Slow MA (trend reversal)
    - Trailing stop at trailing_stop_atr_mult * ATR below highest close since entry
    - Time-based exit after max_hold_days

Config Section: strategies.trend_following

Usage:
    from strategies.trend_following import TrendFollowing
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_position_size, calc_volume_ratio

logger = logging.getLogger(__name__)


class TrendFollowing(BaseStrategy):
    """Trend following strategy: buy pullbacks within established uptrends."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("trend_following", {})
        self.fast_ma_period = strat_cfg.get("fast_ma", 10)
        self.slow_ma_period = strat_cfg.get("slow_ma", 30)
        self.pullback_pct = strat_cfg.get("pullback_pct", 0.02)
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)
        self.trailing_stop_atr_mult = strat_cfg.get("trailing_stop_atr_mult", 3.0)
        self.max_hold_days = strat_cfg.get("max_hold_days", 7)
        # Phase 7A: Volume confirmation parameters
        vol_cfg = strat_cfg.get("volume", {})
        self.vol_lookback = vol_cfg.get("lookback", 20)
        self.vol_min_ratio = vol_cfg.get("min_ratio", 0.5)
        self.vol_boost_threshold = vol_cfg.get("boost_threshold", 1.5)
        self.vol_boost_amount = vol_cfg.get("boost_amount", 0.1)
        self.vol_penalty_amount = vol_cfg.get("penalty_amount", 0.15)
        self._logger.info(
            f"TrendFollowing initialized: fast_ma={self.fast_ma_period}, "
            f"slow_ma={self.slow_ma_period}, pullback={self.pullback_pct*100}%, "
            f"vol_boost={self.vol_boost_threshold}x, vol_min={self.vol_min_ratio}x"
        )

    @property
    def name(self) -> str:
        return "trend_following"

    def precompute(self, data: Dict[str, pd.DataFrame]) -> None:
        """Pre-compute all indicators as DataFrame columns (called once before walk-forward)."""
        for ticker, df in data.items():
            close = df["close"]
            high = df["high"]
            low = df["low"]
            volume = df["volume"]
            df["_tf_fast_ma"] = close.rolling(window=self.fast_ma_period).mean()
            df["_tf_slow_ma"] = close.rolling(window=self.slow_ma_period).mean()
            df["_tf_ma_diff"] = df["_tf_fast_ma"] - df["_tf_slow_ma"]
            df["_tf_atr"] = calc_atr(high, low, close, period=self.atr_period)
            df["_tf_vol_ratio"] = calc_volume_ratio(volume, lookback=self.vol_lookback)
        self._precomputed = True

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Scan all tickers for trend-following pullback entry signals.

        A signal is generated when:
            1. Fast MA > Slow MA (uptrend confirmed)
            2. Price has pulled back pullback_pct from the recent high
               within the current uptrend
            3. Risk limits allow a new position
            4. Ticker is not already held
        """
        signals: List[Signal] = []
        held_tickers = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0008)
        min_position_value = self.fees_config.get("min_position_value", 0.0)
        max_position_value = self.config.get("trading", {}).get("live_safety", {}).get("max_order_value", 0.0)

        # Minimum rows needed: slow_ma + atr_period + buffer
        min_rows = self.slow_ma_period + self.atr_period + 10

        for ticker, df in data.items():
            try:
                if ticker in held_tickers:
                    continue

                if not self._can_open_position(existing_positions):
                    self._logger.debug("Max positions reached, skipping remaining tickers")
                    break

                if not self._has_sufficient_data(df, min_rows):
                    self._logger.debug(f"{ticker}: insufficient data ({len(df)} rows, need {min_rows})")
                    continue

                close = df["close"]
                high = df["high"]
                low = df["low"]
                volume = df["volume"]

                # Use pre-computed moving averages
                fast_ma = df["_tf_fast_ma"]
                slow_ma = df["_tf_slow_ma"]

                current_fast = fast_ma.iloc[-1]
                current_slow = slow_ma.iloc[-1]

                if pd.isna(current_fast) or pd.isna(current_slow):
                    continue

                # Condition 1: Uptrend regime — fast MA > slow MA
                is_uptrend = current_fast > current_slow
                if not is_uptrend:
                    continue

                # Find the recent high within the uptrend
                # Look back to find where fast_ma first crossed above slow_ma
                ma_diff = df["_tf_ma_diff"]
                # Find how long the uptrend has been active (max lookback_days bars)
                lookback_limit = min(len(ma_diff), 60)  # cap at 60 bars
                trend_bars = 0
                for i in range(1, lookback_limit):
                    if ma_diff.iloc[-i] > 0:
                        trend_bars = i
                    else:
                        break

                if trend_bars < 2:
                    # Trend just started, wait for it to establish
                    continue

                # Recent high within the uptrend
                recent_high = close.iloc[-trend_bars:].max()
                today_close = close.iloc[-1]

                # Condition 2: Price has pulled back pullback_pct from recent high
                pullback_from_high = (recent_high - today_close) / recent_high
                is_pullback = pullback_from_high >= self.pullback_pct

                if not is_pullback:
                    continue

                # Ensure price is still above slow MA (not a breakdown)
                if today_close < current_slow:
                    continue

                # Phase 7A: Volume confirmation (pre-computed)
                vol_ratio = df["_tf_vol_ratio"]
                current_vol_ratio = vol_ratio.iloc[-1]

                if pd.isna(current_vol_ratio):
                    current_vol_ratio = 1.0  # Neutral if no data

                # Phase 7A: Volume noted for confidence adjustment (no hard filter)

                # Use pre-computed ATR
                atr = df["_tf_atr"]
                current_atr = atr.iloc[-1]

                if pd.isna(current_atr) or current_atr <= 0:
                    self._logger.debug(f"{ticker}: invalid ATR ({current_atr})")
                    continue

                entry_price = today_close

                # Stop loss: entry - atr_stop_mult * ATR
                stop_price = entry_price - (self.atr_stop_mult * current_atr)
                if stop_price <= 0:
                    self._logger.debug(f"{ticker}: stop price <= 0, skipping")
                    continue

                # Position sizing
                try:
                    pos = calc_position_size(
                        equity=equity,
                        risk_pct=risk_pct,
                        entry_price=entry_price,
                        stop_price=stop_price,
                        commission_per_trade=commission_per_trade,
                        commission_pct=commission_pct,
                        min_position_value=min_position_value,
                        max_position_value=max_position_value,
                    )
                except ValueError as e:
                    self._logger.debug(f"{ticker}: position sizing error: {e}")
                    continue

                if pos["shares"] <= 0:
                    self._logger.debug(f"{ticker}: position size is 0, skipping")
                    continue

                # Confidence: based on MA spread strength and pullback depth
                ma_spread = (current_fast - current_slow) / current_slow
                pullback_depth = pullback_from_high / self.pullback_pct  # 1.0 = minimum

                confidence = min(
                    1.0,
                    0.5 * min(ma_spread * 50, 1.0)
                    + 0.3 * min(pullback_depth / 3.0, 1.0)
                    + 0.2 * min(trend_bars / 20.0, 1.0),
                )
                confidence = max(0.1, confidence)

                # Phase 7A: Volume information (recorded in features, no confidence change)
                if current_vol_ratio >= self.vol_boost_threshold:
                    vol_note = f"Volume {current_vol_ratio:.1f}x avg (HIGH). "
                elif current_vol_ratio < 0.6:
                    vol_note = f"Volume {current_vol_ratio:.1f}x avg (LOW). "
                else:
                    vol_note = f"Volume {current_vol_ratio:.1f}x avg. "

                # Rationale
                rationale = (
                    f"{ticker} in uptrend (fast MA ${current_fast:.2f} > slow MA "
                    f"${current_slow:.2f}, spread {ma_spread*100:.1f}%). "
                    f"Price pulled back {pullback_from_high*100:.1f}% from recent "
                    f"high of ${recent_high:.2f} to ${today_close:.2f}. "
                    f"Trend active for {trend_bars} bars. "
                    f"{vol_note}"
                    f"ATR={current_atr:.2f}, stop at ${stop_price:.2f}."
                )

                signal = Signal(
                    ticker=ticker,
                    strategy=self.name,
                    direction="long",
                    entry_price=entry_price,
                    stop_price=round(stop_price, 4),
                    take_profit=None,  # trailing stop only
                    position_size=pos["shares"],
                    position_value=pos["position_value"],
                    risk_amount=pos["total_risk"],
                    confidence=round(confidence, 4),
                    rationale=rationale,
                    features={
                        "fast_ma": round(current_fast, 4),
                        "slow_ma": round(current_slow, 4),
                        "ma_spread_pct": round(ma_spread * 100, 2),
                        "pullback_pct": round(pullback_from_high * 100, 2),
                        "recent_high": round(recent_high, 4),
                        "trend_bars": trend_bars,
                        "atr": round(current_atr, 4),
                        "close": round(today_close, 4),
                        "volume_ratio": round(current_vol_ratio, 2),
                    },
                    timestamp=datetime.now(),
                )
                signals.append(signal)
                self._logger.info(f"SIGNAL: {signal}")

            except Exception as e:
                self._logger.error(
                    f"{ticker}: unexpected error in signal generation: {e}",
                    exc_info=True,
                )
                continue

        self._logger.info(f"TrendFollowing generated {len(signals)} signals")
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check open trend-following positions for exit conditions.

        Exit conditions:
            1. Hard stop: price drops below original stop_price
            2. Trend reversal: fast MA crosses below slow MA
            3. Trailing stop: price drops below highest_since_entry - trailing_atr_mult * ATR
            4. Time exit: position held longer than max_hold_days
        """
        exits: List[Dict[str, Any]] = []

        for pos in positions:
            if pos.get("strategy") != self.name:
                continue

            ticker = pos["ticker"]
            df = data.get(ticker)

            if df is None or df.empty:
                self._logger.warning(f"{ticker}: no data for exit check")
                continue

            try:
                close = df["close"]
                high = df["high"]
                low = df["low"]
                today_close = close.iloc[-1]
                today_date = df.index[-1]

                entry_date = pd.Timestamp(pos["entry_date"])
                entry_price = pos["entry_price"]
                stop_price = pos.get("stop_price", 0)

                # Days held
                days_held = (today_date - entry_date).days

                # Use pre-computed MAs
                fast_ma = df["_tf_fast_ma"]
                slow_ma = df["_tf_slow_ma"]
                current_fast = fast_ma.iloc[-1]
                current_slow = slow_ma.iloc[-1]

                # Use pre-computed ATR
                atr = df["_tf_atr"]
                current_atr = atr.iloc[-1]
                if pd.isna(current_atr):
                    current_atr = abs(entry_price - stop_price) / self.atr_stop_mult

                # Highest close since entry
                mask = df.index >= entry_date
                if mask.any():
                    highest_since_entry = close[mask].max()
                else:
                    highest_since_entry = entry_price

                # Trailing stop level
                trailing_stop = highest_since_entry - (
                    self.trailing_stop_atr_mult * current_atr
                )

                # Check exit conditions (priority order)
                # 1. Hard stop hit
                if today_close <= stop_price:
                    exits.append({
                        "ticker": ticker,
                        "reason": "stop_hit",
                        "exit_price": today_close,
                        "details": (
                            f"{ticker} hit hard stop at ${stop_price:.2f}. "
                            f"Close=${today_close:.2f}, held {days_held} days."
                        ),
                    })
                # 2. Trend reversal: fast MA < slow MA
                # 2-day confirmed reversal: require fast < slow on both current and previous day
                elif (not pd.isna(current_fast) and not pd.isna(current_slow)
                      and current_fast < current_slow
                      and len(fast_ma) >= 2 and len(slow_ma) >= 2
                      and not pd.isna(fast_ma.iloc[-2]) and not pd.isna(slow_ma.iloc[-2])
                      and fast_ma.iloc[-2] < slow_ma.iloc[-2]):
                    exits.append({
                        "ticker": ticker,
                        "reason": "signal_exit",
                        "exit_price": today_close,
                        "details": (
                            f"{ticker} 2-day confirmed reversal: fast MA ${current_fast:.2f} "
                            f"crossed below slow MA ${current_slow:.2f}. "
                            f"Close=${today_close:.2f}, held {days_held} days."
                        ),
                    })
                # 3. Trailing stop hit
                elif today_close <= trailing_stop:
                    exits.append({
                        "ticker": ticker,
                        "reason": "trailing_stop",
                        "exit_price": today_close,
                        "details": (
                            f"{ticker} hit trailing stop. Peak=${highest_since_entry:.2f}, "
                            f"trail=${trailing_stop:.2f}, close=${today_close:.2f}. "
                            f"Held {days_held} days."
                        ),
                    })
                # 4. Time exit
                elif days_held >= self.max_hold_days:
                    exits.append({
                        "ticker": ticker,
                        "reason": "time_exit",
                        "exit_price": today_close,
                        "details": (
                            f"{ticker} time exit after {days_held} days "
                            f"(max={self.max_hold_days}). Close=${today_close:.2f}, "
                            f"entry=${entry_price:.2f}."
                        ),
                    })

            except Exception as e:
                self._logger.error(
                    f"{ticker}: exit check error: {e}", exc_info=True
                )
                continue

        return exits
