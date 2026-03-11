"""
Atlas Overnight Return Strategy
========================================
Captures the overnight premium: stocks historically earn more returns
overnight (close-to-open) than intraday (open-to-close).

Reference: Cliff et al. "Overnight Return, the Invisible Hand Behind
  Intraday Returns?" (2019), Quantpedia #53.
Academic basis: ~70% of S&P500 daily returns come overnight; the intraday
session is typically where losses occur (Berkman et al. 2012).

Core logic:
  1. Select stocks with strong recent upward momentum
     (price above SMA-20, trend in force)
  2. Filter for mild pullback state (RSI 45-65: not overbought, not breaking)
  3. Confirm with volume above average (institutional interest)
  4. Buy at close today (model as position held overnight)
  5. Exit after 1-3 days (approximates close-to-open holding)

Note: With daily bar data we model this as a 1-day hold since we cannot
literally transact at the open. The close-to-close 1-day return has strong
correlation with the overnight premium in large-cap SP500 names.

Config Section: strategies.overnight_return
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size, calc_volume_ratio

logger = logging.getLogger(__name__)


class OvernightReturn(BaseStrategy):
    """Buy strong-momentum stocks at close to capture overnight premium."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("overnight_return", {})

        # Momentum filters
        self.sma_trend_period = strat_cfg.get("sma_trend_period", 20)
        self.rsi_period = strat_cfg.get("rsi_period", 14)
        self.rsi_min = strat_cfg.get("rsi_min", 45)    # Not too oversold
        self.rsi_max = strat_cfg.get("rsi_max", 70)    # Not overbought

        # Intraday quality: stock must close in upper portion of day's range
        # (IBS >= ibs_min means closed near high, institutional buying pressure)
        self.ibs_min = strat_cfg.get("ibs_min", 0.5)

        # Momentum quality: recent N-day return must be positive
        self.momentum_period = strat_cfg.get("momentum_period", 5)
        self.momentum_min = strat_cfg.get("momentum_min", 0.0)

        # Volume confirmation
        self.vol_lookback = strat_cfg.get("vol_lookback", 20)
        self.vol_min_ratio = strat_cfg.get("vol_min_ratio", 0.8)

        # SMA-200 uptrend filter
        self.sma200_filter = strat_cfg.get("sma200_filter", True)

        # Risk management
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)

        # Hold for 1-3 days (1-2 overnight periods)
        self.max_hold_days = strat_cfg.get("max_hold_days", 2)

        self._logger.info(
            f"OvernightReturn initialized: sma_trend={self.sma_trend_period}, "
            f"rsi={self.rsi_min}-{self.rsi_max}, ibs_min={self.ibs_min}, "
            f"momentum_period={self.momentum_period}, "
            f"vol_min={self.vol_min_ratio}, sma200={'ON' if self.sma200_filter else 'OFF'}, "
            f"max_hold={self.max_hold_days}"
        )

    @property
    def name(self) -> str:
        return "overnight_return"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Scan for overnight return candidates.

        Entry criteria (all must be met):
          1. Price > SMA(sma_trend_period) — short-term uptrend in force
          2. RSI between rsi_min and rsi_max — not exhausted, not breaking
          3. IBS >= ibs_min — closed near day's high (buying pressure)
          4. Recent N-day return positive (momentum confirmed)
          5. Volume >= vol_min_ratio × avg volume (institutional interest)
          6. Price > SMA-200 (long-term uptrend, if enabled)
        """
        signals: List[Signal] = []
        held = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0008)
        min_position_value = self.fees_config.get("min_position_value", 0.0)
        max_position_value = self.config.get("trading", {}).get(
            "live_safety", {}
        ).get("max_order_value", 0.0)

        min_rows = max(
            200 + 10 if self.sma200_filter else 0,
            self.sma_trend_period + self.momentum_period + 10,
            self.rsi_period + 5,
            self.atr_period + 5,
            self.vol_lookback + 5,
        )

        for ticker, df in data.items():
            try:
                if ticker in held:
                    continue
                if not self._can_open_position(existing_positions):
                    self._logger.debug("Max positions reached")
                    break
                if not self._has_sufficient_data(df, min_rows):
                    continue

                close = df["close"]
                high = df["high"]
                low = df["low"]
                volume = df["volume"]

                current_close = float(close.iloc[-1])
                current_high = float(high.iloc[-1])
                current_low = float(low.iloc[-1])

                # ── SMA-200 uptrend filter ──────────────────────────────────
                if self.sma200_filter:
                    sma200 = close.rolling(200).mean()
                    if pd.isna(sma200.iloc[-1]) or current_close <= sma200.iloc[-1]:
                        continue

                # ── Short-term trend filter ─────────────────────────────────
                sma_trend = close.rolling(self.sma_trend_period).mean()
                if pd.isna(sma_trend.iloc[-1]) or current_close <= float(sma_trend.iloc[-1]):
                    continue  # Price must be above short-term SMA

                # ── RSI filter ──────────────────────────────────────────────
                rsi = calc_rsi(close, period=self.rsi_period)
                if rsi is None or pd.isna(rsi.iloc[-1]):
                    continue
                current_rsi = float(rsi.iloc[-1])
                if current_rsi < self.rsi_min or current_rsi > self.rsi_max:
                    continue

                # ── IBS (Intraday Breadth Strength) filter ──────────────────
                # IBS = (close - low) / (high - low)
                # High IBS means closed near day's high = buying pressure
                day_range = current_high - current_low
                if day_range <= 0:
                    continue
                ibs = (current_close - current_low) / day_range
                if ibs < self.ibs_min:
                    continue

                # ── Recent momentum filter ──────────────────────────────────
                if len(close) < self.momentum_period + 2:
                    continue
                past_close = float(close.iloc[-(self.momentum_period + 1)])
                if past_close <= 0:
                    continue
                momentum_return = (current_close - past_close) / past_close
                if momentum_return < self.momentum_min:
                    continue

                # ── Volume filter ────────────────────────────────────────────
                vol_ratio = calc_volume_ratio(volume, self.vol_lookback)
                if vol_ratio.iloc[-1] < self.vol_min_ratio:
                    continue

                # ── ATR-based stop and position sizing ──────────────────────
                atr = calc_atr(high, low, close, self.atr_period)
                atr_val = float(atr.iloc[-1])
                if atr_val <= 0 or np.isnan(atr_val):
                    continue

                entry_price = current_close
                stop_price = entry_price - self.atr_stop_mult * atr_val

                if stop_price <= 0 or stop_price >= entry_price:
                    continue

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
                shares = pos["shares"]
                if shares <= 0:
                    continue

                # ── Confidence scoring ──────────────────────────────────────
                # Base: 0.65
                # Bonus: higher RSI (more momentum) within our range
                rsi_score = (current_rsi - self.rsi_min) / max(
                    self.rsi_max - self.rsi_min, 1
                )
                rsi_bonus = rsi_score * 0.10  # up to +0.10
                # Bonus: higher IBS (stronger closing position)
                ibs_bonus = min((ibs - self.ibs_min) * 0.15, 0.10)
                # Bonus: stronger recent momentum
                momentum_bonus = min(momentum_return * 5.0, 0.08)
                # Volume above average adds confidence
                vol_bonus = min((float(vol_ratio.iloc[-1]) - 1.0) * 0.03, 0.05) if float(vol_ratio.iloc[-1]) > 1.0 else 0.0

                confidence = min(
                    0.90,
                    0.65 + rsi_bonus + ibs_bonus + momentum_bonus + vol_bonus,
                )

                rationale = (
                    f"{ticker}: overnight return setup — "
                    f"close={current_close:.2f} > SMA{self.sma_trend_period}={sma_trend.iloc[-1]:.2f}, "
                    f"RSI={current_rsi:.1f}, IBS={ibs:.2f}, "
                    f"{self.momentum_period}d_ret={momentum_return*100:.1f}%, "
                    f"vol_ratio={vol_ratio.iloc[-1]:.2f}"
                )

                signals.append(Signal(
                    ticker=ticker,
                    strategy=self.name,
                    direction="long",
                    entry_price=round(entry_price, 2),
                    stop_price=round(stop_price, 2),
                    take_profit=None,
                    position_size=shares,
                    position_value=round(pos["position_value"], 2),
                    risk_amount=round(pos["total_risk"], 2),
                    confidence=round(confidence, 3),
                    rationale=rationale,
                    features={
                        "rsi": round(current_rsi, 2),
                        "ibs": round(ibs, 3),
                        "momentum_pct": round(momentum_return * 100, 2),
                        "vol_ratio": round(float(vol_ratio.iloc[-1]), 3),
                        "atr": round(atr_val, 3),
                        "sma_trend": round(float(sma_trend.iloc[-1]), 2),
                    },
                    market_id=self.config.get("market", "sp500"),
                ))

            except Exception as e:
                self._logger.warning(f"{ticker}: signal generation failed: {e}")
                continue

        # Sort by composite momentum score (IBS × RSI momentum)
        signals.sort(key=lambda s: s.confidence, reverse=True)
        self._logger.info(
            f"OvernightReturn: {len(signals)} signals from {len(data)} tickers"
        )
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check exit conditions for held overnight_return positions.

        Exit rules (priority order):
          1. Stop hit: price <= stop_price
          2. Time exit: held >= max_hold_days (primary exit for this strategy —
             we hold 1-2 overnight periods then sell)
          3. Trend reversal: price drops below short-term SMA (momentum lost)
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

            current_price = float(df["close"].iloc[-1])
            stop_price = pos.get("stop_price", 0.0)
            entry_date = pos.get("entry_date")

            # Days held
            days_held = 0
            if entry_date:
                if isinstance(entry_date, str):
                    entry_date = pd.Timestamp(entry_date)
                days_held = (df.index[-1] - pd.Timestamp(entry_date)).days

            reason = None
            details = None

            # 1. Stop hit
            if stop_price and current_price <= stop_price:
                reason = "stop_hit"
                details = (
                    f"{ticker} stop hit: {current_price:.2f} <= {stop_price:.2f}, "
                    f"held {days_held}d"
                )

            # 2. Time exit (primary for overnight strategy)
            elif days_held >= self.max_hold_days:
                reason = "time_exit"
                details = (
                    f"{ticker} overnight hold complete: "
                    f"held {days_held}d >= max {self.max_hold_days}"
                )

            # 3. Trend reversal: price drops below short-term SMA
            elif len(df) >= self.sma_trend_period + 5:
                close = df["close"]
                sma_trend = close.rolling(self.sma_trend_period).mean()
                sma_val = float(sma_trend.iloc[-1])
                if not pd.isna(sma_val) and current_price < sma_val:
                    reason = "signal_exit"
                    details = (
                        f"{ticker} trend lost: {current_price:.2f} < "
                        f"SMA{self.sma_trend_period} {sma_val:.2f}, held {days_held}d"
                    )

            if reason:
                exits.append({
                    "ticker": ticker,
                    "reason": reason,
                    "exit_price": current_price,
                    "details": details or reason,
                })

        self._logger.debug(
            f"OvernightReturn: {len(exits)} exits from {len(positions)} positions"
        )
        return exits


# Default parameter grid for optimization
PARAM_GRID = {
    "sma_trend_period": [10, 15, 20, 30],
    "rsi_min": [40, 45, 50],
    "rsi_max": [65, 70, 75],
    "ibs_min": [0.4, 0.5, 0.6, 0.7],
    "momentum_period": [3, 5, 7, 10],
    "vol_min_ratio": [0.5, 0.8, 1.0],
    "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
    "max_hold_days": [1, 2, 3, 5],
    "sma200_filter": [True, False],
}
