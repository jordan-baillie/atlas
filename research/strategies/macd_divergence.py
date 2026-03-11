"""
Atlas MACD Divergence Strategy
========================================
Bullish divergence: price makes new lower low, but MACD histogram makes a
higher low (momentum recovering while price still falling).  This signals
exhaustion of sellers and an impending reversal.

Reference: Gerald Appel (1979), MACD divergence patterns
Academic basis: Bullish divergence success rate ~64% in trending markets
(Lo & Hasanhodzic, 2010: The Evolution of Technical Analysis)

Core logic:
  1. Scan for price making a N-period low (new low over divergence_lookback)
  2. Check MACD histogram is higher now vs the previous low bar
     (momentum recovering even as price grinds lower)
  3. Histogram must still be negative (below zero) or recently crossing
     (we're catching the turn, not chasing it)
  4. SMA-200 filter: only buy in uptrends (divergences in downtrends fail more)
  5. Entry at close, ATR-based stop below entry
  6. Exit: MACD histogram crosses above zero (momentum confirmed) OR time

Config Section: strategies.macd_divergence
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size, calc_volume_ratio

logger = logging.getLogger(__name__)


class MacdDivergence(BaseStrategy):
    """Buy bullish MACD divergence: price lower low, histogram higher low."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("macd_divergence", {})

        # MACD parameters
        self.macd_fast = strat_cfg.get("macd_fast", 12)
        self.macd_slow = strat_cfg.get("macd_slow", 26)
        self.macd_signal = strat_cfg.get("macd_signal", 9)

        # Divergence detection
        self.divergence_lookback = strat_cfg.get("divergence_lookback", 20)
        # Minimum histogram delta to qualify as divergence (avoids noise)
        self.min_hist_improvement = strat_cfg.get("min_hist_improvement", 0.001)

        # Trend filter
        self.sma200_filter = strat_cfg.get("sma200_filter", True)

        # Risk management
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)
        self.max_hold_days = strat_cfg.get("max_hold_days", 10)
        self.profit_target_atr_mult = strat_cfg.get("profit_target_atr_mult", 0.0)

        # Volume confirmation
        self.vol_lookback = strat_cfg.get("vol_lookback", 20)
        self.vol_min_ratio = strat_cfg.get("vol_min_ratio", 0.5)

        self._logger.info(
            f"MacdDivergence initialized: MACD({self.macd_fast},{self.macd_slow},"
            f"{self.macd_signal}), lookback={self.divergence_lookback}, "
            f"sma200={'ON' if self.sma200_filter else 'OFF'}, "
            f"atr_stop_mult={self.atr_stop_mult}, max_hold={self.max_hold_days}"
        )

    @property
    def name(self) -> str:
        return "macd_divergence"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Scan for bullish MACD divergence entry signals.

        Entry criteria:
          1. Price at new N-period low (vs divergence_lookback)
          2. MACD histogram higher than at the previous price low
          3. Histogram still negative or just crossing (catching the turn)
          4. SMA-200 above price (uptrend, if enabled)
          5. Volume adequate (not a thin, unconfirmed move)
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

        # Minimum rows: slow EMA + signal + divergence lookback + SMA-200
        min_rows = max(
            self.macd_slow + self.macd_signal + self.divergence_lookback + 10,
            200 + 10 if self.sma200_filter else 0,
            self.atr_period + 10,
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

                # ── SMA-200 uptrend filter ──────────────────────────────────
                if self.sma200_filter:
                    sma200 = close.rolling(200).mean()
                    if pd.isna(sma200.iloc[-1]) or close.iloc[-1] <= sma200.iloc[-1]:
                        continue

                # ── MACD calculation ────────────────────────────────────────
                ema_fast = close.ewm(span=self.macd_fast, adjust=False).mean()
                ema_slow = close.ewm(span=self.macd_slow, adjust=False).mean()
                macd_line = ema_fast - ema_slow
                signal_line = macd_line.ewm(span=self.macd_signal, adjust=False).mean()
                histogram = macd_line - signal_line

                if pd.isna(histogram.iloc[-1]) or pd.isna(histogram.iloc[-2]):
                    continue

                current_close = float(close.iloc[-1])
                current_hist = float(histogram.iloc[-1])

                # ── Divergence detection ────────────────────────────────────
                # Look at last divergence_lookback bars (excluding current)
                lookback = self.divergence_lookback
                if len(close) < lookback + 2:
                    continue

                close_window = close.iloc[-(lookback + 1):-1]
                hist_window = histogram.iloc[-(lookback + 1):-1]

                if close_window.empty or hist_window.empty:
                    continue

                # Find the previous local low (minimum close in window)
                min_loc = close_window.idxmin()
                prev_low_close = float(close_window[min_loc])
                prev_low_hist = float(hist_window[min_loc])

                # Condition 1: Current close is at/below the previous low
                if current_close >= prev_low_close:
                    continue  # No new low — no divergence

                # Condition 2: MACD histogram is higher than at previous low
                hist_delta = current_hist - prev_low_hist
                if hist_delta < self.min_hist_improvement:
                    continue  # Histogram not recovering — no divergence

                # Condition 3: Histogram is negative or just crossed zero
                # (we want to catch the reversal, not chase a confirmed trend)
                if current_hist > 0.05 * abs(current_close):
                    continue  # Already too far into positive — late signal

                # ── Volume check ────────────────────────────────────────────
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

                take_profit = None
                if self.profit_target_atr_mult > 0:
                    take_profit = entry_price + self.profit_target_atr_mult * atr_val

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
                # Base 0.65, bonuses for strength of divergence
                base_conf = 0.65
                # Stronger histogram recovery = higher confidence
                hist_bonus = min(hist_delta / max(abs(prev_low_hist), 0.0001) * 0.1, 0.10)
                # Deeper price decline = more oversold = better reversal setup
                price_depth = (prev_low_close - current_close) / prev_low_close
                depth_bonus = min(price_depth * 2.0, 0.10)
                # Volume above average adds confidence
                vol_bonus = min((vol_ratio.iloc[-1] - 1.0) * 0.05, 0.05) if vol_ratio.iloc[-1] > 1.0 else 0.0
                confidence = min(0.90, base_conf + hist_bonus + depth_bonus + vol_bonus)

                rationale = (
                    f"{ticker}: bullish MACD divergence — "
                    f"price low {current_close:.2f} < prev low {prev_low_close:.2f}, "
                    f"histogram {current_hist:.4f} > prev {prev_low_hist:.4f} "
                    f"(+{hist_delta:.4f}), "
                    f"vol_ratio={vol_ratio.iloc[-1]:.2f}"
                )

                signals.append(Signal(
                    ticker=ticker,
                    strategy=self.name,
                    direction="long",
                    entry_price=round(entry_price, 2),
                    stop_price=round(stop_price, 2),
                    take_profit=round(take_profit, 2) if take_profit else None,
                    position_size=shares,
                    position_value=round(pos["position_value"], 2),
                    risk_amount=round(pos["total_risk"], 2),
                    confidence=round(confidence, 3),
                    rationale=rationale,
                    features={
                        "macd_histogram": round(current_hist, 6),
                        "prev_low_histogram": round(prev_low_hist, 6),
                        "hist_delta": round(hist_delta, 6),
                        "price_depth_pct": round(price_depth * 100, 2),
                        "atr": round(atr_val, 3),
                        "vol_ratio": round(float(vol_ratio.iloc[-1]), 3),
                    },
                    market_id=self.config.get("market", "sp500"),
                ))

            except Exception as e:
                self._logger.warning(f"{ticker}: signal generation failed: {e}")
                continue

        # Sort by histogram recovery strength (strongest divergence first)
        signals.sort(
            key=lambda s: s.features.get("hist_delta", 0) if s.features else 0,
            reverse=True,
        )
        self._logger.info(
            f"MacdDivergence: {len(signals)} signals from {len(data)} tickers"
        )
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check exit conditions for held macd_divergence positions.

        Exit rules (priority order):
          1. Stop hit: price <= stop_price
          2. Profit target: price >= take_profit (if enabled)
          3. Time exit: held >= max_hold_days
          4. MACD signal exit: histogram crosses above zero (momentum confirmed)
        """
        exits = []
        for pos in positions:
            if pos.get("strategy") != self.name:
                continue
            ticker = pos.get("ticker")
            if not ticker or ticker not in data:
                continue

            df = data[ticker]
            if len(df) < self.macd_slow + self.macd_signal + 5:
                continue

            current_price = float(df["close"].iloc[-1])
            stop_price = pos.get("stop_price", 0.0)
            take_profit = pos.get("take_profit")
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

            # 2. Profit target
            elif take_profit and current_price >= take_profit:
                reason = "take_profit"
                details = (
                    f"{ticker} profit target: {current_price:.2f} >= {take_profit:.2f}, "
                    f"held {days_held}d"
                )

            # 3. Time exit
            elif days_held >= self.max_hold_days:
                reason = "time_exit"
                details = f"{ticker} time exit: held {days_held}d >= max {self.max_hold_days}"

            # 4. MACD histogram crosses above zero (divergence resolved, momentum restored)
            else:
                try:
                    close = df["close"]
                    ema_fast = close.ewm(span=self.macd_fast, adjust=False).mean()
                    ema_slow = close.ewm(span=self.macd_slow, adjust=False).mean()
                    macd_line = ema_fast - ema_slow
                    signal_line = macd_line.ewm(
                        span=self.macd_signal, adjust=False
                    ).mean()
                    histogram = macd_line - signal_line

                    if (
                        not pd.isna(histogram.iloc[-1])
                        and not pd.isna(histogram.iloc[-2])
                        and histogram.iloc[-2] <= 0
                        and histogram.iloc[-1] > 0
                    ):
                        reason = "signal_exit"
                        details = (
                            f"{ticker} MACD histogram crossed above zero "
                            f"({histogram.iloc[-1]:.4f}), held {days_held}d"
                        )
                except Exception:
                    pass

            if reason:
                exits.append({
                    "ticker": ticker,
                    "reason": reason,
                    "exit_price": current_price,
                    "details": details or reason,
                })

        self._logger.debug(
            f"MacdDivergence: {len(exits)} exit signals from {len(positions)} positions"
        )
        return exits


# Default parameter grid for optimization
PARAM_GRID = {
    "macd_fast": [8, 10, 12, 15],
    "macd_slow": [21, 26, 30],
    "divergence_lookback": [15, 20, 30],
    "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
    "max_hold_days": [5, 7, 10, 15],
    "sma200_filter": [True, False],
    "profit_target_atr_mult": [0.0, 1.5, 2.0, 2.5],
}
