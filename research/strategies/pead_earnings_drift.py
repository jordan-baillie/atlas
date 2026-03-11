"""
Atlas PEAD Earnings Drift Strategy
========================================
Post-Earnings Announcement Drift (PEAD): stocks with a large positive
price surprise continue drifting higher for 20-60 days after the event.

Reference: Ball & Brown (1968), Bernard & Thomas (1989),
           Jegadeesh & Livnat (2006), Quantpedia #22.
Academic basis: Market underreacts to earnings surprises. The abnormal
return following a positive earnings surprise is ~3-5% over 60 days,
even after transaction costs in large-cap names.

Since we don't have a real-time earnings calendar, we use a price proxy
for positive earnings surprise:
  - Large single-day positive return (> min_jump_pct)
  - High volume on that day (> vol_mult × average) — confirms unusual event
  - Closed in upper portion of the day's range (buyers in control)
  - Price above SMA-200 (pre-existing uptrend amplifies the drift)

The proxy captures: earnings beats, product launches, analyst upgrades,
contract wins — any large positive catalyst that the market underreacts to.

Hold 20-60 days (adjustable). ATR stop provides downside protection.

Config Section: strategies.pead_earnings_drift
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_rsi, calc_position_size, calc_volume_ratio

logger = logging.getLogger(__name__)


class PeadEarningsDrift(BaseStrategy):
    """Buy after large positive catalyst (earnings proxy), ride the drift."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("pead_earnings_drift", {})

        # Catalyst detection: minimum single-day price jump to qualify
        # (proxy for positive earnings/event surprise)
        self.min_jump_pct = strat_cfg.get("min_jump_pct", 0.03)   # 3% minimum

        # Volume confirmation: event day must have unusual volume
        self.vol_mult = strat_cfg.get("vol_mult", 2.0)
        self.vol_lookback = strat_cfg.get("vol_lookback", 20)

        # Intraday quality: stock must close in upper half of event day's range
        # (if sellers overwhelmed buyers on the event, skip it)
        self.min_event_ibs = strat_cfg.get("min_event_ibs", 0.5)

        # Entry timing: enter on the event day or up to N days after
        # (PEAD research shows drift starts immediately but lasts weeks)
        self.max_days_after_event = strat_cfg.get("max_days_after_event", 2)

        # Uptrend filter
        self.sma200_filter = strat_cfg.get("sma200_filter", True)

        # RSI cap: don't chase stocks already overbought after the jump
        self.rsi_max = strat_cfg.get("rsi_max", 75)
        self.rsi_period = strat_cfg.get("rsi_period", 14)

        # Risk management
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 2.0)
        self.max_hold_days = strat_cfg.get("max_hold_days", 30)  # PEAD drift ~30 days
        self.profit_target_atr_mult = strat_cfg.get("profit_target_atr_mult", 0.0)

        self._logger.info(
            f"PeadEarningsDrift initialized: min_jump={self.min_jump_pct:.1%}, "
            f"vol_mult={self.vol_mult}, max_days_after={self.max_days_after_event}, "
            f"sma200={'ON' if self.sma200_filter else 'OFF'}, "
            f"rsi_max={self.rsi_max}, atr_stop={self.atr_stop_mult}, "
            f"max_hold={self.max_hold_days}"
        )

    @property
    def name(self) -> str:
        return "pead_earnings_drift"

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Scan for post-earnings drift setups.

        Entry criteria (all must be met):
          1. Recent large single-day positive return (>= min_jump_pct)
             on very high volume (>= vol_mult × average) — event proxy
          2. Event day IBS >= min_event_ibs (buyers dominated, not reversal)
          3. Entry within max_days_after_event of the catalyst event
          4. RSI <= rsi_max (not already overbought post-event)
          5. Price > SMA-200 (uptrend amplifies PEAD, if enabled)
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

        min_rows = max(
            200 + 10 if self.sma200_filter else 0,
            self.vol_lookback + self.max_days_after_event + 10,
            self.rsi_period + 5,
            self.atr_period + 5,
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

                current_close = float(close.iloc[-1])

                # ── SMA-200 uptrend filter ──────────────────────────────────
                if self.sma200_filter:
                    sma200 = close.rolling(200).mean()
                    if pd.isna(sma200.iloc[-1]) or current_close <= sma200.iloc[-1]:
                        continue

                # ── Scan recent bars for catalyst event ─────────────────────
                # Look at today and up to max_days_after_event bars back
                # to find a qualifying positive jump on high volume
                search_window = self.max_days_after_event + 1  # today + N prior days
                found_event = False
                event_day_idx = None  # index position in df (relative, -1 = today)
                event_jump = 0.0
                event_vol_ratio = 0.0
                event_ibs = 0.0

                # Volume baseline: compute avg volume over vol_lookback
                # using data before the search window to avoid contamination
                vol_base_end = -(search_window)
                if abs(vol_base_end) > len(volume) - self.vol_lookback:
                    continue
                avg_vol = float(
                    volume.iloc[vol_base_end - self.vol_lookback: vol_base_end].mean()
                )
                if avg_vol <= 0:
                    continue

                # Search: iterate from oldest-in-window to newest
                for days_back in range(search_window - 1, -1, -1):
                    bar_idx = -(days_back + 1)  # -1 = today, -2 = yesterday, etc.

                    if abs(bar_idx) > len(close):
                        continue

                    bar_close = float(close.iloc[bar_idx])
                    bar_prev_close = float(close.iloc[bar_idx - 1])

                    if bar_prev_close <= 0:
                        continue

                    # Day return (close-to-close)
                    day_return = (bar_close - bar_prev_close) / bar_prev_close

                    # Skip if not a qualifying positive jump
                    if day_return < self.min_jump_pct:
                        continue

                    # Volume on event day
                    bar_vol = float(volume.iloc[bar_idx])
                    bar_vol_ratio = bar_vol / avg_vol

                    if bar_vol_ratio < self.vol_mult:
                        continue

                    # IBS on event day: buyers must dominate
                    bar_high = float(high.iloc[bar_idx])
                    bar_low = float(low.iloc[bar_idx])
                    bar_range = bar_high - bar_low
                    if bar_range <= 0:
                        continue
                    bar_ibs = (bar_close - bar_low) / bar_range

                    if bar_ibs < self.min_event_ibs:
                        continue

                    # Valid event found
                    found_event = True
                    event_day_idx = bar_idx
                    event_jump = day_return
                    event_vol_ratio = bar_vol_ratio
                    event_ibs = bar_ibs
                    # Use the most recent qualifying event (loop will overwrite)

                if not found_event:
                    continue

                # ── RSI cap: don't chase overbought ─────────────────────────
                rsi = calc_rsi(close, period=self.rsi_period)
                if rsi is None or pd.isna(rsi.iloc[-1]):
                    continue
                current_rsi = float(rsi.iloc[-1])
                if current_rsi > self.rsi_max:
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
                # Base 0.65: PEAD is a well-researched anomaly
                base_conf = 0.65
                # Larger jump = stronger catalyst signal
                jump_bonus = min((event_jump - self.min_jump_pct) * 2.0, 0.10)
                # Higher vol multiple = more unusual event = clearer signal
                vol_bonus = min((event_vol_ratio - self.vol_mult) * 0.02, 0.08)
                # Strong event IBS (buyers overwhelmed sellers)
                ibs_bonus = min((event_ibs - self.min_event_ibs) * 0.15, 0.08)
                confidence = min(0.90, base_conf + jump_bonus + vol_bonus + ibs_bonus)

                # Days since event (0 = same day, 1 = next day, etc.)
                days_since = abs(event_day_idx) - 1  # -1 → 0, -2 → 1, etc.

                rationale = (
                    f"{ticker}: PEAD setup — "
                    f"event jump={event_jump:.1%} "
                    f"({days_since}d ago), "
                    f"vol_ratio={event_vol_ratio:.1f}x, "
                    f"event_ibs={event_ibs:.2f}, "
                    f"RSI={current_rsi:.1f}"
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
                        "event_jump_pct": round(event_jump * 100, 2),
                        "event_vol_ratio": round(event_vol_ratio, 2),
                        "event_ibs": round(event_ibs, 3),
                        "days_since_event": days_since,
                        "rsi": round(current_rsi, 2),
                        "atr": round(atr_val, 3),
                    },
                    market_id=self.config.get("market", "sp500"),
                ))

            except Exception as e:
                self._logger.warning(f"{ticker}: signal generation failed: {e}")
                continue

        # Sort by event strength (jump * vol) — best catalyst first
        signals.sort(
            key=lambda s: (
                s.features.get("event_jump_pct", 0) *
                s.features.get("event_vol_ratio", 1)
                if s.features else 0
            ),
            reverse=True,
        )
        self._logger.info(
            f"PeadEarningsDrift: {len(signals)} signals from {len(data)} tickers"
        )
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check exit conditions for held pead_earnings_drift positions.

        Exit rules (priority order):
          1. Stop hit: price <= stop_price
          2. Profit target: price >= take_profit (if enabled)
          3. Time exit: held >= max_hold_days (PEAD drift exhausted ~30-60d)
          4. Momentum reversal: RSI drops below 40 (drift reversing)
        """
        exits = []
        for pos in positions:
            if pos.get("strategy") != self.name:
                continue
            ticker = pos.get("ticker")
            if not ticker or ticker not in data:
                continue

            df = data[ticker]
            if df.empty:
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

            # 3. Time exit — PEAD drift typically exhausted by max_hold_days
            elif days_held >= self.max_hold_days:
                reason = "time_exit"
                details = (
                    f"{ticker} PEAD drift time exit: "
                    f"held {days_held}d >= max {self.max_hold_days}"
                )

            # 4. Momentum reversal: RSI drops sharply (drift reversing)
            elif len(df["close"]) >= self.rsi_period + 5:
                try:
                    rsi = calc_rsi(df["close"], period=self.rsi_period)
                    if (
                        rsi is not None
                        and not pd.isna(rsi.iloc[-1])
                        and float(rsi.iloc[-1]) < 40.0
                        and days_held >= 5  # Give trade at least 5 days to work
                    ):
                        reason = "signal_exit"
                        details = (
                            f"{ticker} momentum reversal: "
                            f"RSI={rsi.iloc[-1]:.1f} < 40, held {days_held}d"
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
            f"PeadEarningsDrift: {len(exits)} exits from {len(positions)} positions"
        )
        return exits


# Default parameter grid for optimization
PARAM_GRID = {
    "min_jump_pct": [0.02, 0.03, 0.04, 0.05],
    "vol_mult": [1.5, 2.0, 2.5, 3.0],
    "min_event_ibs": [0.4, 0.5, 0.6, 0.7],
    "max_days_after_event": [1, 2, 3, 5],
    "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
    "max_hold_days": [15, 20, 30, 40, 60],
    "rsi_max": [70, 75, 80],
    "sma200_filter": [True, False],
    "profit_target_atr_mult": [0.0, 2.0, 3.0, 4.0],
}
