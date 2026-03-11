"""
Atlas Monthly Rotation Strategy
========================================
Monthly rebalance: rank sectors/stocks by 6-month momentum. Hold top N. Rotate monthly. Cash filter: below SMA-200 -> cash.

Reference: Faber 'A Quantitative Approach to TAA' (2007), Antonacci dual momentum
Generated: 2026-03-10T07:18:31.223004+00:00

Config Section: strategies.monthly_rotation
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_position_size

logger = logging.getLogger(__name__)


class MonthlyRotation(BaseStrategy):
    """Monthly rebalance: rank stocks/sectors by momentum, hold top N."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("monthly_rotation", {})

        # Momentum ranking
        self.momentum_lookback = strat_cfg.get("momentum_lookback", 63)  # ~3 months
        self.top_n = strat_cfg.get("top_n", 5)                           # Hold top N stocks

        # Rebalance frequency
        self.rebalance_day = strat_cfg.get("rebalance_day", 1)           # 1 = first day of month

        # Trend filter
        self.sma200_filter = strat_cfg.get("sma200_filter", True)        # Must be above SMA200

        # Position quality filters
        self.min_momentum = strat_cfg.get("min_momentum", 0.0)           # Require positive momentum

        # Risk management
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.5)        # Wider stop for monthly
        self.max_hold_days = strat_cfg.get("max_hold_days", 35)          # ~1 month

        # Track last rebalance to avoid firing every day
        self._last_rebalance_month: Optional[int] = None
        self._last_rebalance_year: Optional[int] = None

        self._logger.info(
            f"MonthlyRotation initialized: lookback={self.momentum_lookback}d, "
            f"top_n={self.top_n}, sma200={'ON' if self.sma200_filter else 'OFF'}, "
            f"min_mom={self.min_momentum:.0%}"
        )

    @property
    def name(self) -> str:
        return "monthly_rotation"

    def _is_rebalance_day(self, data: Dict[str, pd.DataFrame]) -> bool:
        """Check if today is the first trading day of a new month."""
        # Get the current date from any ticker
        for df in data.values():
            if not df.empty:
                current_date = df.index[-1]
                current_month = current_date.month
                current_year = current_date.year

                # First call: always rebalance
                if self._last_rebalance_month is None:
                    self._last_rebalance_month = current_month
                    self._last_rebalance_year = current_year
                    return True

                # New month
                if current_month != self._last_rebalance_month or current_year != self._last_rebalance_year:
                    self._last_rebalance_month = current_month
                    self._last_rebalance_year = current_year
                    return True

                return False
        return False

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Generate monthly rotation entry signals.

        Rebalances once per month. On rebalance day:
          1. Compute momentum (ROC over momentum_lookback) for all tickers
          2. Filter: must be above SMA-200 (cash filter for downtrends)
          3. Filter: must have positive momentum (absolute momentum filter)
          4. Rank by momentum descending
          5. Generate signals for top N tickers not already held
        """
        signals: List[Signal] = []
        held = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0008)

        # Only rebalance once per month
        if not self._is_rebalance_day(data):
            return signals

        min_rows = max(
            200 if self.sma200_filter else 0,
            self.momentum_lookback + 5,
            self.atr_period + 5,
        )

        # Step 1: Rank all eligible tickers by momentum
        ranked: List[Dict] = []
        for ticker, df in data.items():
            if not self._has_sufficient_data(df, min_rows):
                continue

            close = df["close"]
            high = df["high"]
            low = df["low"]
            current_close = float(close.iloc[-1])

            try:
                # SMA-200 cash filter
                if self.sma200_filter:
                    sma200 = float(close.rolling(200).mean().iloc[-1])
                    if pd.isna(sma200) or current_close < sma200:
                        continue  # In downtrend — cash instead

                # Momentum: rate of change over lookback period
                prior_close = float(close.iloc[-self.momentum_lookback - 1])
                if prior_close <= 0:
                    continue
                momentum = (current_close - prior_close) / prior_close

                # Absolute momentum filter: must be positive
                if momentum < self.min_momentum:
                    continue

                # ATR for stop sizing
                atr = calc_atr(high, low, close, self.atr_period)
                atr_val = float(atr.iloc[-1])
                if pd.isna(atr_val) or atr_val <= 0:
                    continue

                ranked.append({
                    "ticker": ticker,
                    "momentum": momentum,
                    "close": current_close,
                    "atr": atr_val,
                    "df": df,
                })
            except Exception:
                continue

        # Step 2: Sort by momentum (descending) and take top N
        ranked.sort(key=lambda x: x["momentum"], reverse=True)
        top_candidates = ranked[:self.top_n * 2]  # Extra buffer in case some are held

        # Step 3: Generate signals for tickers not already held
        slots_available = self.top_n - len([p for p in held if p in {r["ticker"] for r in ranked[:self.top_n]}])
        signals_generated = 0

        for candidate in top_candidates:
            if signals_generated >= slots_available:
                break
            if not self._can_open_position(existing_positions):
                break

            ticker = candidate["ticker"]
            if ticker in held:
                continue  # Already holding this top performer

            current_close = candidate["close"]
            atr_val = candidate["atr"]

            entry_price = current_close
            stop_price = entry_price - self.atr_stop_mult * atr_val
            # No fixed take-profit for rotation — hold until next rebalance
            take_profit = None

            if stop_price <= 0 or stop_price >= entry_price:
                continue

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

            rank_in_top = next((i for i, r in enumerate(ranked) if r["ticker"] == ticker), 0)
            momentum_pct = candidate["momentum"]

            # Confidence: higher rank and stronger momentum = higher confidence
            rank_conf = min(0.15, (self.top_n - rank_in_top) / self.top_n * 0.15)
            mom_conf = min(0.10, min(momentum_pct, 0.30) / 0.30 * 0.10)
            confidence = round(min(0.95, 0.65 + rank_conf + mom_conf), 4)

            rationale = (
                f"{ticker}: Monthly rotation — rank #{rank_in_top + 1} of {len(ranked)} by "
                f"{self.momentum_lookback}d momentum={momentum_pct:.1%}. "
                f"Entry={entry_price:.2f}, stop={stop_price:.2f} (ATR x {self.atr_stop_mult})."
            )

            signals.append(Signal(
                ticker=ticker,
                strategy=self.name,
                direction="long",
                entry_price=entry_price,
                stop_price=round(stop_price, 4),
                take_profit=take_profit,
                position_size=pos["shares"],
                position_value=pos["position_value"],
                risk_amount=pos["total_risk"],
                confidence=confidence,
                rationale=rationale,
                features={
                    "momentum": round(momentum_pct, 4),
                    "rank": rank_in_top + 1,
                    "total_candidates": len(ranked),
                    "atr": round(atr_val, 4),
                    "close": round(current_close, 4),
                },
                timestamp=datetime.now(),
            ))
            signals_generated += 1

        self._logger.info(
            f"{self.name}: {len(signals)} signals on rebalance day from {len(ranked)} eligible tickers"
        )
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check positions for exit conditions.

        Exit priority:
          1. Stop-loss hit
          2. Trend breakdown: price falls below SMA-200
          3. Time exit (hold until next monthly rebalance)
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

                # 1. Stop-loss
                if stop_price and current_price <= stop_price:
                    exits.append({
                        "ticker": ticker,
                        "reason": "stop_hit",
                        "exit_price": current_price,
                        "details": f"Price {current_price:.2f} <= stop {stop_price:.2f}",
                    })
                    continue

                # 2. SMA-200 cash filter: exit if stock falls below SMA-200
                if self.sma200_filter:
                    close = df["close"]
                    sma200 = float(close.rolling(200).mean().iloc[-1])
                    if not pd.isna(sma200) and current_price < sma200 * 0.98:
                        exits.append({
                            "ticker": ticker,
                            "reason": "signal_exit",
                            "exit_price": current_price,
                            "details": f"Below SMA200 {sma200:.2f}: rotate to cash",
                        })
                        continue

                # 3. Time exit (hold ~1 month then rebalance naturally)
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
                            "details": f"Held {days_held} days >= max {self.max_hold_days} (monthly rebalance)",
                        })
                        continue

            except Exception as e:
                self._logger.error(f"{ticker}: exit check error: {e}", exc_info=True)

        return exits


# Default parameter grid for optimization
PARAM_GRID = {
    "momentum_lookback": [21, 63, 126],
    "top_n": [3, 5, 10],
    "atr_stop_mult": [2.0, 2.5, 3.0],
    "min_momentum": [0.0, 0.05, 0.10],
    "max_hold_days": [21, 28, 35],
}
