"""
Atlas Short-Term Mean Reversion Strategy
=============================================
Generates long signals when a stock shows extreme short-term oversold
conditions using RSI(2) and Internal Bar Strength (IBS), expecting
a quick bounce back to the short-term mean.

Entry Conditions (ALL must be true):
    - RSI(2) < 10 OR IBS < 0.2 (either trigger is sufficient)
    - Price must be below SMA(5) (buying into weakness)
    - Standard volume and earnings filters

Exit Conditions (any triggers exit):
    1. Hard stop: price drops below entry - 1.5 * ATR(14)
    2. Take profit: price reaches entry + 1.0 * ATR(14)
    3. Mean reversion exit: RSI(2) > 70 (overbought bounce achieved)
    4. Close crosses above SMA(5) (trend restored)
    5. Time exit: held > max_hold_days (default 5)

Config Section: strategies.short_term_mr

Usage:
    from strategies.short_term_mr import ShortTermMR
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_ibs, calc_position_size, calc_volume_ratio
from utils.earnings import is_near_earnings

logger = logging.getLogger(__name__)


class ShortTermMR(BaseStrategy):
    """Short-term mean reversion: buy extreme oversold stocks expecting a quick bounce."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("short_term_mr", {})
        self.rsi_period = strat_cfg.get("rsi_period", 2)
        self.rsi_oversold = strat_cfg.get("rsi_oversold", 10)
        self.ibs_oversold = strat_cfg.get("ibs_oversold", 0.2)
        self.sma_period = strat_cfg.get("sma_period", 5)
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 1.5)
        self.profit_target_atr_mult = strat_cfg.get("profit_target_atr_mult", 1.0)
        self.max_hold_days = strat_cfg.get("max_hold_days", 5)
        self.rsi_overbought_exit = strat_cfg.get("rsi_overbought_exit", 70)
        # Volume confirmation parameters
        vol_cfg = strat_cfg.get("volume", {})
        self.vol_lookback = vol_cfg.get("lookback", 20)
        self.vol_min_ratio = vol_cfg.get("min_ratio", 0.5)
        self.vol_surge_threshold = vol_cfg.get("surge_threshold", 2.0)
        self.vol_surge_boost = vol_cfg.get("surge_boost", 0.10)
        # Earnings blackout parameters
        earnings_cfg = strat_cfg.get("earnings_blackout", {})
        self.earnings_blackout_enabled = earnings_cfg.get("enabled", True)
        self.earnings_blackout_before = earnings_cfg.get("days_before", 5)
        self.earnings_blackout_after = earnings_cfg.get("days_after", 1)

        self._precomputed = False
        self._logger.info(
            "ShortTermMR initialized: rsi_period=%d, rsi_oversold=%d, "
            "ibs_oversold=%.2f, sma_period=%d, profit_target=%.1fx ATR, "
            "vol_surge=%.1fx, earnings_blackout=%s",
            self.rsi_period, self.rsi_oversold, self.ibs_oversold,
            self.sma_period, self.profit_target_atr_mult,
            self.vol_surge_threshold,
            "ON" if self.earnings_blackout_enabled else "OFF",
        )

    @property
    def name(self) -> str:
        return "short_term_mr"

    def precompute(self, data: Dict[str, pd.DataFrame]) -> None:
        """Pre-compute all indicator columns once before the walk-forward loop."""
        for ticker, df in data.items():
            close = df["close"]
            high = df["high"]
            low = df["low"]
            volume = df["volume"]
            df["_st_rsi"] = calc_rsi(close, period=self.rsi_period)
            df["_st_ibs"] = calc_ibs(high, low, close)
            df["_st_sma"] = close.rolling(window=self.sma_period).mean()
            df["_st_atr"] = calc_atr(high, low, close, period=self.atr_period)
            df["_st_vol_ratio"] = calc_volume_ratio(volume, lookback=self.vol_lookback)
        self._precomputed = True

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Scan all tickers for short-term mean reversion entry signals.

        A signal is generated when:
            1. RSI(2) < rsi_oversold OR IBS < ibs_oversold
            2. Price is below SMA(sma_period)
            3. Risk limits allow a new position
            4. Ticker is not already held
        """
        signals: List[Signal] = []
        held_tickers = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0008)

        # Minimum rows needed
        min_rows = max(self.rsi_period, self.sma_period, self.atr_period, self.vol_lookback) + 10

        for ticker, df in data.items():
            try:
                if ticker in held_tickers:
                    continue

                if not self._can_open_position(existing_positions):
                    self._logger.debug("Max positions reached, skipping remaining tickers")
                    break

                if not self._has_sufficient_data(df, min_rows):
                    self._logger.debug(
                        "%s: insufficient data (%d rows, need %d)",
                        ticker, len(df), min_rows,
                    )
                    continue

                close = df["close"]
                high = df["high"]
                low = df["low"]
                volume = df["volume"]

                # Calculate indicators
                today_close = close.iloc[-1]
                if self._precomputed:
                    current_rsi = df["_st_rsi"].iloc[-1]
                    current_ibs = df["_st_ibs"].iloc[-1]
                    current_sma = df["_st_sma"].iloc[-1]
                else:
                    rsi = calc_rsi(close, period=self.rsi_period)
                    ibs = calc_ibs(high, low, close)
                    sma = close.rolling(window=self.sma_period).mean()
                    current_rsi = rsi.iloc[-1]
                    current_ibs = ibs.iloc[-1]
                    current_sma = sma.iloc[-1]

                if pd.isna(current_rsi) or pd.isna(current_sma):
                    continue

                # Entry conditions
                is_rsi_oversold = current_rsi < self.rsi_oversold
                is_ibs_oversold = (not pd.isna(current_ibs)) and current_ibs < self.ibs_oversold
                is_below_sma = today_close < current_sma

                # RSI(2) < 10 OR IBS < 0.2, AND price < SMA(5)
                if not ((is_rsi_oversold or is_ibs_oversold) and is_below_sma):
                    continue

                # Earnings blackout check
                if self.earnings_blackout_enabled:
                    reference_date = df.index[-1]
                    try:
                        if is_near_earnings(
                            ticker,
                            reference_date=reference_date,
                            blackout_days_before=self.earnings_blackout_before,
                            blackout_days_after=self.earnings_blackout_after,
                        ):
                            self._logger.debug(
                                "%s: within earnings blackout window, skipping", ticker
                            )
                            continue
                    except Exception as e:
                        self._logger.debug(
                            "%s: earnings check failed (%s), proceeding", ticker, e
                        )

                # Volume confirmation (noted for confidence, no hard filter)
                if self._precomputed:
                    current_vol_ratio = df["_st_vol_ratio"].iloc[-1]
                else:
                    vol_ratio = calc_volume_ratio(volume, lookback=self.vol_lookback)
                    current_vol_ratio = vol_ratio.iloc[-1]

                if pd.isna(current_vol_ratio):
                    current_vol_ratio = 1.0  # Neutral if no data

                # Calculate ATR
                if self._precomputed:
                    current_atr = df["_st_atr"].iloc[-1]
                else:
                    atr = calc_atr(high, low, close, period=self.atr_period)
                    current_atr = atr.iloc[-1]

                if pd.isna(current_atr) or current_atr <= 0:
                    self._logger.debug("%s: invalid ATR (%s)", ticker, current_atr)
                    continue

                entry_price = today_close

                # Stop loss: entry - atr_stop_mult * ATR
                stop_price = entry_price - (self.atr_stop_mult * current_atr)
                if stop_price <= 0:
                    self._logger.debug("%s: stop price <= 0, skipping", ticker)
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
                    )
                except ValueError as e:
                    self._logger.debug("%s: position sizing error: %s", ticker, e)
                    continue

                if pos["shares"] <= 0:
                    self._logger.debug("%s: position size is 0, skipping", ticker)
                    continue

                # Confidence scoring
                # Base: 0.6 for meeting entry criteria
                confidence = 0.6

                # RSI(2) depth bonus: up to +0.15, saturates at RSI=0
                if is_rsi_oversold:
                    rsi_depth = min(1.0, max(0.0, (self.rsi_oversold - current_rsi) / self.rsi_oversold))
                    confidence += 0.15 * rsi_depth

                # IBS depth bonus: up to +0.15, saturates at IBS=0
                if is_ibs_oversold:
                    ibs_depth = min(1.0, max(0.0, (self.ibs_oversold - current_ibs) / self.ibs_oversold))
                    confidence += 0.15 * ibs_depth

                # Volume surge bonus: +0.10 if volume_ratio > surge_threshold
                if current_vol_ratio > self.vol_surge_threshold:
                    confidence += self.vol_surge_boost

                confidence = min(1.0, confidence)

                # Volume note for rationale
                if current_vol_ratio >= self.vol_surge_threshold:
                    vol_note = "Volume surge %.1fx avg (capitulation). " % current_vol_ratio
                elif current_vol_ratio < 0.6:
                    vol_note = "Volume low %.1fx avg (weak dip). " % current_vol_ratio
                else:
                    vol_note = "Volume %.1fx avg. " % current_vol_ratio

                # Build rationale
                triggers = []
                if is_rsi_oversold:
                    triggers.append("RSI(2)=%.1f < %d" % (current_rsi, self.rsi_oversold))
                if is_ibs_oversold:
                    triggers.append("IBS=%.3f < %.1f" % (current_ibs, self.ibs_oversold))

                rationale = (
                    "%s short-term oversold: %s. "
                    "Price $%.2f below SMA(%d)=$%.2f. "
                    "%s"
                    "Target $%.2f (+%.1fx ATR), stop at $%.2f (-%.1fx ATR)."
                ) % (
                    ticker, ", ".join(triggers),
                    today_close, self.sma_period, current_sma,
                    vol_note,
                    take_profit, self.profit_target_atr_mult,
                    stop_price, self.atr_stop_mult,
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
                        "rsi_2": round(current_rsi, 2),
                        "ibs": round(current_ibs, 4) if not pd.isna(current_ibs) else None,
                        "sma_5": round(current_sma, 4),
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
                    f"{ticker}: signal generation error: {e}", exc_info=True
                )
                continue

        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check open short-term MR positions for exit conditions.

        Exit conditions (priority order):
            1. Hard stop: price drops below stop_price
            2. Take profit: price reaches take_profit level
            3. RSI overbought exit: RSI(2) > rsi_overbought_exit (bounce achieved)
            4. SMA crossover exit: close > SMA(sma_period) (trend restored)
            5. Time exit: position held longer than max_hold_days
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

                days_held = (today_date - entry_date).days

                # Calculate RSI(2) and SMA for exit checks
                if self._precomputed:
                    current_rsi = df["_st_rsi"].iloc[-1]
                    current_sma = df["_st_sma"].iloc[-1]
                else:
                    rsi = calc_rsi(close, period=self.rsi_period)
                    sma = close.rolling(window=self.sma_period).mean()
                    current_rsi = rsi.iloc[-1] if not rsi.empty else 50
                    current_sma = sma.iloc[-1] if not sma.empty else today_close

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
                # 3. RSI overbought exit (bounce achieved)
                elif not pd.isna(current_rsi) and current_rsi > self.rsi_overbought_exit:
                    exits.append({
                        "ticker": ticker,
                        "reason": "signal_exit",
                        "exit_price": today_close,
                        "details": (
                            f"{ticker} RSI({self.rsi_period})={current_rsi:.1f} > "
                            f"{self.rsi_overbought_exit} (overbought bounce). "
                            f"Close=${today_close:.2f}, entry=${entry_price:.2f}, "
                            f"held {days_held} days."
                        ),
                    })
                # 4. SMA crossover exit (trend restored)
                elif not pd.isna(current_sma) and today_close > current_sma and entry_price < current_sma:
                    exits.append({
                        "ticker": ticker,
                        "reason": "signal_exit",
                        "exit_price": today_close,
                        "details": (
                            f"{ticker} crossed above SMA({self.sma_period})=${current_sma:.2f}. "
                            f"Close=${today_close:.2f}, entry=${entry_price:.2f}, "
                            f"held {days_held} days."
                        ),
                    })
                # 5. Time exit
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
