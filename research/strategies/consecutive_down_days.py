"""
Atlas Consecutive Down Days Strategy (Wave 5 — Sandbox)
========================================================
Mean reversion strategy that buys stocks after N consecutive down closes,
designed specifically for individual large-cap stocks (not ETF adaptation).

Published research basis:
  - Larry Connors "Short-Term Trading Strategies That Work" (2008)
  - Quantpedia: Short-term reversal effect in large-cap stocks (Sharpe 1.09)
  - Groot, Huij, Zhou (2012): "Another Look at Trading Costs and Short-Term
    Reversal Profits" — 30-50 bps/week net of costs on large-caps
  - Key insight: buying recent losers among large-cap stocks captures
    liquidity provision premium + overreaction correction

Core logic:
  1. Count consecutive down closes (close < prev close)
  2. Buy when count >= min_down_days AND price > SMA-200 (uptrend)
  3. Additional filter: IBS < ibs_threshold (selling exhaustion)
  4. Exit when close > previous day's high (strength confirmation)
  5. Fallback: time-based exit after max_hold_days
  6. Stop loss: ATR-based protective stop

Key difference from existing mean_reversion:
  - mean_reversion: RSI(14) < 35 + z-score < -2.0 entry
  - consecutive_down_days: N consecutive red candles entry
  - Different signal source → expected to be uncorrelated
  - Simpler signal → fewer parameters → lower overfit risk

Adaptation notes:
  - NOT adapted from ETF strategy — Connors' work covers individual stocks
  - Large-cap stocks in SP500 top-200 match the academic literature universe
  - SMA-200 filter aligns with Wave 1 findings (proven win)
  - Strength exit (close > prev high) borrowed from LBR research

Config Section: strategies.consecutive_down_days

Usage:
    from research.strategies.consecutive_down_days import ConsecutiveDownDays
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_ibs, calc_position_size, calc_volume_ratio

logger = logging.getLogger(__name__)


class ConsecutiveDownDays(BaseStrategy):
    """Buy stocks after N consecutive down closes in uptrending large-caps."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("consecutive_down_days", {})

        # Core signal parameters
        self.min_down_days = strat_cfg.get("min_down_days", 3)        # Min consecutive closes
        self.max_down_days = strat_cfg.get("max_down_days", 8)        # Ignore >8 (trend collapse)

        # Trend filter
        self.sma200_filter = strat_cfg.get("sma200_filter", True)     # Only buy in uptrends

        # IBS exhaustion filter (optional)
        self.ibs_threshold = strat_cfg.get("ibs_threshold", 0.3)      # Low IBS = selling exhaustion
        self.ibs_enabled = strat_cfg.get("ibs_enabled", True)

        # Risk management
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)

        # Exit rules
        self.exit_rule = strat_cfg.get("exit_rule", "strength")       # "strength" or "sma"
        self.sma_exit_period = strat_cfg.get("sma_exit_period", 5)    # SMA exit period (if rule=sma)
        self.max_hold_days = strat_cfg.get("max_hold_days", 5)        # Forced exit

        # Profit target (optional, 0 = disabled)
        self.profit_target_atr_mult = strat_cfg.get("profit_target_atr_mult", 0.0)

        # Volume filter
        vol_cfg = strat_cfg.get("volume", {})
        self.vol_lookback = vol_cfg.get("lookback", 20)
        self.vol_min_ratio = vol_cfg.get("min_ratio", 0.5)

        self._logger.info(
            f"ConsecutiveDownDays initialized: min_down={self.min_down_days}, "
            f"sma200={'ON' if self.sma200_filter else 'OFF'}, "
            f"ibs_thresh={self.ibs_threshold if self.ibs_enabled else 'OFF'}, "
            f"exit={self.exit_rule}, max_hold={self.max_hold_days}"
        )

    @property
    def name(self) -> str:
        return "consecutive_down_days"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Scan all tickers for consecutive down day entry signals.

        A signal is generated when:
            1. Stock has min_down_days consecutive down closes
            2. Price is above SMA-200 (uptrend, if enabled)
            3. IBS < ibs_threshold (selling exhaustion, if enabled)
            4. Volume is adequate (not a dry, illiquid move)
        """
        signals: List[Signal] = []
        held_tickers = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0008)
        min_position_value = self.fees_config.get("min_position_value", 0.0)
        max_position_value = self.config.get("trading", {}).get("live_safety", {}).get("max_order_value", 0.0)

        # Minimum rows needed for indicators
        min_rows = max(200 + 10, self.atr_period + 10, self.vol_lookback + 10)

        for ticker, df in data.items():
            try:
                if ticker in held_tickers:
                    continue

                if not self._can_open_position(existing_positions):
                    self._logger.debug("Max positions reached, skipping remaining tickers")
                    break

                if not self._has_sufficient_data(df, min_rows):
                    continue

                close = df["close"]
                high = df["high"]
                low = df["low"]
                volume = df["volume"]

                # ── Count consecutive down closes ──
                down = close < close.shift(1)
                # Count run length: for each bar, how many consecutive True values ending here
                consec_down = down.astype(int)
                # Use cumsum trick: group by runs, count within each run
                reset = (~down).cumsum()
                consec_count = down.groupby(reset).cumsum()

                current_consec = int(consec_count.iloc[-1])

                if current_consec < self.min_down_days:
                    continue
                if current_consec > self.max_down_days:
                    self._logger.debug(f"{ticker}: {current_consec} down days > max {self.max_down_days}, skipping (trend collapse)")
                    continue

                # ── SMA-200 trend filter ──
                if self.sma200_filter:
                    sma200 = close.rolling(200).mean()
                    if close.iloc[-1] <= sma200.iloc[-1]:
                        continue

                # ── IBS exhaustion filter ──
                if self.ibs_enabled:
                    ibs = calc_ibs(high, low, close)
                    if ibs.iloc[-1] >= self.ibs_threshold:
                        continue

                # ── Volume adequacy ──
                vol_ratio = calc_volume_ratio(volume, self.vol_lookback)
                if vol_ratio.iloc[-1] < self.vol_min_ratio:
                    continue

                # ── Calculate entry, stop, and position size ──
                entry_price = close.iloc[-1]
                atr = calc_atr(high, low, close, self.atr_period)
                atr_val = atr.iloc[-1]

                if atr_val <= 0 or np.isnan(atr_val):
                    continue

                stop_price = entry_price - self.atr_stop_mult * atr_val
                if stop_price <= 0 or stop_price >= entry_price:
                    continue

                # Take profit (if enabled)
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
                # Higher confidence for more consecutive down days and lower IBS
                base_conf = 0.70
                # Bonus for each extra down day beyond minimum
                extra_days = min(current_consec - self.min_down_days, 3)
                day_bonus = extra_days * 0.05  # +0.05 per extra day, max +0.15
                # IBS bonus (lower IBS = more exhausted)
                ibs_val = calc_ibs(high, low, close).iloc[-1] if self.ibs_enabled else 0.5
                ibs_bonus = max(0, (self.ibs_threshold - ibs_val) * 0.2)
                confidence = min(0.95, base_conf + day_bonus + ibs_bonus)

                rationale = (
                    f"{ticker}: {current_consec} consecutive down closes, "
                    f"price={entry_price:.2f}, "
                    f"{'above SMA-200, ' if self.sma200_filter else ''}"
                    f"IBS={ibs_val:.2f}, vol_ratio={vol_ratio.iloc[-1]:.2f}"
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
                    risk_amount=round(pos_result["risk_amount"], 2),
                    confidence=round(confidence, 3),
                    rationale=rationale,
                    features={
                        "consecutive_down": current_consec,
                        "ibs": round(ibs_val, 3) if self.ibs_enabled else None,
                        "atr": round(atr_val, 3),
                        "vol_ratio": round(vol_ratio.iloc[-1], 3),
                    },
                    market_id=self.market_id,
                ))

            except Exception as e:
                self._logger.warning(f"{ticker}: signal generation failed: {e}")
                continue

        self._logger.info(f"ConsecutiveDownDays: {len(signals)} signals from {len(data)} tickers")
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check exit conditions for held consecutive_down_days positions.

        Exit rules (in priority order):
          1. Stop hit: current price <= stop_price
          2. Profit target: price >= take_profit (if profit_target_atr_mult > 0)
          3. Time exit: held >= max_hold_days
          4. Strength exit (exit_rule == "strength"): close > previous close
          5. SMA exit (exit_rule == "sma"): close > SMA(sma_exit_period)

        Returns:
            List of dicts with keys: ticker, reason, exit_price, details.
        """
        exits: List[Dict[str, Any]] = []

        for pos in positions:
            if pos.get("strategy") != self.name:
                continue

            ticker = pos.get("ticker")
            if not ticker or ticker not in data:
                continue

            df = data[ticker]
            if len(df) < 2:
                continue

            current_close = df["close"].iloc[-1]
            prev_close = df["close"].iloc[-2]
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

            # 1. Stop loss hit
            if current_close <= stop_price:
                reason = "stop_hit"
                details = (
                    f"{ticker} hit stop: close {current_close:.2f} <= stop {stop_price:.2f}, "
                    f"held {days_held} days"
                )

            # 2. Profit target hit
            elif (
                self.profit_target_atr_mult > 0
                and take_profit is not None
                and current_close >= take_profit
            ):
                reason = "take_profit"
                details = (
                    f"{ticker} take profit: close {current_close:.2f} >= target {take_profit:.2f}, "
                    f"held {days_held} days"
                )

            # 3. Time exit
            elif days_held >= self.max_hold_days:
                reason = "time_exit"
                details = f"{ticker} time exit: held {days_held} days >= max {self.max_hold_days}"

            # 4. Strength exit: bounce completed — close > previous close
            elif self.exit_rule == "strength" and current_close > prev_close:
                reason = "signal_exit"
                details = (
                    f"{ticker} strength exit: close {current_close:.2f} > prev_close {prev_close:.2f}, "
                    f"held {days_held} days"
                )

            # 5. SMA exit: close crossed above short SMA
            elif self.exit_rule == "sma":
                sma = df["close"].rolling(self.sma_exit_period).mean()
                sma_val = sma.iloc[-1]
                if not pd.isna(sma_val) and current_close > sma_val:
                    reason = "signal_exit"
                    details = (
                        f"{ticker} SMA exit: close {current_close:.2f} > SMA({self.sma_exit_period}) "
                        f"{sma_val:.2f}, held {days_held} days"
                    )

            if reason:
                exits.append({
                    "ticker": ticker,
                    "reason": reason,
                    "exit_price": current_close,
                    "details": details or reason,
                })

        self._logger.debug(f"ConsecutiveDownDays: {len(exits)} exit signals from {len(positions)} positions")
        return exits

    def get_exit_signals(
        self,
        positions: List[Dict[str, Any]],
        data: Dict[str, pd.DataFrame],
    ) -> List[Dict[str, Any]]:
        """Generate exit signals for held positions.

        Exit rules:
          1. Stop loss hit (price <= stop_price)
          2. Strength exit: close > previous day's high
          3. Time-based exit after max_hold_days
          4. Profit target hit (if enabled)
        """
        exits = []
        for pos in positions:
            if pos.get("strategy") != self.name:
                continue

            ticker = pos["ticker"]
            if ticker not in data:
                continue

            df = data[ticker]
            if len(df) < 2:
                continue

            current_close = df["close"].iloc[-1]
            current_low = df["low"].iloc[-1]
            prev_high = df["high"].iloc[-2]  # Use T-1 high (no look-ahead)
            entry_price = pos.get("entry_price", current_close)
            stop_price = pos.get("stop_price", 0)

            reason = None

            # 1. Stop loss
            if current_low <= stop_price:
                reason = f"stop_loss: low {current_low:.2f} <= stop {stop_price:.2f}"

            # 2. Strength exit: close > previous day's high
            elif self.exit_rule == "strength" and current_close > prev_high:
                reason = f"strength_exit: close {current_close:.2f} > prev_high {prev_high:.2f}"

            # 3. SMA exit (alternative)
            elif self.exit_rule == "sma":
                sma = df["close"].rolling(self.sma_exit_period).mean()
                if current_close > sma.iloc[-1]:
                    reason = f"sma_exit: close {current_close:.2f} > SMA({self.sma_exit_period}) {sma.iloc[-1]:.2f}"

            # 4. Profit target
            elif self.profit_target_atr_mult > 0:
                take_profit = pos.get("take_profit")
                if take_profit and current_close >= take_profit:
                    reason = f"profit_target: close {current_close:.2f} >= target {take_profit:.2f}"

            # 5. Time-based exit
            if reason is None:
                entry_date = pos.get("entry_date")
                if entry_date:
                    if isinstance(entry_date, str):
                        entry_date = datetime.fromisoformat(entry_date)
                    hold_days = (df.index[-1] - pd.Timestamp(entry_date)).days
                    if hold_days >= self.max_hold_days:
                        reason = f"time_exit: held {hold_days} days >= max {self.max_hold_days}"

            if reason:
                exits.append({
                    "ticker": ticker,
                    "strategy": self.name,
                    "exit_price": current_close,
                    "reason": reason,
                })

        return exits
