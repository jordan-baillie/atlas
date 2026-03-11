"""
Atlas Gap And Go Strategy
========================================
Buy stocks that gap UP > 2% at open with volume confirmation. Ride momentum.
Exit: ATR trailing stop or time limit.

A gap-up occurs when today's open is > gap_threshold % above yesterday's close.
With volume confirmation (volume surge), this signals institutional interest and
momentum continuation. We enter at the close of the gap day and ride momentum.

Reference: Quantified Strategies gap research, related to Opening Gap
Generated: 2026-03-10T07:18:31.216660+00:00

Config Section: strategies.gap_and_go
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size, calc_volume_ratio

logger = logging.getLogger(__name__)


class GapAndGo(BaseStrategy):
    """Buy stocks that gap UP > 2% at open with volume confirmation."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("gap_and_go", {})

        # Gap detection
        self.gap_threshold = strat_cfg.get("gap_threshold", 0.02)       # 2% gap-up minimum
        self.gap_max = strat_cfg.get("gap_max", 0.15)                    # Ignore extreme gaps > 15%

        # Volume confirmation: gap day must show elevated volume
        self.vol_lookback = strat_cfg.get("vol_lookback", 20)
        self.vol_min_ratio = strat_cfg.get("vol_min_ratio", 1.5)          # 1.5x avg volume

        # Momentum filter: RSI range
        self.rsi_period = strat_cfg.get("rsi_period", 10)
        self.rsi_min = strat_cfg.get("rsi_min", 50)                       # Not weak
        self.rsi_max = strat_cfg.get("rsi_max", 80)                       # Not overbought

        # Gap must hold: today's close above gap open (gap didn't fail)
        self.require_close_above_open = strat_cfg.get("require_close_above_open", True)

        # Trend filter
        self.sma200_filter = strat_cfg.get("sma200_filter", True)
        self.trend_sma = strat_cfg.get("trend_sma", 50)                   # Additional trend filter

        # Risk management
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)
        self.profit_target_atr_mult = strat_cfg.get("profit_target_atr_mult", 2.5)
        self.max_hold_days = strat_cfg.get("max_hold_days", 5)

        self._logger.info(
            f"GapAndGo initialized: gap>={self.gap_threshold:.0%} (max {self.gap_max:.0%}), "
            f"vol>={self.vol_min_ratio}x, rsi=[{self.rsi_min},{self.rsi_max}], "
            f"sma200={'ON' if self.sma200_filter else 'OFF'}, "
            f"max_hold={self.max_hold_days}d"
        )

    @property
    def name(self) -> str:
        return "gap_and_go"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Generate gap-and-go entry signals.

        Signal when:
            1. Today's open is gap_threshold % above yesterday's close (gap up)
            2. Gap is not extreme (below gap_max)
            3. Volume today is above vol_min_ratio * avg_volume (institutional interest)
            4. RSI in [rsi_min, rsi_max] (momentum confirmed, not overbought)
            5. Close >= Open (gap held through the day — no gap fill reversal)
            6. Optional: price above SMA-200 and trend_sma
        """
        signals: List[Signal] = []
        held = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0008)

        min_rows = max(
            200 if self.sma200_filter else 0,
            self.trend_sma,
            self.vol_lookback,
            self.rsi_period,
            self.atr_period,
        ) + 5

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
                open_ = df["open"]
                volume = df["volume"]

                today_open = float(open_.iloc[-1])
                today_close = float(close.iloc[-1])
                prev_close = float(close.iloc[-2])

                # ── Gap detection ──
                gap_pct = (today_open - prev_close) / prev_close
                if gap_pct < self.gap_threshold:
                    continue
                if gap_pct > self.gap_max:
                    self._logger.debug(f"{ticker}: gap {gap_pct:.1%} > max {self.gap_max:.1%}, skipping")
                    continue

                # ── Gap must hold: close at or above open ──
                if self.require_close_above_open and today_close < today_open * 0.995:
                    self._logger.debug(f"{ticker}: gap filled — close {today_close:.2f} < open {today_open:.2f}")
                    continue

                # ── Volume confirmation ──
                vol_ratio = calc_volume_ratio(volume, lookback=self.vol_lookback)
                current_vol_ratio = float(vol_ratio.iloc[-1])
                if pd.isna(current_vol_ratio) or current_vol_ratio < self.vol_min_ratio:
                    continue

                # ── Momentum: RSI check ──
                rsi = calc_rsi(close, period=self.rsi_period)
                current_rsi = float(rsi.iloc[-1])
                if pd.isna(current_rsi):
                    continue
                if not (self.rsi_min <= current_rsi <= self.rsi_max):
                    continue

                # ── Trend filter: above trend SMA ──
                trend_sma_val = float(close.rolling(self.trend_sma).mean().iloc[-1])
                if pd.isna(trend_sma_val) or today_close < trend_sma_val:
                    continue

                # ── SMA-200 filter ──
                if self.sma200_filter:
                    sma200 = float(close.rolling(200).mean().iloc[-1])
                    if pd.isna(sma200) or today_close < sma200:
                        continue

                # ── ATR and position sizing ──
                atr = calc_atr(high, low, close, period=self.atr_period)
                current_atr = float(atr.iloc[-1])
                if pd.isna(current_atr) or current_atr <= 0:
                    continue

                entry_price = today_close
                stop_price = entry_price - self.atr_stop_mult * current_atr
                if stop_price <= 0 or stop_price >= entry_price:
                    continue
                take_profit = entry_price + self.profit_target_atr_mult * current_atr

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

                # ── Confidence scoring ──
                # Higher gap = more conviction
                gap_conf = min(0.15, (gap_pct - self.gap_threshold) / 0.05 * 0.15)
                # Higher volume = more conviction
                vol_conf = min(0.10, (current_vol_ratio - self.vol_min_ratio) / 2.0 * 0.10)
                # RSI sweet spot (60-70) = strongest momentum
                rsi_conf = min(0.10, max(0.0, (current_rsi - self.rsi_min) / (self.rsi_max - self.rsi_min) * 0.10))
                confidence = round(min(0.95, 0.65 + gap_conf + vol_conf + rsi_conf), 4)

                rationale = (
                    f"{ticker}: Gap-up {gap_pct:.1%} (open={today_open:.2f} vs prev_close={prev_close:.2f}), "
                    f"gap held (close={today_close:.2f}), "
                    f"volume {current_vol_ratio:.1f}x avg, RSI={current_rsi:.1f}. "
                    f"Entry={entry_price:.2f}, stop={stop_price:.2f}, target={take_profit:.2f}."
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
                        "gap_pct": round(gap_pct, 4),
                        "vol_ratio": round(current_vol_ratio, 2),
                        "rsi": round(current_rsi, 2),
                        "atr": round(current_atr, 4),
                        "close": round(today_close, 4),
                    },
                    timestamp=datetime.now(),
                ))

            except Exception as e:
                self._logger.error(f"{ticker}: error in gap_and_go signal gen: {e}", exc_info=True)
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
            3. Gap reversal: close drops below the prior day's close (momentum lost)
            4. Time exit (max_hold_days)
        """
        exits = []
        for pos in positions:
            if pos.get("strategy") != self.name:
                continue
            ticker = pos.get("ticker")
            if not ticker or ticker not in data:
                continue

            df = data[ticker]
            if len(df) < 2:
                continue

            try:
                current_price = float(df["close"].iloc[-1])
                stop_price = pos.get("stop_price", 0)
                take_profit = pos.get("take_profit")
                entry_price = pos.get("entry_price", current_price)

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

                # 3. Momentum lost: close below entry day close (gap gave back gains)
                prev_close = float(df["close"].iloc[-2])
                if current_price < prev_close * 0.99 and current_price < entry_price:
                    exits.append({
                        "ticker": ticker,
                        "reason": "signal_exit",
                        "exit_price": current_price,
                        "details": f"Gap momentum lost: close {current_price:.2f} < prev {prev_close:.2f} and below entry {entry_price:.2f}",
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
    "gap_threshold": [0.015, 0.02, 0.025, 0.03],
    "vol_min_ratio": [1.2, 1.5, 2.0, 2.5],
    "atr_stop_mult": [1.5, 2.0, 2.5],
    "profit_target_atr_mult": [2.0, 2.5, 3.0],
    "max_hold_days": [3, 5, 7, 10],
    "rsi_min": [45, 50, 55],
    "rsi_max": [75, 80, 85],
}
