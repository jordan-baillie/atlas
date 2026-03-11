"""
Atlas Connors RSI(2) Mean Reversion Strategy
==============================================
Short-term mean reversion strategy based on Larry Connors' RSI(2) methodology
from "Short Term Trading Strategies That Work" (2008).

Core concept: When RSI(2) drops to extreme oversold levels (<10) in stocks
trading above their 200-day SMA, a short-term bounce is highly probable.
Exit when the stock closes above its 5-day SMA (mean reversion complete).

This strategy is DISTINCT from the existing mean_reversion strategy:
  - mean_reversion: RSI(14) + z-score entry, ATR profit target exit
  - connors_rsi2: RSI(2) extreme oversold, SMA(5) mean-reversion exit

Published edge: 74%+ win rate across 34 years of S&P 500 data. Works on
individual stocks with higher volume (institutional participation).

Key research references:
  - Connors & Alvarez, "Short Term Trading Strategies That Work" (2008)
  - QuantifiedStrategies.com backtests (2012-2025, strategies still profitable)
  - Alvarez Quant Trading: IBS + RSI(2) combination research

Config Section: strategies.connors_rsi2

Usage:
    from strategies.connors_rsi2 import ConnorsRSI2
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size, calc_volume_ratio, calc_ibs

logger = logging.getLogger(__name__)


class ConnorsRSI2(BaseStrategy):
    """Connors RSI(2) mean reversion: buy extreme oversold, exit at SMA recovery."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("connors_rsi2", {})

        # Entry parameters
        self.rsi_period = strat_cfg.get("rsi_period", 2)
        self.rsi_entry = strat_cfg.get("rsi_entry", 10)        # Buy when RSI(2) < this
        self.sma_trend_period = strat_cfg.get("sma_trend_period", 200)  # Trend filter
        self.sma200_filter = strat_cfg.get("sma200_filter", True)       # Require above SMA-200

        # Additional entry filters
        self.min_consecutive_down = strat_cfg.get("min_consecutive_down", 0)  # 0=disabled
        self.ibs_max = strat_cfg.get("ibs_max", 0.5)    # IBS < this for entry (close near low)
        self.ibs_filter_enabled = strat_cfg.get("ibs_filter_enabled", False)

        # Volume filter (proven in wave 1: 1.5x improves quality)
        vol_cfg = strat_cfg.get("volume", {})
        self.vol_lookback = vol_cfg.get("lookback", 20)
        self.vol_min_ratio = vol_cfg.get("min_ratio", 0.5)

        # Exit parameters
        self.sma_exit_period = strat_cfg.get("sma_exit_period", 5)  # Exit when close > SMA(N)
        self.rsi_exit = strat_cfg.get("rsi_exit", 65)              # Alternative: exit when RSI(2) > this
        self.exit_mode = strat_cfg.get("exit_mode", "sma")         # "sma" or "rsi" or "both"
        self.max_hold_days = strat_cfg.get("max_hold_days", 10)

        # Risk parameters
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 3.0)  # Wide stop — MR needs room

        self._precomputed = False
        self._logger.info(
            "ConnorsRSI2 initialized: rsi_period=%d, rsi_entry=%d, "
            "sma_trend=%d, sma_exit=%d, exit_mode=%s, max_hold=%d, "
            "atr=%d, stop=%.1fx, sma200=%s, ibs_filter=%s",
            self.rsi_period, self.rsi_entry,
            self.sma_trend_period, self.sma_exit_period,
            self.exit_mode, self.max_hold_days,
            self.atr_period, self.atr_stop_mult,
            'ON' if self.sma200_filter else 'OFF',
            'ON' if self.ibs_filter_enabled else 'OFF',
        )

    @property
    def name(self) -> str:
        return "connors_rsi2"

    def precompute(self, data: Dict[str, pd.DataFrame]) -> None:
        """Pre-compute all indicator columns once before the walk-forward loop."""
        for ticker, df in data.items():
            close = df["close"]
            high = df["high"]
            low = df["low"]
            volume = df["volume"]
            df["_cr_rsi"] = calc_rsi(close, period=self.rsi_period)
            df["_cr_sma_trend"] = close.rolling(self.sma_trend_period).mean()
            df["_cr_atr"] = calc_atr(high, low, close, period=self.atr_period)
            df["_cr_vol_ratio"] = calc_volume_ratio(volume, lookback=self.vol_lookback)
            df["_cr_sma_exit"] = close.rolling(self.sma_exit_period).mean()
            if getattr(self, 'ibs_filter_enabled', False):
                df["_cr_ibs"] = calc_ibs(high, low, close)
        self._precomputed = True

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Scan all tickers for RSI(2) extreme oversold entry signals."""
        signals: List[Signal] = []
        held_tickers = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0008)
        min_position_value = self.fees_config.get("min_position_value", 100.0)

        for ticker, df in data.items():
            try:
                if ticker in held_tickers:
                    continue

                if not self._can_open_position(existing_positions):
                    break

                # Need enough data for SMA-200 + indicators
                min_rows = max(self.sma_trend_period + 20, 220)
                if not self._has_sufficient_data(df, min_rows):
                    continue

                # Use T-1 data (last complete bar) to avoid look-ahead
                current = df.iloc[-1]
                close = current["close"]
                high = current["high"]
                low = current["low"]

                # --- SMA-200 trend filter ---
                if self.sma200_filter:
                    if self._precomputed:
                        sma200 = df["_cr_sma_trend"].iloc[-1]
                    else:
                        sma200 = df["close"].rolling(self.sma_trend_period).mean().iloc[-1]
                    if pd.isna(sma200) or close <= sma200:
                        continue

                # --- RSI(2) extreme oversold ---
                if self._precomputed:
                    current_rsi = df["_cr_rsi"].iloc[-1]
                else:
                    rsi_vals = calc_rsi(df["close"], period=self.rsi_period)
                    if rsi_vals is None or len(rsi_vals) < 2:
                        continue
                    current_rsi = rsi_vals.iloc[-1]
                if pd.isna(current_rsi) or current_rsi >= self.rsi_entry:
                    continue

                # --- Volume filter ---
                if self._precomputed:
                    vol_ratio = df["_cr_vol_ratio"].iloc[-1]
                else:
                    vol_series = calc_volume_ratio(df["volume"], self.vol_lookback)
                    vol_ratio = vol_series.iloc[-1] if vol_series is not None and len(vol_series) > 0 else None
                if vol_ratio is not None and not pd.isna(vol_ratio) and vol_ratio < self.vol_min_ratio:
                    continue

                # --- IBS filter (optional) ---
                if self.ibs_filter_enabled:
                    if self._precomputed:
                        ibs = df["_cr_ibs"].iloc[-1]
                    else:
                        ibs_series = calc_ibs(df["high"], df["low"], df["close"])
                        ibs = ibs_series.iloc[-1] if ibs_series is not None and len(ibs_series) > 0 else None
                    if ibs is None or pd.isna(ibs) or ibs > self.ibs_max:
                        continue

                # --- Consecutive down days filter (optional) ---
                if self.min_consecutive_down > 0:
                    closes = df["close"].iloc[-(self.min_consecutive_down + 1):]
                    down_days = sum(1 for i in range(1, len(closes))
                                   if closes.iloc[i] < closes.iloc[i - 1])
                    if down_days < self.min_consecutive_down:
                        continue

                # --- Calculate entry, stop, position size ---
                # Entry at next bar open (we detect signal at close, enter next open)
                entry_price = close  # Approximation; actual entry at next open

                if self._precomputed:
                    atr = df["_cr_atr"].iloc[-1]
                else:
                    atr_series = calc_atr(
                        df["high"], df["low"], df["close"],
                        period=self.atr_period,
                    )
                    if atr_series is None or atr_series.empty:
                        continue
                    atr = atr_series.iloc[-1]
                if pd.isna(atr) or atr <= 0:
                    continue

                stop_price = entry_price - (self.atr_stop_mult * atr)
                if stop_price <= 0 or stop_price >= entry_price:
                    continue

                risk_per_share = entry_price - stop_price
                pos_result = calc_position_size(
                    equity=equity,
                    risk_pct=risk_pct,
                    entry_price=entry_price,
                    stop_price=stop_price,
                    commission_per_trade=commission_per_trade,
                    commission_pct=commission_pct,
                )
                position_size = pos_result["shares"] if isinstance(pos_result, dict) else pos_result
                if position_size <= 0:
                    continue

                position_value = position_size * entry_price
                if position_value < min_position_value:
                    continue

                risk_amount = position_size * risk_per_share

                # Confidence: based on RSI extremity (lower RSI = higher confidence)
                # RSI 0 → conf 0.95, RSI threshold → conf 0.75
                rsi_ratio = max(0, 1 - (current_rsi / max(self.rsi_entry, 1)))
                confidence = 0.75 + (0.20 * rsi_ratio)

                # Volume boost
                if vol_ratio is not None and vol_ratio >= 1.5:
                    confidence = min(1.0, confidence + 0.05)

                rationale = (
                    f"RSI({self.rsi_period})={current_rsi:.1f} < {self.rsi_entry} "
                    f"(extreme oversold). Price above SMA-{self.sma_trend_period}. "
                    f"Vol ratio={vol_ratio:.1f}x. "
                    f"Expecting short-term mean reversion bounce."
                )

                signals.append(Signal(
                    ticker=ticker,
                    strategy=self.name,
                    direction="long",
                    entry_price=entry_price,
                    stop_price=round(stop_price, 2),
                    take_profit=None,  # Exit via SMA or RSI recovery
                    position_size=position_size,
                    position_value=round(position_value, 2),
                    risk_amount=round(risk_amount, 2),
                    confidence=round(confidence, 3),
                    rationale=rationale,
                    features={
                        "rsi_2": round(current_rsi, 2),
                        "atr": round(atr, 4),
                        "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
                        "entry_mode": "rsi2_oversold",
                    },
                ))

            except Exception as e:
                self._logger.warning("ConnorsRSI2 signal error for %s: %s", ticker, e)
                continue

        # Sort by RSI (most oversold first — highest conviction)
        signals.sort(key=lambda s: s.features.get("rsi_2", 999))
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check open positions for RSI(2) exit conditions."""
        exits = []

        for pos in positions:
            if pos.get("strategy") != self.name:
                continue

            ticker = pos["ticker"]
            df = data.get(ticker)
            if df is None or df.empty:
                continue

            try:
                current = df.iloc[-1]
                close = current["close"]
                entry_price = pos.get("entry_price", close)
                entry_date = pos.get("entry_date")

                # --- Stop loss ---
                stop_price = pos.get("stop_price", 0)
                if stop_price > 0 and close <= stop_price:
                    exits.append({
                        "ticker": ticker,
                        "reason": "stop_hit",
                        "exit_price": close,
                        "details": f"Close {close:.2f} <= stop {stop_price:.2f}",
                    })
                    continue

                # --- Time exit ---
                if entry_date:
                    if isinstance(entry_date, str):
                        entry_dt = pd.Timestamp(entry_date)
                    else:
                        entry_dt = pd.Timestamp(entry_date)
                    days_held = (df.index[-1] - entry_dt).days
                    if days_held >= self.max_hold_days:
                        exits.append({
                            "ticker": ticker,
                            "reason": "time_exit",
                            "exit_price": close,
                            "details": f"Held {days_held} days >= max {self.max_hold_days}",
                        })
                        continue

                # --- SMA exit (primary: close > SMA-5 = mean reversion complete) ---
                if self.exit_mode in ("sma", "both"):
                    if self._precomputed:
                        sma_exit = df["_cr_sma_exit"].iloc[-1]
                    else:
                        sma_exit = df["close"].rolling(self.sma_exit_period).mean().iloc[-1]
                    if not pd.isna(sma_exit) and close > sma_exit:
                        exits.append({
                            "ticker": ticker,
                            "reason": "signal_exit",
                            "exit_price": close,
                            "details": (
                                f"Close {close:.2f} > SMA({self.sma_exit_period}) "
                                f"{sma_exit:.2f} — mean reversion complete"
                            ),
                        })
                        continue

                # --- RSI exit (alternative: RSI recovered above threshold) ---
                if self.exit_mode in ("rsi", "both"):
                    if self._precomputed:
                        current_rsi = df["_cr_rsi"].iloc[-1]
                    else:
                        rsi_vals = calc_rsi(df["close"], period=self.rsi_period)
                        current_rsi = rsi_vals.iloc[-1] if rsi_vals is not None and len(rsi_vals) > 0 else float("nan")
                    if not pd.isna(current_rsi) and current_rsi > self.rsi_exit:
                        exits.append({
                            "ticker": ticker,
                            "reason": "signal_exit",
                            "exit_price": close,
                            "details": (
                                f"RSI({self.rsi_period})={current_rsi:.1f} > "
                                f"{self.rsi_exit} — oversold condition resolved"
                            ),
                        })
                        continue

            except Exception as e:
                self._logger.warning("ConnorsRSI2 exit error for %s: %s", ticker, e)
                continue

        return exits
