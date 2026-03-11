"""
Atlas Demark Sequential Strategy
========================================
TD Sequential buy setup: 9 consecutive closes below close 4 bars earlier. Enter on bar 9. Exit on TD sell setup or time.

Reference: Tom DeMark 'The New Science of Technical Analysis' (1994)
Generated: 2026-03-10T07:18:31.215126+00:00

Config Section: strategies.demark_sequential
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size

logger = logging.getLogger(__name__)


def _td_buy_count(close: pd.Series) -> pd.Series:
    """Vectorized TD buy setup count.

    A qualifying bar: close[i] < close[i-4].
    Count increments on each qualifying bar; resets to 0 on first failure.
    Returns Series of int counts aligned with close.
    """
    qualifies = close < close.shift(4)
    reset = (~qualifies).cumsum()
    return qualifies.astype(int).groupby(reset).cumsum()


def _td_sell_count(close: pd.Series) -> pd.Series:
    """Vectorized TD sell setup count.

    A qualifying bar: close[i] > close[i-4].
    """
    qualifies = close > close.shift(4)
    reset = (~qualifies).cumsum()
    return qualifies.astype(int).groupby(reset).cumsum()


class DemarkSequential(BaseStrategy):
    """TD Sequential buy setup: 9 consecutive closes below close 4 bars earlier"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("demark_sequential", {})

        # Core parameters
        self.setup_bars = strat_cfg.get("setup_bars", 9)
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)
        self.max_hold_days = strat_cfg.get("max_hold_days", 10)
        self.sma200_filter = strat_cfg.get("sma200_filter", False)  # OFF → more trades
        self.rsi_max = strat_cfg.get("rsi_max", 75)                # avoid overbought
        self.profit_target_atr_mult = strat_cfg.get("profit_target_atr_mult", 3.0)

        self._logger.info(
            f"DemarkSequential initialized: setup_bars={self.setup_bars}, "
            f"atr_stop={self.atr_stop_mult}x, max_hold={self.max_hold_days}, "
            f"sma200={'ON' if self.sma200_filter else 'OFF'}, rsi_max={self.rsi_max}"
        )

    @property
    def name(self) -> str:
        return "demark_sequential"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Generate demark_sequential entry signals.

        Entry logic:
          - TD buy setup: close < close[i-4] for 9 consecutive bars (fully vectorized)
          - Optional SMA-200 filter
          - RSI < rsi_max to avoid overbought entries
          - Stop: entry - atr_stop_mult * ATR
          - Target: entry + profit_target_atr_mult * ATR
        """
        signals: List[Signal] = []
        held = self._get_held_tickers(existing_positions)

        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 0.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0)
        min_position_value = self.fees_config.get("min_position_value", 0.0)
        max_position_value = (
            self.config.get("trading", {})
            .get("live_safety", {})
            .get("max_order_value", 0.0)
        )

        min_rows = max(210, self.atr_period + 20, self.setup_bars + 10)

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

                # ── TD buy setup count (vectorized) ──
                buy_count = _td_buy_count(close)
                current_count = int(buy_count.iloc[-1])

                # Signal fires exactly on bar 9
                if current_count != self.setup_bars:
                    continue

                # ── Optional SMA-200 trend filter ──
                if self.sma200_filter:
                    sma200 = close.rolling(200).mean()
                    if pd.isna(sma200.iloc[-1]) or close.iloc[-1] <= sma200.iloc[-1]:
                        continue

                # ── RSI filter — avoid chasing overbought bounces ──
                rsi = calc_rsi(close, 14)
                rsi_val = float(rsi.iloc[-1])
                if not np.isnan(rsi_val) and rsi_val > self.rsi_max:
                    continue

                # ── ATR for stops ──
                atr = calc_atr(high, low, close, self.atr_period)
                atr_val = float(atr.iloc[-1])
                if atr_val <= 0 or np.isnan(atr_val):
                    continue

                entry_price = float(close.iloc[-1])
                stop_price = entry_price - self.atr_stop_mult * atr_val
                if stop_price <= 0 or stop_price >= entry_price:
                    continue

                take_profit: Optional[float] = None
                if self.profit_target_atr_mult > 0:
                    take_profit = entry_price + self.profit_target_atr_mult * atr_val

                # ── Position sizing ──
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

                # ── Confidence ──
                confidence = 0.65
                if not np.isnan(rsi_val):
                    if rsi_val < 30:
                        confidence = 0.80
                    elif rsi_val < 45:
                        confidence = 0.72

                rationale = (
                    f"{ticker}: TD buy setup complete ({self.setup_bars} bars), "
                    f"close={entry_price:.2f}, RSI={rsi_val:.1f}, ATR={atr_val:.3f}"
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
                    confidence=round(confidence, 3),
                    rationale=rationale,
                    features={
                        "td_buy_count": current_count,
                        "rsi": round(rsi_val, 2) if not np.isnan(rsi_val) else None,
                        "atr": round(atr_val, 4),
                        "atr_stop_mult": self.atr_stop_mult,
                    },
                    market_id=getattr(self, "market_id", ""),
                ))

            except Exception as e:
                self._logger.warning(f"{ticker}: signal generation failed: {e}")
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
          3. TD sell setup complete (9 consecutive closes > close[i-4])
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
            if len(df) < 5:
                continue

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

            # 3. TD sell setup complete
            sell_count = _td_sell_count(df["close"])
            if int(sell_count.iloc[-1]) >= self.setup_bars:
                exits.append({
                    "ticker": ticker,
                    "reason": "signal_exit",
                    "exit_price": current_price,
                    "details": f"TD sell setup complete ({self.setup_bars} bars)",
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

        return exits


# Default parameter grid for optimization
PARAM_GRID = {
    "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
    "max_hold_days": [5, 10, 15, 20],
    "profit_target_atr_mult": [2.0, 3.0, 4.0],
    "rsi_max": [60, 70, 80],
}
