"""
Atlas Short-Horizon Mean-Reversion Strategy (RSI2 / Connors-style)
===================================================================
A HIGH-TURNOVER fast edge for the rapid validate->live pipeline: buy short-term oversold
dips WITHIN an uptrend, exit on mean-reversion within a few days. Because oversold dips
occur often across ~200 SP500 names and holds are 1-5 days, it generates many trades ->
forward (paper) evidence accrues in weeks, not years (unlike slow factor books).

Rules (long-only):
  Entry  (per ticker, not held):
    close > SMA(trend)            # only buy dips in an uptrend (regime filter)
    AND RSI(2) < rsi_entry        # short-term oversold
    AND close >= min_price
    -> rank candidates by RSI(2) ascending (most oversold first), fill open slots.
  Exit:
    close > SMA(exit_ma)          # reverted up through the short MA
    OR RSI(2) > rsi_exit          # momentum recovered
    OR price <= ATR stop
    OR held >= max_hold_days

Reference: Larry Connors & Cesar Alvarez, "Short Term Trading Strategies That Work" (RSI2).
Config Section: strategies.short_horizon_mr
"""
import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size

logger = logging.getLogger(__name__)


class ShortHorizonMR(BaseStrategy):
    """Short-horizon RSI2 mean-reversion (long-only, high turnover)."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        c = config.get("strategies", {}).get("short_horizon_mr", {})
        self.rsi_period = int(c.get("rsi_period", 2))
        self.rsi_entry = float(c.get("rsi_entry", 10))
        self.rsi_exit = float(c.get("rsi_exit", 65))
        self.sma_trend = int(c.get("sma_trend", 200))
        self.sma_exit = int(c.get("sma_exit", 5))
        self.atr_period = int(c.get("atr_period", 14))
        self.atr_stop_mult = float(c.get("atr_stop_mult", 2.5))
        self.max_hold_days = int(c.get("max_hold_days", 5))
        self.min_price = float(c.get("min_price", 5.0))
        self._logger.info("ShortHorizonMR init: RSI(%d)<%g in uptrend>SMA%d, exit RSI>%g/SMA%d/%dd",
                          self.rsi_period, self.rsi_entry, self.sma_trend, self.rsi_exit,
                          self.sma_exit, self.max_hold_days)

    @property
    def name(self) -> str:
        return "short_horizon_mr"

    def precompute(self, data: Dict[str, pd.DataFrame]) -> None:
        for _t, df in data.items():
            if df is None or df.empty or "close" not in df.columns:
                continue
            close = df["close"]
            df["_shmr_rsi"] = calc_rsi(close, self.rsi_period)
            df["_shmr_sma_trend"] = close.rolling(self.sma_trend, min_periods=self.sma_trend).mean()
            df["_shmr_sma_exit"] = close.rolling(self.sma_exit, min_periods=self.sma_exit).mean()
            if {"high", "low"}.issubset(df.columns):
                df["_shmr_atr"] = calc_atr(df["high"], df["low"], close, period=self.atr_period)
            else:
                df["_shmr_atr"] = close.pct_change().rolling(self.atr_period).std() * close
        self._precomputed = True

    def _row(self, df: pd.DataFrame):
        """(rsi, sma_trend, sma_exit, atr, price) at latest bar; precomputed or on-the-fly."""
        if len(df) < self.sma_trend + 2:
            return None
        last = df.iloc[-1]
        price = last.get("close")
        if price is None or not np.isfinite(price):
            return None
        if "_shmr_rsi" in df.columns and pd.notna(last.get("_shmr_rsi")):
            return (float(last["_shmr_rsi"]),
                    float(last["_shmr_sma_trend"]) if pd.notna(last.get("_shmr_sma_trend")) else np.nan,
                    float(last["_shmr_sma_exit"]) if pd.notna(last.get("_shmr_sma_exit")) else np.nan,
                    float(last["_shmr_atr"]) if pd.notna(last.get("_shmr_atr")) else np.nan,
                    float(price))
        close = df["close"]
        rsi = float(calc_rsi(close, self.rsi_period).iloc[-1])
        smt = float(close.rolling(self.sma_trend).mean().iloc[-1])
        sme = float(close.rolling(self.sma_exit).mean().iloc[-1])
        atr = (float(calc_atr(df["high"], df["low"], close, self.atr_period).iloc[-1])
               if {"high", "low"}.issubset(df.columns) else np.nan)
        return (rsi, smt, sme, atr, float(price))

    def generate_signals(self, data: Dict[str, pd.DataFrame], equity: float,
                         existing_positions: List[Dict[str, Any]]) -> List[Signal]:
        held = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission = self.fees_config.get("commission_per_trade", 0.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0)

        cands = []
        for ticker, df in data.items():
            if ticker in held or df is None or df.empty:
                continue
            row = self._row(df)
            if row is None:
                continue
            rsi, smt, sme, atr, price = row
            if price < self.min_price or not np.isfinite(rsi) or not np.isfinite(smt):
                continue
            if price <= smt:               # uptrend filter
                continue
            if rsi >= self.rsi_entry:       # not oversold
                continue
            if not np.isfinite(atr) or atr <= 0:
                continue
            cands.append((rsi, ticker, price, atr))

        cands.sort(key=lambda x: x[0])      # most oversold first
        signals: List[Signal] = []
        for rsi, ticker, price, atr in cands:
            if not self._can_open_position(existing_positions):
                break
            stop_price = price - self.atr_stop_mult * atr
            if stop_price <= 0 or stop_price >= price:
                continue
            pos = calc_position_size(equity=equity, risk_pct=risk_pct, entry_price=price,
                                     stop_price=stop_price, commission_per_trade=commission,
                                     commission_pct=commission_pct)
            if pos["shares"] <= 0:
                continue
            signals.append(Signal(
                ticker=ticker, strategy=self.name, direction="long",
                entry_price=price, stop_price=round(stop_price, 4), take_profit=None,
                position_size=pos["shares"], position_value=pos["position_value"],
                risk_amount=pos["total_risk"],
                # deeper oversold = higher conviction bounce; band clears the engine's
                # default min_confidence (0.65) for genuine setups.
                confidence=float(np.clip(0.70 + (self.rsi_entry - rsi) / max(self.rsi_entry, 1.0) * 0.20, 0.66, 0.92)),
                rationale=f"{ticker} RSI({self.rsi_period})={rsi:.1f} oversold dip in uptrend.",
                features={"rsi": round(rsi, 2)}, timestamp=datetime.now(),
            ))
            existing_positions = existing_positions + [{"ticker": ticker, "strategy": self.name}]
        self._logger.info("%s: %d entries (%d candidates)", self.name, len(signals), len(cands))
        return signals

    def check_exits(self, data: Dict[str, pd.DataFrame],
                    positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        exits: List[Dict[str, Any]] = []
        for ticker, pos, df in self._iter_my_positions(data, positions):
            row = self._row(df)
            price = float(df["close"].iloc[-1])
            stop_price = pos.get("stop_price", 0) or 0
            days_held = (df.index[-1] - pd.Timestamp(pos["entry_date"])).days
            if stop_price and price <= stop_price:
                exits.append({"ticker": ticker, "reason": "stop_hit", "exit_price": price,
                              "details": f"price {price:.2f} <= stop {stop_price:.2f}"})
                continue
            if row is not None:
                rsi, smt, sme, atr, _p = row
                if np.isfinite(sme) and price > sme:
                    exits.append({"ticker": ticker, "reason": "signal_exit", "exit_price": price,
                                  "details": f"reverted: price {price:.2f} > SMA{self.sma_exit} {sme:.2f}"})
                    continue
                if np.isfinite(rsi) and rsi > self.rsi_exit:
                    exits.append({"ticker": ticker, "reason": "signal_exit", "exit_price": price,
                                  "details": f"RSI {rsi:.1f} > {self.rsi_exit}"})
                    continue
            if days_held >= self.max_hold_days:
                exits.append({"ticker": ticker, "reason": "time_exit", "exit_price": price,
                              "details": f"held {days_held}d >= {self.max_hold_days}"})
        return exits


# Small, deliberate parameter grid (low search burden -> fair effective-N DSR).
PARAM_GRID = {
    "rsi_entry": [5, 10, 15],
    "rsi_exit": [60, 70],
    "max_hold_days": [3, 5, 8],
    "atr_stop_mult": [2.0, 2.5, 3.0],
    "sma_trend": [100, 200],
}
