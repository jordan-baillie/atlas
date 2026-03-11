"""
Atlas Relative Strength Pullback Strategy
========================================
Stocks with relative strength rank > 80th percentile that pull back to 10-EMA. Enter on bounce. Exit: trailing stop.

Reference: O'Neil CANSLIM (1988), Minervini 'Trade Like a Stock Market Wizard'
Generated: 2026-03-10T07:18:31.218111+00:00

Config Section: strategies.relative_strength_pullback
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size

logger = logging.getLogger(__name__)


class RelativeStrengthPullback(BaseStrategy):
    """Stocks with relative strength rank > 80th percentile that pull back to 20-day SMA."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("relative_strength_pullback", {})

        # RS ranking parameters
        self.rs_lookback = strat_cfg.get("rs_lookback", 120)            # 6-month RS window
        self.rs_top_pct = strat_cfg.get("rs_top_pct", 0.20)             # Top 20% RS rank

        # Pullback / support detection
        self.sma_period = strat_cfg.get("sma_period", 20)               # 20-day SMA as support
        self.pullback_max_pct = strat_cfg.get("pullback_max_pct", 0.05) # Price within 5% of SMA
        self.bounce_min_pct = strat_cfg.get("bounce_min_pct", 0.002)    # Close must clear SMA by 0.2%

        # Volume confirmation
        self.vol_lookback = strat_cfg.get("vol_lookback", 20)
        self.vol_min_ratio = strat_cfg.get("vol_min_ratio", 0.8)        # Modest volume (not panic)

        # Risk management
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)
        self.profit_target_atr_mult = strat_cfg.get("profit_target_atr_mult", 2.5)
        self.max_hold_days = strat_cfg.get("max_hold_days", 10)
        self.sma200_filter = strat_cfg.get("sma200_filter", True)

        self._logger.info(
            f"RelativeStrengthPullback initialized: rs_top={self.rs_top_pct:.0%}, "
            f"sma={self.sma_period}, pullback<{self.pullback_max_pct:.0%}, "
            f"atr_stop={self.atr_stop_mult}x, max_hold={self.max_hold_days}d"
        )

    @property
    def name(self) -> str:
        return "relative_strength_pullback"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Generate relative_strength_pullback entry signals.

        Entry criteria:
          1. Ticker is in the top rs_top_pct of all tickers by RS (ROC over rs_lookback)
          2. Price pulled back to within pullback_max_pct of 20-day SMA (touching support)
          3. Price just bounced: close is above SMA (confirmed support hold)
          4. Optional SMA-200 trend filter
          5. Volume is healthy (not a panic flush)
        """
        signals: List[Signal] = []
        held = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0008)

        min_rows = max(
            200 if self.sma200_filter else 0,
            self.rs_lookback + 5,
            self.sma_period + 5,
            self.atr_period + 5,
        )

        # Step 1: Compute RS ranks across ALL eligible tickers
        roc_scores: Dict[str, float] = {}
        for ticker, df in data.items():
            if len(df) >= self.rs_lookback + 5:
                close = df["close"]
                try:
                    roc = (float(close.iloc[-1]) - float(close.iloc[-self.rs_lookback - 1])) / float(close.iloc[-self.rs_lookback - 1])
                    roc_scores[ticker] = roc
                except Exception:
                    continue

        if not roc_scores:
            self._logger.info(f"{self.name}: 0 signals — no RS data available")
            return signals

        all_rocs = list(roc_scores.values())
        # Percentile cutoff: top rs_top_pct means above (1 - rs_top_pct) * 100th percentile
        rs_threshold = np.percentile(all_rocs, (1.0 - self.rs_top_pct) * 100)
        max_roc = max(all_rocs) if all_rocs else 1.0

        # Step 2: Filter and scan for pullback setup
        for ticker, df in data.items():
            try:
                if ticker in held:
                    continue
                if not self._can_open_position(existing_positions):
                    break
                if not self._has_sufficient_data(df, min_rows):
                    continue

                # RS filter: must be in top rs_top_pct
                if ticker not in roc_scores or roc_scores[ticker] < rs_threshold:
                    continue

                close = df["close"]
                high = df["high"]
                low = df["low"]
                volume = df["volume"]

                current_close = float(close.iloc[-1])

                # SMA-200 uptrend filter
                if self.sma200_filter:
                    sma200 = float(close.rolling(200).mean().iloc[-1])
                    if pd.isna(sma200) or current_close < sma200:
                        continue

                # 20-day SMA support level
                sma20_series = close.rolling(self.sma_period).mean()
                sma20_val = float(sma20_series.iloc[-1])
                if pd.isna(sma20_val):
                    continue

                # Pullback condition: price is near the SMA (within pullback_max_pct)
                distance_from_sma = (current_close - sma20_val) / sma20_val
                if distance_from_sma > self.pullback_max_pct:
                    continue  # Too far above SMA — not a pullback
                if distance_from_sma < -self.pullback_max_pct:
                    continue  # Breakdown below SMA — skip

                # Bounce confirmation: close must be at or above SMA (not still falling through)
                if current_close < sma20_val * (1.0 + self.bounce_min_pct):
                    # Allow if price is extremely close to SMA (about to bounce)
                    if distance_from_sma < 0:
                        # Still below SMA — not confirmed bounce yet
                        prev_close = float(close.iloc[-2])
                        sma20_prev = float(sma20_series.iloc[-2])
                        if not pd.isna(sma20_prev) and prev_close < sma20_prev:
                            continue  # Both yesterday and today below SMA — skip

                # Volume: adequate confirmation (not too thin)
                avg_vol = float(volume.rolling(self.vol_lookback).mean().iloc[-1])
                if pd.isna(avg_vol) or avg_vol <= 0:
                    continue
                vol_ratio = float(volume.iloc[-1]) / avg_vol
                if vol_ratio < self.vol_min_ratio:
                    continue

                # ATR and position sizing
                atr = calc_atr(high, low, close, self.atr_period)
                atr_val = float(atr.iloc[-1])
                if pd.isna(atr_val) or atr_val <= 0:
                    continue

                entry_price = current_close
                # Stop: ATR-based OR just below SMA20 (whichever is tighter = higher stop)
                atr_stop = entry_price - self.atr_stop_mult * atr_val
                sma_stop = sma20_val * 0.99  # 1% below SMA
                stop_price = max(atr_stop, sma_stop)

                if stop_price <= 0 or stop_price >= entry_price:
                    continue

                take_profit = entry_price + self.profit_target_atr_mult * atr_val

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

                # Confidence scoring
                denom = max_roc - rs_threshold + 1e-6
                rs_conf = min(0.15, (roc_scores[ticker] - rs_threshold) / denom * 0.15)
                vol_conf = min(0.10, (vol_ratio - self.vol_min_ratio) / 2.0 * 0.10)
                prox_conf = min(0.10, (1 - abs(distance_from_sma) / self.pullback_max_pct) * 0.10)
                confidence = round(min(0.95, 0.65 + rs_conf + vol_conf + prox_conf), 4)

                rationale = (
                    f"{ticker}: RS={roc_scores[ticker]:.1%} (top {self.rs_top_pct:.0%} threshold={rs_threshold:.1%}), "
                    f"pulled back to SMA{self.sma_period}={sma20_val:.2f} (dist={distance_from_sma:+.1%}), "
                    f"vol={vol_ratio:.1f}x. Entry={entry_price:.2f}, stop={stop_price:.2f}, target={take_profit:.2f}."
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
                        "rs_score": round(roc_scores[ticker], 4),
                        "rs_threshold": round(rs_threshold, 4),
                        "sma20": round(sma20_val, 4),
                        "dist_from_sma": round(distance_from_sma, 4),
                        "vol_ratio": round(vol_ratio, 2),
                        "atr": round(atr_val, 4),
                        "close": round(current_close, 4),
                    },
                    timestamp=datetime.now(),
                ))

            except Exception as e:
                self._logger.error(f"{ticker}: error in rs_pullback signal gen: {e}", exc_info=True)
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
          1. Stop-loss hit (ATR stop or 1% below SMA20)
          2. Take-profit hit
          3. Support breakdown: close drops significantly below SMA20
          4. Time exit
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

                # 3. Support breakdown: price breaks below SMA20 decisively
                close = df["close"]
                if len(close) >= self.sma_period:
                    sma20 = float(close.rolling(self.sma_period).mean().iloc[-1])
                    if not pd.isna(sma20) and current_price < sma20 * 0.98:
                        exits.append({
                            "ticker": ticker,
                            "reason": "signal_exit",
                            "exit_price": current_price,
                            "details": f"SMA{self.sma_period} support broken: {current_price:.2f} < {sma20:.2f}*0.98",
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
    "rs_lookback": [63, 120, 252],
    "rs_top_pct": [0.20, 0.30],
    "sma_period": [10, 20],
    "atr_stop_mult": [1.5, 2.0, 2.5],
    "profit_target_atr_mult": [2.0, 2.5, 3.0],
    "max_hold_days": [5, 10, 15, 20],
}
