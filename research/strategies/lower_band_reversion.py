"""
Atlas Lower Band Reversion Strategy (Wave 4 — Sandbox)
========================================================
Mean reversion strategy based on the Quantitativo/Pagonidis IBS lower-band
research, adapted from SPY/QQQ ETF trading to individual S&P 500 stocks.

Published results (SPY, 25 years):
  - Sharpe 2.11, CAGR 13.0%, max DD 20.3%
  - 414 trades, 69% win rate, PF 1.98, avg return/trade 0.79%
  - Source: quantitativo.substack.com, reddit.com/r/algotrading

Core logic:
  1. Compute rolling mean of (High - Low) over `range_lookback` days (volatility proxy)
  2. Compute lower band = rolling High over `high_lookback` days - `band_mult` x range mean
  3. Buy when Close < lower band AND IBS < `ibs_threshold`
  4. Exit when Close > yesterday's High (strength confirmation exit)
  5. Fallback: time-based exit after `max_hold_days`

Key difference from existing strategies:
  - mean_reversion: RSI(14) + z-score entry, ATR-based stop + profit target exit
  - connors_rsi2: RSI(2) extreme, SMA(5) exit
  - lower_band_reversion: price-based band entry + IBS, close > prev high exit

Signal is fundamentally different — uses price range dynamics rather than
momentum oscillators. Expected to be uncorrelated with existing MR signals.

Adaptations for individual stocks (vs ETF original):
  - SMA-200 filter (only buy in uptrends, proven in wave 1)
  - ATR-based stop loss (individual stocks have higher tail risk than SPY)
  - Volume confirmation (institutional participation filter)
  - Position sizing via standard calc_position_size

Config Section: strategies.lower_band_reversion

Usage:
    from research.strategies.lower_band_reversion import LowerBandReversion
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_ibs, calc_position_size, calc_volume_ratio

logger = logging.getLogger(__name__)


class LowerBandReversion(BaseStrategy):
    """Lower band reversion: buy when price drops below volatility-adjusted band with low IBS."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("lower_band_reversion", {})

        # Core signal parameters (from published research)
        self.range_lookback = strat_cfg.get("range_lookback", 25)      # Rolling mean of H-L
        self.high_lookback = strat_cfg.get("high_lookback", 10)        # Rolling high period
        self.band_mult = strat_cfg.get("band_mult", 2.5)              # Band width multiplier
        self.ibs_threshold = strat_cfg.get("ibs_threshold", 0.3)      # Max IBS for entry

        # Exit parameters
        self.max_hold_days = strat_cfg.get("max_hold_days", 7)         # Time-based exit fallback

        # Risk management
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)      # Stop loss width

        # Filters (proven from prior waves)
        self.sma200_filter = strat_cfg.get("sma200_filter", True)      # Uptrend filter
        self.volume_entry_min = strat_cfg.get("volume_entry_min", 0.0) # Min vol ratio for entry

        # Volume config
        vol_cfg = strat_cfg.get("volume", {})
        self.vol_lookback = vol_cfg.get("lookback", 20)

        self._logger.info(
            f"LowerBandReversion initialized: range_lookback={self.range_lookback}, "
            f"high_lookback={self.high_lookback}, band_mult={self.band_mult}, "
            f"ibs_threshold={self.ibs_threshold}, max_hold={self.max_hold_days}, "
            f"sma200_filter={'ON' if self.sma200_filter else 'OFF'}"
        )

    @property
    def name(self) -> str:
        return "lower_band_reversion"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Scan all tickers for lower band reversion entry signals.

        A signal is generated when:
            1. Close < lower band (rolling high - band_mult * avg range)
            2. IBS < ibs_threshold (close near day's low = selling exhaustion)
            3. Price > SMA(200) if sma200_filter enabled
            4. Volume above minimum if volume_entry_min > 0
        """
        signals: List[Signal] = []
        held_tickers = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0008)
        min_position_value = self.fees_config.get("min_position_value", 0.0)
        max_position_value = self.config.get("trading", {}).get(
            "live_safety", {}
        ).get("max_order_value", 0.0)

        # Minimum rows needed for all indicators
        min_rows = max(self.range_lookback, self.high_lookback, self.atr_period, 200) + 10

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

                # ── SMA-200 trend filter ──
                if self.sma200_filter:
                    sma200 = close.rolling(200).mean()
                    sma200_val = sma200.iloc[-1]
                    if pd.isna(sma200_val) or close.iloc[-1] < sma200_val:
                        continue

                # ── Core signal: Lower Band ──
                # Step 1: Rolling mean of daily range (H-L) = volatility proxy
                daily_range = high - low
                avg_range = daily_range.rolling(self.range_lookback).mean()

                # Step 2: Rolling high over high_lookback days
                rolling_high = high.rolling(self.high_lookback).max()

                # Step 3: Lower band = rolling high - band_mult * avg_range
                lower_band = rolling_high - self.band_mult * avg_range

                current_close = close.iloc[-1]
                current_band = lower_band.iloc[-1]

                if pd.isna(current_band):
                    continue

                # Entry condition 1: Close below lower band
                if current_close >= current_band:
                    continue

                # ── IBS filter ──
                # Step 4: IBS must be low (close near day's low = selling exhaustion)
                ibs = calc_ibs(high, low, close)
                current_ibs = ibs.iloc[-1]

                if pd.isna(current_ibs) or current_ibs >= self.ibs_threshold:
                    continue

                # ── Volume filter ──
                if self.volume_entry_min > 0:
                    vol_ratio = calc_volume_ratio(volume, lookback=self.vol_lookback)
                    current_vol_ratio = vol_ratio.iloc[-1]
                    if pd.isna(current_vol_ratio) or current_vol_ratio < self.volume_entry_min:
                        continue

                # ── Position sizing ──
                atr = calc_atr(high, low, close, period=self.atr_period)
                current_atr = atr.iloc[-1]

                if pd.isna(current_atr) or current_atr <= 0:
                    continue

                entry_price = current_close
                stop_price = entry_price - self.atr_stop_mult * current_atr

                if stop_price <= 0 or stop_price >= entry_price:
                    continue

                # Use yesterday's high as initial take profit target
                # (close > yesterday's high is the original exit rule)
                prev_high = high.iloc[-2] if len(high) >= 2 else entry_price * 1.02
                take_profit = max(prev_high, entry_price + 0.5 * current_atr)

                risk_per_share = entry_price - stop_price
                pos_result = calc_position_size(
                    equity=equity,
                    risk_pct=risk_pct,
                    entry_price=entry_price,
                    stop_price=stop_price,
                    commission_per_trade=commission_per_trade,
                    commission_pct=commission_pct,
                    min_position_value=min_position_value,
                    max_position_value=max_position_value,
                )

                shares = pos_result["shares"]
                if shares <= 0:
                    continue

                position_value = shares * entry_price
                risk_amount = shares * risk_per_share

                # Confidence: based on how far below the band and how low IBS is
                band_depth = (current_band - current_close) / current_atr  # How deep below band
                ibs_strength = 1.0 - (current_ibs / self.ibs_threshold)    # How low IBS is
                confidence = min(0.95, 0.70 + 0.10 * min(band_depth, 1.5) + 0.10 * ibs_strength)

                rationale = (
                    f"LBR: close={current_close:.2f} < band={current_band:.2f} "
                    f"(depth={band_depth:.2f}x ATR), IBS={current_ibs:.3f}, "
                    f"stop={stop_price:.2f}, target={take_profit:.2f}"
                )

                signals.append(Signal(
                    ticker=ticker,
                    strategy=self.name,
                    direction="long",
                    entry_price=entry_price,
                    stop_price=stop_price,
                    take_profit=take_profit,
                    position_size=shares,
                    position_value=position_value,
                    risk_amount=risk_amount,
                    confidence=confidence,
                    rationale=rationale,
                    features={
                        "close": float(current_close),
                        "lower_band": float(current_band),
                        "band_depth_atr": float(band_depth),
                        "ibs": float(current_ibs),
                        "atr": float(current_atr),
                        "prev_high": float(prev_high),
                    },
                    market_id=self.config.get("market", "sp500"),
                ))

            except Exception as e:
                self._logger.error(f"{ticker}: signal generation error: {e}")
                continue

        self._logger.info(
            f"LowerBandReversion: scanned {len(data)} tickers, "
            f"generated {len(signals)} signals"
        )
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check open LBR positions for exit conditions.

        Exit rules (in priority order):
            1. Stop loss: price <= stop_price
            2. Strength exit: close > yesterday's high (original published exit)
            3. Time exit: held >= max_hold_days
        """
        exits: List[Dict[str, Any]] = []

        for pos in positions:
            if pos.get("strategy") != self.name:
                continue

            ticker = pos["ticker"]
            df = data.get(ticker)

            if df is None or df.empty or len(df) < 2:
                self._logger.warning(f"{ticker}: insufficient data for exit check")
                continue

            try:
                current_close = df["close"].iloc[-1]
                current_low = df["low"].iloc[-1]
                prev_high = df["high"].iloc[-2]
                entry_price = pos["entry_price"]
                stop_price = pos.get("stop_price", 0)
                entry_date = pd.Timestamp(pos["entry_date"])
                today_date = df.index[-1]
                days_held = (today_date - entry_date).days

                # 1. Stop loss
                if current_low <= stop_price:
                    exits.append({
                        "ticker": ticker,
                        "reason": "stop_hit",
                        "exit_price": stop_price,
                        "details": (
                            f"{ticker} hit stop at ${stop_price:.2f}. "
                            f"Low=${current_low:.2f}, entry=${entry_price:.2f}, "
                            f"held {days_held}d"
                        ),
                    })
                    continue

                # 2. Strength exit: close > yesterday's high (original published rule)
                if current_close > prev_high:
                    pnl_pct = (current_close - entry_price) / entry_price * 100
                    exits.append({
                        "ticker": ticker,
                        "reason": "strength_exit",
                        "exit_price": current_close,
                        "details": (
                            f"{ticker} strength exit: close=${current_close:.2f} > "
                            f"prev_high=${prev_high:.2f}. "
                            f"Entry=${entry_price:.2f}, P&L={pnl_pct:+.1f}%, "
                            f"held {days_held}d"
                        ),
                    })
                    continue

                # 3. Time-based exit
                if days_held >= self.max_hold_days:
                    pnl_pct = (current_close - entry_price) / entry_price * 100
                    exits.append({
                        "ticker": ticker,
                        "reason": "time_exit",
                        "exit_price": current_close,
                        "details": (
                            f"{ticker} time exit: held {days_held}d >= "
                            f"{self.max_hold_days}d max. "
                            f"Entry=${entry_price:.2f}, close=${current_close:.2f}, "
                            f"P&L={pnl_pct:+.1f}%"
                        ),
                    })
                    continue

            except Exception as e:
                self._logger.error(f"{ticker}: exit check error: {e}")
                continue

        return exits
