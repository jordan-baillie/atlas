"""
Atlas Triple RSI Mean Reversion Strategy (Wave 3 — Sandbox)
=============================================================
High-conviction mean reversion strategy inspired by the Triple RSI methodology
published by QuantifiedStrategies.com (modified Larry Connors R3 concept).

Core concept: Buy when RSI is declining for multiple consecutive days into
oversold territory, but wasn't overbought recently. Exit when RSI recovers.
The consecutive-decline filter dramatically improves signal quality vs
single-reading oversold strategies.

Published edge (SPY): 90% win rate, PF ~4.0, avg gain 1.2% per trade,
~103 trades since 1993. Low trade count but very high quality.

Key difference from existing strategies:
  - mean_reversion: RSI(14) + z-score, take-profit exit
  - connors_rsi2: RSI(2) extreme, SMA(5) exit
  - triple_rsi: RSI(5) + 3-day decline + lookback check, RSI recovery exit

This generates RARE but HIGH-CONVICTION signals. Expects ~3-8 trades/year
per stock universe. Designed to complement (not compete with) existing MR.

References:
  - QuantifiedStrategies.com: Triple RSI Trading Strategy (90% Win Rate)
  - Connors & Alvarez, R3 strategy variant
  - Alvarez Quant Trading: IBS + RSI filter research

Config Section: strategies.triple_rsi

Usage:
    from research.strategies.triple_rsi import TripleRSI
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size, calc_volume_ratio, calc_ibs

logger = logging.getLogger(__name__)


class TripleRSI(BaseStrategy):
    """Triple RSI mean reversion: buy on confirmed RSI decline into oversold."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("triple_rsi", {})

        # RSI parameters
        self.rsi_period = strat_cfg.get("rsi_period", 5)          # RSI lookback (5 in published)
        self.rsi_entry = strat_cfg.get("rsi_entry", 30)           # Buy when RSI < this
        self.rsi_exit = strat_cfg.get("rsi_exit", 50)             # Sell when RSI crosses above this
        self.decline_days = strat_cfg.get("decline_days", 3)       # Consecutive RSI decline days
        self.rsi_lookback_max = strat_cfg.get("rsi_lookback_max", 60)  # RSI was < this N days ago

        # Trend filter
        self.sma_trend_period = strat_cfg.get("sma_trend_period", 200)
        self.sma200_filter = strat_cfg.get("sma200_filter", True)

        # Volume filter
        vol_cfg = strat_cfg.get("volume", {})
        self.vol_lookback = vol_cfg.get("lookback", 20)
        self.vol_min_ratio = vol_cfg.get("min_ratio", 1.0)

        # IBS filter (optional — from Alvarez research)
        self.ibs_max = strat_cfg.get("ibs_max", 1.0)  # 1.0 = disabled

        # Risk parameters
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.5)
        self.max_hold_days = strat_cfg.get("max_hold_days", 10)

        self._logger.info(
            "TripleRSI initialized: rsi_period=%d, rsi_entry=%d, "
            "rsi_exit=%d, decline_days=%d, rsi_lookback_max=%d, "
            "sma200=%s, vol_min=%.1f, ibs_max=%.2f, "
            "atr=%d, stop=%.1fx, max_hold=%d",
            self.rsi_period, self.rsi_entry, self.rsi_exit,
            self.decline_days, self.rsi_lookback_max,
            'ON' if self.sma200_filter else 'OFF',
            self.vol_min_ratio, self.ibs_max,
            self.atr_period, self.atr_stop_mult, self.max_hold_days,
        )

    @property
    def name(self) -> str:
        return "triple_rsi"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Scan for Triple RSI entry signals.

        Entry requires ALL of:
        1. RSI(N) < rsi_entry (e.g., RSI(5) < 30)
        2. RSI has been declining for decline_days consecutive days
        3. RSI was < rsi_lookback_max (e.g., < 60) exactly decline_days ago
        4. Close > SMA-200 (trend filter)
        5. Volume > vol_min_ratio * 20-day avg (optional)
        6. IBS < ibs_max (optional)
        """
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

                # Need enough data for SMA-200 + RSI + lookback
                min_rows = max(self.sma_trend_period + 20, self.rsi_period + self.decline_days + 20, 220)
                if not self._has_sufficient_data(df, min_rows):
                    continue

                current = df.iloc[-1]
                close = current["close"]

                # --- 1. SMA-200 trend filter ---
                if self.sma200_filter:
                    sma200 = df["close"].rolling(self.sma_trend_period).mean().iloc[-1]
                    if pd.isna(sma200) or close <= sma200:
                        continue

                # --- 2. RSI calculation and entry check ---
                rsi_series = calc_rsi(df["close"], period=self.rsi_period)
                if rsi_series is None or len(rsi_series) < self.decline_days + 2:
                    continue

                current_rsi = rsi_series.iloc[-1]
                if pd.isna(current_rsi) or current_rsi >= self.rsi_entry:
                    continue

                # --- 3. RSI declining for decline_days consecutive days ---
                rsi_declining = True
                for i in range(1, self.decline_days + 1):
                    rsi_today = rsi_series.iloc[-i]
                    rsi_yesterday = rsi_series.iloc[-(i + 1)]
                    if pd.isna(rsi_today) or pd.isna(rsi_yesterday):
                        rsi_declining = False
                        break
                    if rsi_today >= rsi_yesterday:
                        rsi_declining = False
                        break
                if not rsi_declining:
                    continue

                # --- 4. RSI was < rsi_lookback_max N days ago ---
                # This prevents buying after a sharp drop from overbought
                rsi_n_days_ago = rsi_series.iloc[-(self.decline_days + 1)]
                if pd.isna(rsi_n_days_ago) or rsi_n_days_ago >= self.rsi_lookback_max:
                    continue

                # --- 5. Volume filter ---
                vol_series = calc_volume_ratio(df["volume"], self.vol_lookback)
                vol_ratio = vol_series.iloc[-1] if vol_series is not None and len(vol_series) > 0 else None
                if vol_ratio is not None and not pd.isna(vol_ratio) and vol_ratio < self.vol_min_ratio:
                    continue

                # --- 6. IBS filter (optional) ---
                if self.ibs_max < 1.0:
                    ibs_series = calc_ibs(df["high"], df["low"], df["close"])
                    ibs = ibs_series.iloc[-1] if ibs_series is not None and len(ibs_series) > 0 else None
                    if ibs is not None and not pd.isna(ibs) and ibs > self.ibs_max:
                        continue

                # --- Calculate entry, stop, position size ---
                entry_price = close  # Signal at close, enter next open

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

                # Confidence: high base (selective strategy) + RSI depth bonus
                rsi_depth = max(0, 1 - (current_rsi / max(self.rsi_entry, 1)))
                confidence = 0.80 + (0.15 * rsi_depth)
                if vol_ratio is not None and vol_ratio >= 1.5:
                    confidence = min(1.0, confidence + 0.05)

                rationale = (
                    f"TripleRSI: RSI({self.rsi_period})={current_rsi:.1f} < {self.rsi_entry}, "
                    f"declining {self.decline_days} consecutive days. "
                    f"RSI was {rsi_n_days_ago:.1f} < {self.rsi_lookback_max} "
                    f"{self.decline_days} days ago. "
                    f"Price > SMA-{self.sma_trend_period}. "
                    f"Vol={vol_ratio:.1f}x. High-conviction MR signal."
                )

                signals.append(Signal(
                    ticker=ticker,
                    strategy=self.name,
                    direction="long",
                    entry_price=entry_price,
                    stop_price=round(stop_price, 2),
                    take_profit=None,
                    position_size=position_size,
                    position_value=round(position_value, 2),
                    risk_amount=round(risk_amount, 2),
                    confidence=round(confidence, 3),
                    rationale=rationale,
                    features={
                        "rsi": round(current_rsi, 2),
                        "rsi_n_days_ago": round(rsi_n_days_ago, 2),
                        "decline_days": self.decline_days,
                        "atr": round(atr, 4),
                        "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
                        "entry_mode": "triple_rsi_decline",
                    },
                ))

            except Exception as e:
                self._logger.warning("TripleRSI signal error for %s: %s", ticker, e)
                continue

        # Sort by RSI (most oversold first — highest conviction)
        signals.sort(key=lambda s: s.features.get("rsi", 999))
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check Triple RSI exit conditions.

        Exit when:
        1. RSI(N) crosses above rsi_exit (mean reversion complete)
        2. Stop loss hit
        3. Time-based (max_hold_days exceeded)
        """
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

                # --- RSI recovery exit (primary) ---
                rsi_series = calc_rsi(df["close"], period=self.rsi_period)
                if rsi_series is not None and len(rsi_series) > 0:
                    current_rsi = rsi_series.iloc[-1]
                    if not pd.isna(current_rsi) and current_rsi > self.rsi_exit:
                        exits.append({
                            "ticker": ticker,
                            "reason": "signal_exit",
                            "exit_price": close,
                            "details": (
                                f"RSI({self.rsi_period})={current_rsi:.1f} > "
                                f"{self.rsi_exit} — mean reversion complete"
                            ),
                        })
                        continue

            except Exception as e:
                self._logger.warning("TripleRSI exit error for %s: %s", ticker, e)
                continue

        return exits
