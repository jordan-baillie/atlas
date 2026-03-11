"""
Atlas Put Call Vix Proxy Strategy
========================================
VIX > 30 or VIX spike > 20% in 1 day -> buy SPY/broad market. Exit when VIX drops below 20. Contrarian sentiment play.

Reference: VIX fear gauge research, CBOE put/call ratio studies
Generated: 2026-03-10T07:18:31.224972+00:00

Config Section: strategies.put_call_vix_proxy
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size

logger = logging.getLogger(__name__)

# Standard VIX ticker keys to look for in data dict
VIX_TICKERS = ["^VIX", "VIX", "VIXY", "VXX"]


class PutCallVixProxy(BaseStrategy):
    """Buy oversold stocks/market when VIX spikes — contrarian fear gauge strategy."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("put_call_vix_proxy", {})

        # VIX thresholds
        self.vix_fear_threshold = strat_cfg.get("vix_fear_threshold", 30.0)    # VIX above this = fear
        self.vix_spike_pct = strat_cfg.get("vix_spike_pct", 0.20)              # 1-day VIX spike %
        self.vix_calm_threshold = strat_cfg.get("vix_calm_threshold", 20.0)    # Exit when VIX drops here

        # Stock-level filters
        self.rsi_period = strat_cfg.get("rsi_period", 14)
        self.rsi_oversold = strat_cfg.get("rsi_oversold", 40)    # Stock must be oversold
        self.sma200_filter = strat_cfg.get("sma200_filter", True)

        # Risk management
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.5)   # Wider stops during fear
        self.max_hold_days = strat_cfg.get("max_hold_days", 20)

        self._logger.info(
            f"PutCallVixProxy initialized: vix_fear={self.vix_fear_threshold}, "
            f"vix_spike={self.vix_spike_pct:.0%}, vix_calm={self.vix_calm_threshold}, "
            f"rsi_oversold={self.rsi_oversold}, max_hold={self.max_hold_days}d"
        )

    @property
    def name(self) -> str:
        return "put_call_vix_proxy"

    def _get_vix_data(self, data: Dict[str, pd.DataFrame]) -> Optional[pd.DataFrame]:
        """Extract VIX DataFrame from data dict. Tries multiple ticker names."""
        for vix_key in VIX_TICKERS:
            if vix_key in data:
                df = data[vix_key]
                if not df.empty and len(df) >= 2:
                    return df
        return None

    def _is_vix_fear_regime(self, vix_df: pd.DataFrame) -> bool:
        """Check if VIX is in a fear regime (high absolute level or sharp spike)."""
        vix_close = vix_df["close"]
        current_vix = float(vix_close.iloc[-1])
        prev_vix = float(vix_close.iloc[-2])

        # Condition 1: VIX above absolute fear threshold
        if current_vix >= self.vix_fear_threshold:
            return True

        # Condition 2: VIX spiked sharply in one day (panic spike)
        if prev_vix > 0:
            vix_change = (current_vix - prev_vix) / prev_vix
            if vix_change >= self.vix_spike_pct:
                return True

        return False

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Generate VIX-based contrarian entry signals.

        Entry criteria:
          1. VIX is in a fear regime (above vix_fear_threshold OR spiked by vix_spike_pct)
          2. Individual stock is oversold (RSI below rsi_oversold)
          3. Stock is in a broader uptrend (above SMA-200, if enabled)
          4. ATR-based position sizing
        """
        signals: List[Signal] = []
        held = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0008)

        # Check VIX data availability
        vix_df = self._get_vix_data(data)
        if vix_df is None:
            # Fallback: use market-wide drawdown as VIX proxy
            # If no VIX data, generate no signals (strategy is specifically VIX-based)
            self._logger.debug(f"{self.name}: no VIX data in data dict — no signals generated")
            return signals

        # Check if we are in a fear regime
        if not self._is_vix_fear_regime(vix_df):
            self._logger.debug(f"{self.name}: VIX not in fear regime — no signals")
            return signals

        current_vix = float(vix_df["close"].iloc[-1])
        prev_vix = float(vix_df["close"].iloc[-2])
        vix_spike = (current_vix - prev_vix) / prev_vix if prev_vix > 0 else 0.0

        min_rows = max(
            200 if self.sma200_filter else 0,
            self.rsi_period + 5,
            self.atr_period + 5,
        )

        for ticker, df in data.items():
            # Skip VIX itself
            if ticker in VIX_TICKERS:
                continue

            try:
                if ticker in held:
                    continue
                if not self._can_open_position(existing_positions):
                    break
                if not self._has_sufficient_data(df, min_rows):
                    continue

                close = df["close"]
                high = df["high"]
                low = df["low"]
                current_close = float(close.iloc[-1])

                # SMA-200 uptrend filter: buy dips in uptrending stocks only
                if self.sma200_filter:
                    sma200 = float(close.rolling(200).mean().iloc[-1])
                    if pd.isna(sma200) or current_close < sma200:
                        continue

                # RSI oversold: stock must be showing fear/weakness
                rsi = calc_rsi(close, period=self.rsi_period)
                current_rsi = float(rsi.iloc[-1])
                if pd.isna(current_rsi) or current_rsi >= self.rsi_oversold:
                    continue

                # ATR-based stop and position sizing
                atr = calc_atr(high, low, close, self.atr_period)
                atr_val = float(atr.iloc[-1])
                if pd.isna(atr_val) or atr_val <= 0:
                    continue

                entry_price = current_close
                # Wider stops during high VIX (more volatility)
                effective_stop_mult = self.atr_stop_mult * (1.0 + min(0.5, (current_vix - 15) / 60))
                stop_price = entry_price - effective_stop_mult * atr_val

                if stop_price <= 0 or stop_price >= entry_price:
                    continue

                # No fixed take-profit — exit when VIX calms
                take_profit = None

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
                # Higher VIX = more fear = better contrarian setup
                vix_conf = min(0.15, (current_vix - self.vix_fear_threshold) / 30.0 * 0.15)
                # Deeper RSI oversold = stronger confirmation
                rsi_conf = min(0.10, (self.rsi_oversold - current_rsi) / 30.0 * 0.10)
                confidence = round(min(0.95, 0.65 + vix_conf + rsi_conf), 4)

                rationale = (
                    f"{ticker}: VIX fear play — VIX={current_vix:.1f} "
                    + (f"(spike={vix_spike:+.1%})" if abs(vix_spike) >= 0.05 else "")
                    + f", stock RSI={current_rsi:.1f} (oversold). "
                    f"Entry={entry_price:.2f}, stop={stop_price:.2f} "
                    f"(ATR x {effective_stop_mult:.1f}). Exit when VIX < {self.vix_calm_threshold}."
                )

                signals.append(Signal(
                    ticker=ticker,
                    strategy=self.name,
                    direction="long",
                    entry_price=entry_price,
                    stop_price=round(stop_price, 4),
                    take_profit=take_profit,
                    position_size=pos["shares"],
                    position_value=pos["position_value"],
                    risk_amount=pos["total_risk"],
                    confidence=confidence,
                    rationale=rationale,
                    features={
                        "vix": round(current_vix, 2),
                        "vix_spike": round(vix_spike, 4),
                        "rsi": round(current_rsi, 2),
                        "atr": round(atr_val, 4),
                        "atr_stop_mult_used": round(effective_stop_mult, 2),
                        "close": round(current_close, 4),
                    },
                    timestamp=datetime.now(),
                ))

            except Exception as e:
                self._logger.error(f"{ticker}: error in put_call_vix_proxy signal gen: {e}", exc_info=True)
                continue

        self._logger.info(f"{self.name}: {len(signals)} signals (VIX={current_vix:.1f}) from {len(data)} tickers")
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check positions for exit conditions.

        Exit priority:
          1. Stop-loss hit
          2. VIX calmed down (drops below vix_calm_threshold) — fear resolved
          3. Time exit
        """
        exits = []

        # Check current VIX level for signal exits
        vix_df = self._get_vix_data(data)
        current_vix = None
        if vix_df is not None and not vix_df.empty:
            current_vix = float(vix_df["close"].iloc[-1])

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

                # 1. Stop-loss
                if stop_price and current_price <= stop_price:
                    exits.append({
                        "ticker": ticker,
                        "reason": "stop_hit",
                        "exit_price": current_price,
                        "details": f"Price {current_price:.2f} <= stop {stop_price:.2f}",
                    })
                    continue

                # 2. VIX calmed: fear resolved — exit the contrarian trade
                if current_vix is not None and current_vix < self.vix_calm_threshold:
                    exits.append({
                        "ticker": ticker,
                        "reason": "signal_exit",
                        "exit_price": current_price,
                        "details": f"VIX calmed to {current_vix:.1f} < calm threshold {self.vix_calm_threshold}",
                    })
                    continue

                # 3. Time exit
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
    "vix_fear_threshold": [25.0, 30.0, 35.0],
    "vix_spike_pct": [0.15, 0.20, 0.25],
    "vix_calm_threshold": [18.0, 20.0, 22.0],
    "rsi_oversold": [35, 40, 45],
    "atr_stop_mult": [2.0, 2.5, 3.0],
    "max_hold_days": [10, 15, 20],
}
