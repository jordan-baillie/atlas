"""
Atlas Momentum Breakout Strategy (Phase 8A)
================================================
Generates long signals when price breaks above N-day high
with trend alignment confirmation.

Key changes from original:
    - Removed hard volume filter (Phase 7A lesson: info-only)
    - Wider ATR stops (Phase 3-6 lesson: give trades room)
    - Revised confidence formula: breakout strength + trend alignment
    - Added trend MA filter for quality breakouts only

Entry Conditions:
    - Price closes above the highest close of the last N days (lookback_days)
    - Price is above trend_ma_period moving average (trend alignment)

Stop Loss:
    - Entry price - atr_stop_mult * ATR(atr_period)

Exit Conditions:
    - Trailing stop at trailing_stop_atr_mult * ATR below highest close since entry
    - Time-based exit after max_hold_days

Config Section: strategies.momentum_breakout
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_position_size

logger = logging.getLogger(__name__)


class MomentumBreakout(BaseStrategy):
    """Momentum breakout strategy: buy on N-day high breakout with trend alignment."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("momentum_breakout", {})
        self.lookback_days = strat_cfg.get("lookback_days", 20)
        self.volume_mult = strat_cfg.get("volume_confirmation_mult", 1.5)
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 3.5)
        self.trailing_stop_atr_mult = strat_cfg.get("trailing_stop_atr_mult", 4.0)
        self.max_hold_days = strat_cfg.get("max_hold_days", 20)
        self.trend_ma_period = strat_cfg.get("trend_ma_period", 50)
        self._logger.info(
            f"MomentumBreakout initialized: lookback={self.lookback_days}, "
            f"atr_stop={self.atr_stop_mult}, trailing={self.trailing_stop_atr_mult}, "
            f"trend_ma={self.trend_ma_period}"
        )

    @property
    def name(self) -> str:
        return "momentum_breakout"

    def precompute(self, data: Dict[str, pd.DataFrame]) -> None:
        """Pre-compute all indicators as DataFrame columns (called once before walk-forward)."""
        for ticker, df in data.items():
            close = df["close"]
            high = df["high"]
            low = df["low"]
            volume = df["volume"]
            df["_mb_trend_ma"] = close.rolling(window=self.trend_ma_period).mean()
            df["_mb_lookback_high"] = close.rolling(self.lookback_days).max().shift(1)
            df["_mb_avg_vol"] = volume.rolling(20).mean().shift(1)
            df["_mb_atr"] = calc_atr(high, low, close, period=self.atr_period)
        self._precomputed = True

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Scan all tickers for momentum breakout entry signals."""
        signals: List[Signal] = []
        held_tickers = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0008)

        min_rows = max(self.lookback_days, self.atr_period, self.trend_ma_period) + 10

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
                volume = df["volume"]
                high = df["high"]
                low = df["low"]

                today_close = close.iloc[-1]
                today_volume = volume.iloc[-1]

                # N-day high (excluding today) — pre-computed
                lookback_high = df["_mb_lookback_high"].iloc[-1]
                if pd.isna(lookback_high):
                    continue

                # Breakout condition
                if today_close <= lookback_high:
                    continue

                # Trend alignment: price above slow MA — pre-computed
                trend_ma = df["_mb_trend_ma"]
                current_trend_ma = trend_ma.iloc[-1]
                if pd.isna(current_trend_ma):
                    continue
                if today_close <= current_trend_ma:
                    continue

                # Volume info-only (Phase 7A lesson: no hard filter) — pre-computed
                avg_volume_20 = df["_mb_avg_vol"].iloc[-1]
                volume_ratio = today_volume / avg_volume_20 if (not pd.isna(avg_volume_20) and avg_volume_20 > 0) else 0

                # ATR for stop placement — pre-computed
                atr = df["_mb_atr"]
                current_atr = atr.iloc[-1]
                if pd.isna(current_atr) or current_atr <= 0:
                    continue

                entry_price = today_close
                stop_price = entry_price - (self.atr_stop_mult * current_atr)
                if stop_price <= 0:
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
                    )
                except ValueError as e:
                    self._logger.debug(f"{ticker}: position sizing error: {e}")
                    continue

                if pos["shares"] <= 0:
                    continue

                # Confidence formula (Phase 8A):
                # Base 0.6 for meeting entry criteria (breakout + trend aligned)
                # + 0.2 * breakout_strength_bonus (how far above N-day high)
                # + 0.2 * trend_strength_bonus (how far above trend MA)
                breakout_strength = (today_close - lookback_high) / lookback_high
                trend_strength = (today_close - current_trend_ma) / current_trend_ma

                breakout_bonus = min(1.0, breakout_strength / 0.03)
                trend_bonus = min(1.0, trend_strength / 0.10)

                confidence = min(1.0, 0.6 + 0.2 * breakout_bonus + 0.2 * trend_bonus)
                confidence = max(0.1, confidence)

                # Volume info note
                is_volume_high = volume_ratio > self.volume_mult
                if is_volume_high:
                    vol_note = f"Volume surged to {volume_ratio:.1f}x avg (HIGH). "
                elif volume_ratio < 0.6:
                    vol_note = f"Volume low {volume_ratio:.1f}x avg. "
                else:
                    vol_note = f"Volume {volume_ratio:.1f}x avg. "

                rationale = (
                    f"{ticker} broke above {self.lookback_days}-day high of "
                    f"${lookback_high:.2f} closing at ${today_close:.2f} "
                    f"(+{breakout_strength*100:.1f}%). "
                    f"Trend aligned: price > {self.trend_ma_period}MA "
                    f"${current_trend_ma:.2f} (+{trend_strength*100:.1f}%). "
                    f"{vol_note}"
                    f"ATR={current_atr:.2f}, stop=${stop_price:.2f}."
                )

                signal = Signal(
                    ticker=ticker,
                    strategy=self.name,
                    direction="long",
                    entry_price=entry_price,
                    stop_price=round(stop_price, 4),
                    take_profit=None,
                    position_size=pos["shares"],
                    position_value=pos["position_value"],
                    risk_amount=pos["total_risk"],
                    confidence=round(confidence, 4),
                    rationale=rationale,
                    features={
                        "lookback_high": round(lookback_high, 4),
                        "breakout_pct": round(breakout_strength * 100, 2),
                        "volume_ratio": round(volume_ratio, 2),
                        "trend_ma": round(current_trend_ma, 4),
                        "trend_strength_pct": round(trend_strength * 100, 2),
                        "atr": round(current_atr, 4),
                        "close": round(today_close, 4),
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

        self._logger.info(f"MomentumBreakout generated {len(signals)} signals")
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check open momentum positions for exit conditions."""
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

                days_held = (today_date - entry_date).days

                # Use pre-computed ATR
                current_atr = df["_mb_atr"].iloc[-1]
                if pd.isna(current_atr):
                    current_atr = abs(entry_price - stop_price) / self.atr_stop_mult

                mask = df.index >= entry_date
                if mask.any():
                    highest_since_entry = close[mask].max()
                else:
                    highest_since_entry = entry_price

                trailing_stop = highest_since_entry - (self.trailing_stop_atr_mult * current_atr)

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
                    f"{ticker}: exit check error: {e}", exc_info=True,
                )
                continue

        return exits
