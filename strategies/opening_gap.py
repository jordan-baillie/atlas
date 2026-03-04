"""
Atlas Opening Gap Reversal Strategy
========================================
Captures mean reversion after overnight gap-downs using daily OHLCV data.
When a stock gaps down significantly at open (today's open << yesterday's
close), it tends to recover over subsequent days. Identifies gap-down
stocks on Day T and enters at Day T+1 open for a multi-day recovery swing.

Config Section: strategies.opening_gap

Usage:
    from strategies.opening_gap import OpeningGap
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_ibs, calc_position_size, calc_rsi, calc_volume_ratio
from utils.earnings import is_near_earnings

logger = logging.getLogger(__name__)


class OpeningGap(BaseStrategy):
    """Opening gap reversal: enter when stock gaps down significantly and shows oversold confirmation."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("opening_gap", {})
        self.gap_threshold = strat_cfg.get("gap_threshold", -0.02)
        self.ibs_confirm = strat_cfg.get("ibs_confirm", 0.3)
        self.rsi14_max = strat_cfg.get("rsi14_max", 50)
        self.vol_surge_threshold = strat_cfg.get("vol_surge_threshold", 1.5)
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.5)
        self.sma_exit_period = strat_cfg.get("sma_exit_period", 5)
        self.ibs_exit_threshold = strat_cfg.get("ibs_exit_threshold", 0.8)
        self.max_hold_days = strat_cfg.get("max_hold_days", 7)
        # US-optimization: SMA-200 trend filter (only buy gap-downs in uptrends)
        self.sma200_filter = strat_cfg.get("sma200_filter", False)
        # Earnings blackout (gap-down after earnings may not mean revert)
        earnings_cfg = strat_cfg.get("earnings_blackout", {})
        self.earnings_blackout_enabled = earnings_cfg.get("enabled", False)
        self.earnings_blackout_before = earnings_cfg.get("days_before", 5)
        self.earnings_blackout_after = earnings_cfg.get("days_after", 1)

        self._logger.info(
            "OpeningGap initialized: gap_thresh=%.3f, ibs_confirm=%.2f, "
            "rsi14_max=%d, vol_surge=%.1fx, atr=%d, stop=%.1fx, "
            "sma_exit=%d, ibs_exit=%.2f, max_hold=%d, "
            "sma200_filter=%s, earnings_blackout=%s",
            self.gap_threshold, self.ibs_confirm, self.rsi14_max,
            self.vol_surge_threshold, self.atr_period,
            self.atr_stop_mult, self.sma_exit_period,
            self.ibs_exit_threshold, self.max_hold_days,
            'ON' if self.sma200_filter else 'OFF',
            'ON' if self.earnings_blackout_enabled else 'OFF',
        )

    @property
    def name(self) -> str:
        return "opening_gap"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Scan all tickers for gap-down reversal entry signals."""
        signals: List[Signal] = []
        held_tickers = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0008)
        min_position_value = self.fees_config.get("min_position_value", 0.0)
        max_position_value = self.config.get("trading", {}).get("live_safety", {}).get("max_order_value", 0.0)

        # Minimum rows needed for all indicators
        # RSI(14), ATR(14), SMA(5), volume_ratio(20) + buffer
        min_rows = 40

        for ticker, df in data.items():
            try:
                if ticker in held_tickers:
                    continue

                if not self._can_open_position(existing_positions):
                    self._logger.debug("Max positions reached, skipping remaining")
                    break

                if not self._has_sufficient_data(df, min_rows):
                    continue

                open_ = df["open"]
                high = df["high"]
                low = df["low"]
                close = df["close"]
                volume = df["volume"]

                today_open = float(open_.iloc[-1])
                today_close = float(close.iloc[-1])
                yesterday_close = float(close.iloc[-2])

                # Validate prices are positive and not NaN
                if (
                    pd.isna(today_open) or pd.isna(today_close)
                    or pd.isna(yesterday_close)
                    or yesterday_close <= 0 or today_open <= 0 or today_close <= 0
                ):
                    continue

                # --- Entry Condition 1: Gap detection ---
                gap_pct = (today_open - yesterday_close) / yesterday_close

                if gap_pct >= self.gap_threshold:
                    continue  # Not a significant gap down

                # SMA-200 trend filter: only buy gap-downs in uptrends
                if self.sma200_filter:
                    sma200 = close.rolling(200).mean()
                    sma200_val = sma200.iloc[-1]
                    if pd.isna(sma200_val) or today_close < sma200_val:
                        continue

                # Earnings blackout: skip gap-downs near earnings
                if self.earnings_blackout_enabled:
                    reference_date = df.index[-1]
                    try:
                        if is_near_earnings(
                            ticker,
                            reference_date=reference_date,
                            blackout_days_before=self.earnings_blackout_before,
                            blackout_days_after=self.earnings_blackout_after,
                        ):
                            continue
                    except Exception:
                        pass  # Gracefully degrade if earnings data unavailable

                # --- Entry Condition 2: Oversold confirmation ---
                ibs_series = calc_ibs(high, low, close)
                current_ibs = float(ibs_series.iloc[-1])

                if pd.isna(current_ibs):
                    continue

                # IBS < ibs_confirm OR bearish candle (close < open on gap day)
                ibs_oversold = current_ibs < self.ibs_confirm
                bearish_candle = today_close < today_open

                if not (ibs_oversold or bearish_candle):
                    self._logger.debug(
                        "%s: gap %.2f%% but no oversold confirmation "
                        "(IBS=%.2f, close>open)",
                        ticker, gap_pct * 100, current_ibs,
                    )
                    continue

                # --- Entry Condition 3: Volume filter ---
                vol_ratio_series = calc_volume_ratio(volume, lookback=20)
                current_vol_ratio = float(vol_ratio_series.iloc[-1])

                if pd.isna(current_vol_ratio):
                    current_vol_ratio = 0.0

                if current_vol_ratio < self.vol_surge_threshold:
                    self._logger.debug(
                        "%s: gap %.2f%% but volume ratio %.1fx < %.1fx threshold",
                        ticker, gap_pct * 100, current_vol_ratio,
                        self.vol_surge_threshold,
                    )
                    continue

                # --- Entry Condition 4: RSI(14) filter ---
                rsi_series = calc_rsi(close, period=14)
                current_rsi = float(rsi_series.iloc[-1])

                if pd.isna(current_rsi):
                    continue

                if current_rsi >= self.rsi14_max:
                    self._logger.debug(
                        "%s: gap %.2f%% but RSI(14)=%.1f >= %d",
                        ticker, gap_pct * 100, current_rsi, self.rsi14_max,
                    )
                    continue

                # --- Calculate ATR for stops ---
                atr = calc_atr(high, low, close, period=self.atr_period)
                current_atr = float(atr.iloc[-1])

                if pd.isna(current_atr) or current_atr <= 0:
                    continue

                # Entry price = today's close (signal at EOD T, execute MOO T+1)
                entry_price = today_close

                # Stop loss: entry - atr_stop_mult * ATR
                stop_price = entry_price - (self.atr_stop_mult * current_atr)
                if stop_price <= 0:
                    continue

                # No fixed take profit (exits via SMA/IBS/time)
                take_profit = None

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
                except (ValueError, ZeroDivisionError):
                    continue

                if pos["shares"] <= 0:
                    continue

                # --- Confidence scoring ---
                confidence = 0.60  # Base (range ~0.60–0.95)

                # 1. Gap magnitude bonus: up to +0.15 (larger gaps, saturate at 5%)
                abs_gap_pct = abs(gap_pct)
                gap_bonus = 0.15 * min(1.0, abs_gap_pct / 0.05)
                confidence += gap_bonus

                # 2. IBS depth bonus: up to +0.10 (lower IBS = more oversold)
                # IBS of 0.0 -> full bonus, IBS of ibs_confirm -> zero bonus
                if current_ibs < self.ibs_confirm:
                    ibs_bonus = 0.10 * (1.0 - current_ibs / self.ibs_confirm)
                else:
                    ibs_bonus = 0.0
                confidence += ibs_bonus

                # 3. Volume surge bonus: +0.10 if volume > 2x average
                vol_surge = current_vol_ratio > 2.0
                if vol_surge:
                    confidence += 0.10

                # 4. RSI depth bonus: up to +0.10 (lower RSI = more oversold)
                # RSI of 0 -> full bonus, RSI of rsi14_max -> zero bonus
                rsi_bonus = 0.10 * max(0.0, 1.0 - current_rsi / self.rsi14_max)
                confidence += rsi_bonus

                confidence = round(min(0.95, confidence), 4)

                # Build rationale
                confirm_type = "IBS=%.2f" % current_ibs if ibs_oversold else "bearish candle"
                vol_note = (
                    "Volume surge %.1fx avg" % current_vol_ratio
                    if vol_surge
                    else "Volume %.1fx avg" % current_vol_ratio
                )

                rationale = (
                    "%s gap down %.2f%% (open $%.2f vs prev close $%.2f). "
                    "Oversold confirm: %s. RSI(14)=%.1f. %s. "
                    "Stop $%.2f (-%.1fx ATR). Exit via SMA(%d)/IBS/time(%dd)."
                    % (
                        ticker, gap_pct * 100, today_open, yesterday_close,
                        confirm_type, current_rsi, vol_note,
                        stop_price, self.atr_stop_mult,
                        self.sma_exit_period, self.max_hold_days,
                    )
                )

                features = {
                    "gap_pct": round(gap_pct, 6),
                    "ibs": round(current_ibs, 4),
                    "rsi14": round(current_rsi, 2),
                    "volume_ratio": round(current_vol_ratio, 2),
                    "atr": round(current_atr, 4),
                    "oversold_type": "ibs" if ibs_oversold else "bearish_candle",
                }

                signals.append(Signal(
                    ticker=ticker,
                    strategy="opening_gap",
                    direction="long",
                    entry_price=entry_price,
                    stop_price=round(stop_price, 4),
                    take_profit=take_profit,
                    position_size=pos["shares"],
                    position_value=pos["position_value"],
                    risk_amount=pos["total_risk"],
                    confidence=confidence,
                    rationale=rationale,
                    features=features,
                ))

            except Exception as e:
                logger.debug(f"OpeningGap error for {ticker}: {e}")
                continue

        signals.sort(key=lambda s: s.confidence, reverse=True)
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        existing_positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check exit conditions for opening gap positions."""
        exits = []

        for pos in existing_positions:
            if pos.get("strategy") != "opening_gap":
                continue

            ticker = pos.get("ticker", "")
            if ticker not in data:
                continue

            df = data[ticker]
            if len(df) < max(self.sma_exit_period, self.atr_period) + 5:
                continue

            try:
                close = df["close"]
                high = df["high"]
                low = df["low"]
                current_price = float(close.iloc[-1])
                entry_price = pos.get("entry_price", current_price)
                entry_date = pos.get("entry_date")

                # ATR for stops
                atr_vals = calc_atr(high, low, close, self.atr_period)
                current_atr = float(atr_vals.iloc[-1])
                if np.isnan(current_atr) or current_atr <= 0:
                    current_atr = entry_price * 0.02

                exit_reason = None

                # 1. Stop loss
                stop_price = entry_price - self.atr_stop_mult * current_atr
                if current_price <= stop_price:
                    exit_reason = f"Stop loss: {current_price:.4f} <= {stop_price:.4f}"

                # 2. SMA exit: price closes above SMA (mean reverted)
                if exit_reason is None:
                    sma = close.rolling(self.sma_exit_period).mean()
                    sma_val = float(sma.iloc[-1])
                    if not np.isnan(sma_val) and current_price > sma_val:
                        exit_reason = f"SMA exit: {current_price:.4f} > SMA({self.sma_exit_period})={sma_val:.4f}"

                # 3. IBS exit: high IBS indicates overbought intraday
                if exit_reason is None:
                    ibs_series = calc_ibs(high, low, close)
                    current_ibs_val = float(ibs_series.iloc[-1])
                    if not np.isnan(current_ibs_val) and current_ibs_val > self.ibs_exit_threshold:
                        exit_reason = f"IBS exit: IBS={current_ibs_val:.4f} > {self.ibs_exit_threshold}"

                # 4. Time exit
                if exit_reason is None and entry_date is not None:
                    if hasattr(entry_date, "date"):
                        entry_dt = entry_date
                    else:
                        entry_dt = pd.Timestamp(entry_date)
                    current_dt = df.index[-1]
                    days_held = (current_dt - entry_dt).days
                    if days_held >= self.max_hold_days:
                        exit_reason = f"Time exit: {days_held} >= {self.max_hold_days} days"

                if exit_reason is not None:
                    exits.append({
                        "ticker": ticker,
                        "strategy": "opening_gap",
                        "exit_price": current_price,
                        "reason": exit_reason,
                        "exit_date": df.index[-1],
                    })

            except Exception as e:
                logger.debug(f"OpeningGap exit check error for {ticker}: {e}")
                continue

        return exits
