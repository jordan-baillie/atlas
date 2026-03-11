"""
Atlas Heikin Ashi Reversal Strategy
========================================
3+ red Heikin-Ashi candles followed by green doji/reversal in uptrend. Enter long.
Exit on 2 consecutive red HA candles.

Heikin-Ashi candles smooth out price noise and make trends and reversals clearer:
  HA_close = (O + H + L + C) / 4
  HA_open  = (prev_HA_open + prev_HA_close) / 2
  HA_high  = max(H, HA_open, HA_close)
  HA_low   = min(L, HA_open, HA_close)

Red HA candle: HA_close < HA_open
Green HA candle: HA_close >= HA_open

Signal: 3+ consecutive red HA bars → reversal candle (green) → long entry.
Exit: 2 consecutive red HA candles (trend flipped) OR ATR stop OR time.

Reference: Japanese candlestick patterns, quantified by Quantified Strategies
Generated: 2026-03-10T07:18:31.219740+00:00

Config Section: strategies.heikin_ashi_reversal
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size, calc_volume_ratio

logger = logging.getLogger(__name__)


def _calc_heikin_ashi(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.DataFrame:
    """Calculate Heikin-Ashi candles from OHLC data.

    Args:
        open_: Open prices.
        high: High prices.
        low: Low prices.
        close: Close prices.

    Returns:
        DataFrame with columns: ha_open, ha_high, ha_low, ha_close, ha_is_green.
    """
    ha_close = (open_ + high + low + close) / 4.0

    # HA open: iterative (each depends on previous)
    ha_open = pd.Series(np.nan, index=open_.index)
    ha_open.iloc[0] = (open_.iloc[0] + close.iloc[0]) / 2.0
    for i in range(1, len(ha_open)):
        ha_open.iloc[i] = (ha_open.iloc[i - 1] + ha_close.iloc[i - 1]) / 2.0

    ha_high = pd.concat([high, ha_open, ha_close], axis=1).max(axis=1)
    ha_low = pd.concat([low, ha_open, ha_close], axis=1).min(axis=1)
    ha_is_green = (ha_close >= ha_open).astype(int)

    return pd.DataFrame({
        "ha_open": ha_open,
        "ha_high": ha_high,
        "ha_low": ha_low,
        "ha_close": ha_close,
        "ha_is_green": ha_is_green,
    }, index=open_.index)


class HeikinAshiReversal(BaseStrategy):
    """3+ red Heikin-Ashi candles followed by green reversal in uptrend."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("heikin_ashi_reversal", {})

        # Signal parameters
        self.min_red_bars = strat_cfg.get("min_red_bars", 3)            # Min consecutive red HA bars before reversal
        self.max_red_bars = strat_cfg.get("max_red_bars", 10)           # Max (beyond = trend collapse)
        self.reversal_bars = strat_cfg.get("reversal_bars", 1)          # Green bars needed to confirm reversal

        # Body size filter for reversal candle (optional)
        # Doji/small body allowed but HA candle must be green
        self.min_body_pct = strat_cfg.get("min_body_pct", 0.0)         # 0 = disabled

        # Trend filters
        self.sma200_filter = strat_cfg.get("sma200_filter", True)
        self.trend_sma = strat_cfg.get("trend_sma", 50)

        # RSI filter: not already overbought when we enter
        self.rsi_period = strat_cfg.get("rsi_period", 14)
        self.rsi_max = strat_cfg.get("rsi_max", 70)
        self.rsi_min = strat_cfg.get("rsi_min", 25)                    # Minimum RSI to not be in freefall

        # Volume: elevated volume on reversal candle preferred
        self.vol_lookback = strat_cfg.get("vol_lookback", 20)
        self.vol_min_ratio = strat_cfg.get("vol_min_ratio", 0.8)       # Loose filter (0.8x avg = sufficient)

        # Exit trigger: N consecutive red HA bars after entry
        self.exit_red_bars = strat_cfg.get("exit_red_bars", 2)

        # Risk management
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)
        self.profit_target_atr_mult = strat_cfg.get("profit_target_atr_mult", 2.0)
        self.max_hold_days = strat_cfg.get("max_hold_days", 10)

        self._logger.info(
            f"HeikinAshiReversal initialized: min_red={self.min_red_bars}, "
            f"exit_red={self.exit_red_bars}, rsi<={self.rsi_max}, "
            f"sma200={'ON' if self.sma200_filter else 'OFF'}"
        )

    @property
    def name(self) -> str:
        return "heikin_ashi_reversal"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Generate Heikin-Ashi reversal entry signals.

        Signal when (checking last bar):
            1. Previous N bars were red HA candles (≥ min_red_bars)
            2. Today is a green HA candle (reversal)
            3. RSI in valid range (not overbought, not in freefall)
            4. Price above trend SMA (uptrend context)
            5. Optional: SMA-200 filter
        """
        signals: List[Signal] = []
        held = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0008)

        min_rows = max(
            200 if self.sma200_filter else 0,
            self.trend_sma,
            self.rsi_period,
            self.atr_period,
            self.vol_lookback,
            self.min_red_bars + 5,
        ) + 10

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

                # ── Compute Heikin-Ashi candles ──
                ha = _calc_heikin_ashi(open_, high, low, close)
                ha_green = ha["ha_is_green"]

                # Today must be green
                if ha_green.iloc[-1] != 1:
                    continue

                # Count consecutive red bars immediately before today
                consec_red = 0
                for i in range(2, min(self.max_red_bars + 3, len(ha_green))):
                    if ha_green.iloc[-i] == 0:
                        consec_red += 1
                    else:
                        break  # Stop at first non-red bar

                if consec_red < self.min_red_bars:
                    continue
                if consec_red > self.max_red_bars:
                    self._logger.debug(
                        f"{ticker}: {consec_red} red HA bars > max {self.max_red_bars}, "
                        f"possible trend collapse, skipping"
                    )
                    continue

                # ── Trend filter: price above SMA ──
                today_close = float(close.iloc[-1])
                trend_sma_val = float(close.rolling(self.trend_sma).mean().iloc[-1])
                if pd.isna(trend_sma_val) or today_close < trend_sma_val:
                    continue

                # ── SMA-200 filter ──
                if self.sma200_filter:
                    sma200 = float(close.rolling(200).mean().iloc[-1])
                    if pd.isna(sma200) or today_close < sma200:
                        continue

                # ── RSI check ──
                rsi = calc_rsi(close, period=self.rsi_period)
                current_rsi = float(rsi.iloc[-1])
                if pd.isna(current_rsi):
                    continue
                if current_rsi > self.rsi_max or current_rsi < self.rsi_min:
                    continue

                # ── Volume confirmation ──
                vol_ratio = calc_volume_ratio(volume, lookback=self.vol_lookback)
                current_vol_ratio = float(vol_ratio.iloc[-1])
                if not pd.isna(current_vol_ratio) and current_vol_ratio < self.vol_min_ratio:
                    continue

                # ── ATR and position sizing ──
                atr = calc_atr(high, low, close, period=self.atr_period)
                current_atr = float(atr.iloc[-1])
                if pd.isna(current_atr) or current_atr <= 0:
                    continue

                entry_price = today_close
                # Stop below today's HA low (natural support) or ATR-based
                ha_low_today = float(ha["ha_low"].iloc[-1])
                stop_price = min(entry_price - self.atr_stop_mult * current_atr, ha_low_today * 0.995)
                if stop_price <= 0 or stop_price >= entry_price:
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
                # More red bars before reversal = stronger exhaustion = higher conf
                red_conf = min(0.15, (consec_red - self.min_red_bars) / 3.0 * 0.15)
                # Vol confirmation
                vol_conf = min(0.10, max(0.0, (current_vol_ratio - 1.0) * 0.05))
                # RSI sweet spot (40-60 after pullback = stronger reversal)
                rsi_conf = min(0.10, max(0.0, 0.10 - abs(current_rsi - 50) / 50.0 * 0.10))
                confidence = round(min(0.95, 0.65 + red_conf + vol_conf + rsi_conf), 4)

                ha_close_today = float(ha["ha_close"].iloc[-1])
                ha_open_today = float(ha["ha_open"].iloc[-1])
                body_size = abs(ha_close_today - ha_open_today)

                rationale = (
                    f"{ticker}: HA reversal — {consec_red} red HA bars then GREEN today "
                    f"(HA_close={ha_close_today:.2f} > HA_open={ha_open_today:.2f}, body={body_size:.2f}). "
                    f"RSI={current_rsi:.1f}, vol={current_vol_ratio:.1f}x. "
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
                        "consec_red_ha": consec_red,
                        "ha_close": round(ha_close_today, 4),
                        "ha_open": round(ha_open_today, 4),
                        "rsi": round(current_rsi, 2),
                        "vol_ratio": round(current_vol_ratio, 2),
                        "atr": round(current_atr, 4),
                        "close": round(today_close, 4),
                    },
                    timestamp=datetime.now(),
                ))

            except Exception as e:
                self._logger.error(f"{ticker}: error in HA reversal signal gen: {e}", exc_info=True)
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
            1. Stop-loss (ATR-based)
            2. Take-profit
            3. HA signal reversal: N consecutive red HA candles post-entry
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
            if len(df) < self.exit_red_bars + 2:
                continue

            try:
                close = df["close"]
                high = df["high"]
                low = df["low"]
                open_ = df["open"]
                current_price = float(close.iloc[-1])
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

                # 3. HA reversal exit: check if last exit_red_bars HA candles are all red
                ha = _calc_heikin_ashi(open_, high, low, close)
                ha_green = ha["ha_is_green"]
                recent_reds = all(ha_green.iloc[-i] == 0 for i in range(1, self.exit_red_bars + 1))
                if recent_reds:
                    exits.append({
                        "ticker": ticker,
                        "reason": "signal_exit",
                        "exit_price": current_price,
                        "details": f"HA trend reversed: {self.exit_red_bars} consecutive red HA bars",
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
                self._logger.error(f"{ticker}: HA reversal exit check error: {e}", exc_info=True)

        return exits


# Default parameter grid for optimization
PARAM_GRID = {
    "min_red_bars": [2, 3, 4, 5],
    "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
    "profit_target_atr_mult": [1.5, 2.0, 2.5, 3.0],
    "max_hold_days": [5, 8, 10, 15],
    "rsi_max": [65, 70, 75],
    "exit_red_bars": [1, 2, 3],
    "trend_sma": [20, 50],
}
