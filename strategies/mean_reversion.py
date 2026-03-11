"""
Atlas Mean Reversion Strategy (Phase 7A)
==================================
Generates long signals when a stock is statistically oversold,
expecting a reversion to the mean.

Entry Conditions:
    - RSI < rsi_oversold (default 30)
    - Z-score < zscore_entry (default -2.0)

Stop Loss:
    - Entry price - atr_stop_mult * ATR(atr_period)

Take Profit:
    - Entry price + profit_target_atr_mult * ATR(atr_period)

Exit Conditions:
    - Price reverts to 20-day moving average
    - Take profit hit
    - Time-based exit after max_hold_days

Config Section: strategies.mean_reversion

Usage:
    from strategies.mean_reversion import MeanReversion
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_zscore, calc_position_size, calc_volume_ratio, calc_ibs
from utils.earnings import is_near_earnings

logger = logging.getLogger(__name__)


class MeanReversion(BaseStrategy):
    """Mean reversion strategy: buy oversold stocks expecting reversion to mean."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("mean_reversion", {})
        self.rsi_period = strat_cfg.get("rsi_period", 14)
        self.rsi_oversold = strat_cfg.get("rsi_oversold", 30)
        self.zscore_lookback = strat_cfg.get("zscore_lookback", 20)
        self.zscore_entry = strat_cfg.get("zscore_entry", -2.0)
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)
        self.profit_target_atr_mult = strat_cfg.get("profit_target_atr_mult", 1.5)
        self.max_hold_days = strat_cfg.get("max_hold_days", 5)
        # Phase 7A: Volume confirmation parameters
        vol_cfg = strat_cfg.get("volume", {})
        self.vol_lookback = vol_cfg.get("lookback", 20)
        self.vol_min_ratio = vol_cfg.get("min_ratio", 0.5)
        self.vol_surge_threshold = vol_cfg.get("surge_threshold", 2.0)
        self.vol_surge_boost = vol_cfg.get("surge_boost", 0.1)
        self.vol_dry_penalty = vol_cfg.get("dry_penalty", 0.15)
        # Hard volume entry gate: skip entry if volume < threshold * avg (0 = disabled)
        self.volume_entry_min = strat_cfg.get("volume_entry_min", 0.0)
        # Phase 7A: Earnings blackout parameters
        earnings_cfg = strat_cfg.get("earnings_blackout", {})
        self.earnings_blackout_enabled = earnings_cfg.get("enabled", True)
        self.earnings_blackout_before = earnings_cfg.get("days_before", 5)
        self.earnings_blackout_after = earnings_cfg.get("days_after", 1)
        # US-optimization: SMA-200 trend filter (only buy in uptrends)
        self.sma200_filter = strat_cfg.get("sma200_filter", False)
        # US-optimization: IBS confirmation filter (low IBS = selling exhaustion)
        self.ibs_max = strat_cfg.get("ibs_max", 1.0)  # 1.0 = disabled
        self._precomputed = False
        self._logger.info(
            f"MeanReversion initialized: rsi_period={self.rsi_period}, "
            f"rsi_oversold={self.rsi_oversold}, "
            f"zscore_entry={self.zscore_entry}, profit_target={self.profit_target_atr_mult}x ATR, "
            f"vol_surge={self.vol_surge_threshold}x, "
            f"sma200_filter={'ON' if self.sma200_filter else 'OFF'}, "
            f"ibs_max={self.ibs_max}, "
            f"earnings_blackout={'ON' if self.earnings_blackout_enabled else 'OFF'}"
        )

    @property
    def name(self) -> str:
        return "mean_reversion"

    def precompute(self, data: Dict[str, pd.DataFrame]) -> None:
        """Pre-compute all indicator columns once before the walk-forward loop."""
        for ticker, df in data.items():
            close = df["close"]
            high = df["high"]
            low = df["low"]
            volume = df["volume"]
            df["_mr_rsi"] = calc_rsi(close, period=self.rsi_period)
            df["_mr_zscore"] = calc_zscore(close, lookback=self.zscore_lookback)
            df["_mr_atr"] = calc_atr(high, low, close, period=self.atr_period)
            df["_mr_vol_ratio"] = calc_volume_ratio(volume, lookback=self.vol_lookback)
            df["_mr_mean_target"] = close.rolling(self.zscore_lookback).mean()
            if self.sma200_filter:
                df["_mr_sma200"] = close.rolling(200).mean()
            if self.ibs_max < 1.0:
                df["_mr_ibs"] = calc_ibs(high, low, close)
        self._precomputed = True

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Scan all tickers for mean reversion entry signals.

        A signal is generated when:
            1. RSI(rsi_period) < rsi_oversold
            2. Z-score(zscore_lookback) < zscore_entry
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

        # Minimum rows needed
        min_rows = max(self.rsi_period, self.zscore_lookback, self.atr_period) + 10

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

                # Calculate indicators
                if self._precomputed:
                    current_rsi = df["_mr_rsi"].iloc[-1]
                    current_zscore = df["_mr_zscore"].iloc[-1]
                else:
                    rsi = calc_rsi(close, period=self.rsi_period)
                    zscore = calc_zscore(close, lookback=self.zscore_lookback)
                    current_rsi = rsi.iloc[-1]
                    current_zscore = zscore.iloc[-1]

                if pd.isna(current_rsi) or pd.isna(current_zscore):
                    continue

                # Entry conditions: RSI oversold AND z-score extreme
                is_rsi_oversold = current_rsi < self.rsi_oversold
                is_zscore_extreme = current_zscore < self.zscore_entry

                if not (is_rsi_oversold and is_zscore_extreme):
                    continue

                # SMA-200 trend filter: only buy if price is above 200-day SMA
                if self.sma200_filter:
                    if self._precomputed:
                        sma200_val = df["_mr_sma200"].iloc[-1]
                    else:
                        sma200_val = close.rolling(200).mean().iloc[-1]
                    if pd.isna(sma200_val) or close.iloc[-1] < sma200_val:
                        self._logger.debug(
                            f"{ticker}: below SMA(200) "
                            f"(close={close.iloc[-1]:.2f}, sma200={sma200_val if not pd.isna(sma200_val) else 'N/A'}), skipping"
                        )
                        continue

                # IBS confirmation filter: only buy if IBS is low (selling exhaustion)
                if self.ibs_max < 1.0:
                    if self._precomputed:
                        current_ibs = df["_mr_ibs"].iloc[-1]
                    else:
                        ibs = calc_ibs(high, low, close)
                        current_ibs = ibs.iloc[-1]
                    if pd.isna(current_ibs) or current_ibs > self.ibs_max:
                        self._logger.debug(
                            f"{ticker}: IBS={current_ibs:.3f} > max {self.ibs_max}, skipping"
                        )
                        continue

                # Phase 7A: Earnings blackout check
                if self.earnings_blackout_enabled:
                    # Use the last date in the dataframe as reference
                    reference_date = df.index[-1]
                    try:
                        if is_near_earnings(
                            ticker,
                            reference_date=reference_date,
                            blackout_days_before=self.earnings_blackout_before,
                            blackout_days_after=self.earnings_blackout_after,
                        ):
                            self._logger.debug(
                                f"{ticker}: within earnings blackout window, skipping"
                            )
                            continue
                    except Exception as e:
                        # Gracefully degrade - if earnings data unavailable, skip filter
                        self._logger.debug(f"{ticker}: earnings check failed ({e}), proceeding")

                # Phase 7A: Volume confirmation
                if self._precomputed:
                    current_vol_ratio = df["_mr_vol_ratio"].iloc[-1]
                else:
                    vol_ratio = calc_volume_ratio(volume, lookback=self.vol_lookback)
                    current_vol_ratio = vol_ratio.iloc[-1]

                if pd.isna(current_vol_ratio):
                    current_vol_ratio = 1.0  # Neutral if no data

                # Hard volume gate: skip entry if volume below threshold
                if self.volume_entry_min > 0 and current_vol_ratio < self.volume_entry_min:
                    self._logger.debug(
                        f"{ticker}: volume {current_vol_ratio:.2f}x < {self.volume_entry_min}x min, skipping"
                    )
                    continue

                # Phase 7A: Volume noted for confidence adjustment

                # Calculate ATR
                if self._precomputed:
                    current_atr = df["_mr_atr"].iloc[-1]
                else:
                    atr = calc_atr(high, low, close, period=self.atr_period)
                    current_atr = atr.iloc[-1]

                if pd.isna(current_atr) or current_atr <= 0:
                    self._logger.debug(f"{ticker}: invalid ATR ({current_atr})")
                    continue

                today_close = close.iloc[-1]
                entry_price = today_close

                # Stop loss: entry - atr_stop_mult * ATR
                stop_price = entry_price - (self.atr_stop_mult * current_atr)
                if stop_price <= 0:
                    self._logger.debug(f"{ticker}: stop price <= 0, skipping")
                    continue

                # Take profit: entry + profit_target_atr_mult * ATR
                take_profit = entry_price + (self.profit_target_atr_mult * current_atr)

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

                # 20-day mean for reference
                if self._precomputed:
                    mean_20 = df["_mr_mean_target"].iloc[-1]
                else:
                    mean_20 = close.iloc[-self.zscore_lookback:].mean()

                # Confidence: base 0.6 for meeting entry criteria + bonuses for depth
                # RSI bonus: saturates when RSI is 15 below oversold threshold
                rsi_bonus = min(1.0, max(0, (self.rsi_oversold - current_rsi) / 15.0))
                # Z-score bonus: saturates when 1.0 std beyond entry threshold
                zscore_bonus = min(1.0, max(0, (abs(current_zscore) - abs(self.zscore_entry)) / 1.0))

                confidence = min(1.0, 0.6 + 0.2 * rsi_bonus + 0.2 * zscore_bonus)

                # Phase 3: Volume Spike Confirmation — activate the vol_surge_boost/dry_penalty
                # High volume on dip = capitulation = stronger reversal conviction
                # Low volume on dip = weak selling = less reliable setup
                _vol_conf_adj = 0.0
                if current_vol_ratio >= self.vol_surge_threshold:
                    _vol_conf_adj = self.vol_surge_boost
                    vol_note = f"Volume surge {current_vol_ratio:.1f}x avg (capitulation +conf). "
                elif current_vol_ratio < self.vol_min_ratio:
                    _vol_conf_adj = -self.vol_dry_penalty
                    vol_note = f"Volume low {current_vol_ratio:.1f}x avg (weak dip -conf). "
                else:
                    vol_note = f"Volume {current_vol_ratio:.1f}x avg. "
                if _vol_conf_adj != 0.0:
                    confidence = min(1.0, max(0.0, confidence + _vol_conf_adj))
                    self._logger.debug(
                        f"{ticker}: vol_adj={_vol_conf_adj:+.2f} (rvol={current_vol_ratio:.1f}x) "
                        f"→ conf={confidence:.3f}"
                    )

                # Rationale
                rationale = (
                    f"{ticker} is oversold with RSI={current_rsi:.1f} (threshold {self.rsi_oversold}) "
                    f"and Z-score={current_zscore:.2f} (threshold {self.zscore_entry}). "
                    f"Price ${today_close:.2f} is {abs(current_zscore):.1f} std devs below "
                    f"20-day mean of ${mean_20:.2f}. "
                    f"{vol_note}"
                    f"Target reversion to ${take_profit:.2f} (+{self.profit_target_atr_mult}x ATR), "
                    f"stop at ${stop_price:.2f}."
                )

                signal = Signal(
                    ticker=ticker,
                    strategy=self.name,
                    direction="long",
                    entry_price=entry_price,
                    stop_price=round(stop_price, 4),
                    take_profit=round(take_profit, 4),
                    position_size=pos["shares"],
                    position_value=pos["position_value"],
                    risk_amount=pos["total_risk"],
                    confidence=round(confidence, 4),
                    rationale=rationale,
                    features={
                        "rsi": round(current_rsi, 2),
                        "zscore": round(current_zscore, 4),
                        "mean_20": round(mean_20, 4),
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

        self._logger.info(f"MeanReversion generated {len(signals)} signals")
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check open mean reversion positions for exit conditions.

        Exit conditions:
            1. Hard stop: price drops below original stop_price
            2. Take profit: price reaches or exceeds take_profit level
            3. Mean reversion: price reverts to 20-day moving average
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
                today_close = close.iloc[-1]
                today_date = df.index[-1]

                entry_date = pd.Timestamp(pos["entry_date"])
                entry_price = pos["entry_price"]
                stop_price = pos.get("stop_price", 0)
                take_profit = pos.get("take_profit")

                # Days held
                days_held = (today_date - entry_date).days

                # 20-day moving average (mean reversion target)
                if self._precomputed:
                    mean_20 = df["_mr_mean_target"].iloc[-1]
                else:
                    mean_20 = close.iloc[-self.zscore_lookback:].mean()

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
                # 2. Take profit hit
                elif take_profit is not None and today_close >= take_profit:
                    exits.append({
                        "ticker": ticker,
                        "reason": "take_profit",
                        "exit_price": today_close,
                        "details": (
                            f"{ticker} hit take profit at ${take_profit:.2f}. "
                            f"Close=${today_close:.2f}, entry=${entry_price:.2f}, "
                            f"held {days_held} days."
                        ),
                    })
                # 3. Mean reversion: price reverted to 20-day mean
                elif today_close >= mean_20 and entry_price < mean_20:
                    exits.append({
                        "ticker": ticker,
                        "reason": "signal_exit",
                        "exit_price": today_close,
                        "details": (
                            f"{ticker} reverted to 20-day mean (${mean_20:.2f}). "
                            f"Close=${today_close:.2f}, entry=${entry_price:.2f}, "
                            f"held {days_held} days."
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
