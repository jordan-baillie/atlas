"""Atlas Ex-Dividend Capture Strategy
========================================================
Buys stocks approaching ex-dividend dates to capture the dividend payment
and benefit from pre-dividend run-up. Falls back to a value/yield proxy
strategy when no live dividend data is available.

Primary mode (dividend data available):
  1. Check for upcoming ex-dividend dates (within days_before_ex days)
  2. Enter N days before ex-date to capture pre-dividend run-up
  3. Hold through ex-date to receive dividend
  4. Exit M days after ex-date (post-ex recovery captured)

Fallback mode (no dividend data):
  - Use 52-week low proximity as a value proxy (cheap stock setup)
  - RSI oversold + near 52-week low = "priced like value"
  - Catch undervalued stocks that may offer implicit yield

Reference: Beggs & Skeels (2006) ASX franking credit research,
           dividend capture strategies in tax-aware investing.

Config Section: strategies.dividend_capture
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_position_size, calc_rsi
from utils.dividends import (
    fetch_dividend_calendar,
    estimate_franking_pct,
    calc_grossed_up_yield,
    get_sector_for_ticker,
)

logger = logging.getLogger(__name__)


class DividendCapture(BaseStrategy):
    """Ex-dividend capture: enter before ex-date, hold through for dividend + recovery.

    Falls back to value proxy (near 52-week low + RSI oversold) when
    live dividend calendar data is unavailable.
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("dividend_capture", {})

        # Entry timing
        self.days_before_ex = strat_cfg.get("days_before_ex", 5)       # Enter N days before ex-date
        self.days_after_ex = strat_cfg.get("days_after_ex", 5)         # Exit M days after ex-date

        # Dividend filters
        self.min_grossed_up_yield = strat_cfg.get("min_grossed_up_yield", 0.015)  # 1.5% min yield
        self.min_franking_pct = strat_cfg.get("min_franking_pct", 75) / 100.0     # 75% franked

        # Quality filters
        self.rsi_max = strat_cfg.get("rsi_max", 70)              # Not overbought at entry
        self.sma200_filter = strat_cfg.get("sma200_filter", False)  # Loose for income stocks

        # Fallback value proxy parameters
        self.use_fallback = strat_cfg.get("use_fallback", True)          # Enable fallback
        self.fallback_rsi_oversold = strat_cfg.get("fallback_rsi_oversold", 40)  # RSI below
        self.fallback_52w_low_pct = strat_cfg.get("fallback_52w_low_pct", 0.20)  # Within 20% of 52w low

        # Risk management
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 3.0)    # Wider stop (event-driven)
        self.max_hold_days = strat_cfg.get("max_hold_days", 20)

        # Cache to avoid repeated API calls per run
        self._div_cache: Dict[str, List[Dict]] = {}

        self._logger.info(
            f"DividendCapture initialized: {self.days_before_ex}d before ex, "
            f"{self.days_after_ex}d after ex, min_yield={self.min_grossed_up_yield:.1%}, "
            f"fallback={'ON' if self.use_fallback else 'OFF'}"
        )

    @property
    def name(self) -> str:
        return "dividend_capture"

    def _get_dividends(self, ticker: str) -> List[Dict]:
        """Fetch dividend calendar with in-run caching."""
        if ticker not in self._div_cache:
            try:
                self._div_cache[ticker] = fetch_dividend_calendar(ticker)
            except Exception as e:
                self._logger.debug(f"{ticker}: dividend fetch failed: {e}")
                self._div_cache[ticker] = []
        return self._div_cache[ticker]

    def _find_upcoming_exdate(
        self, ticker: str, current_date: pd.Timestamp
    ) -> Optional[Dict]:
        """Find the next ex-dividend date within days_before_ex window."""
        dividends = self._get_dividends(ticker)
        if not dividends:
            return None

        lookahead = pd.Timedelta(days=self.days_before_ex + 2)
        for div in dividends:
            try:
                ex_dt = pd.Timestamp(div["ex_date"]).normalize()
                days_until = (ex_dt - current_date).days
                if 0 <= days_until <= self.days_before_ex:
                    return div
            except Exception:
                continue
        return None

    def _is_post_exdate_exit(
        self, ticker: str, entry_date: pd.Timestamp, current_date: pd.Timestamp
    ) -> Optional[str]:
        """Check if we should exit because we are M days after the ex-date."""
        dividends = self._get_dividends(ticker)
        for div in dividends:
            try:
                ex_dt = pd.Timestamp(div["ex_date"]).normalize()
                # Ex-date must be after entry
                if ex_dt < entry_date:
                    continue
                days_after = (current_date - ex_dt).days
                if days_after >= self.days_after_ex:
                    return f"Post-ex exit: {days_after}d after ex-date {div['ex_date']}"
            except Exception:
                continue
        return None

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Generate dividend capture entry signals.

        Mode 1 (dividend data): Enter N days before ex-date if yield qualifies.
        Mode 2 (fallback): Enter value proxy (near 52-week low + RSI oversold).
        """
        signals: List[Signal] = []
        held = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        commission_pct = self.fees_config.get("commission_pct", 0.0008)

        min_rows = max(
            200 if self.sma200_filter else 50,
            252 + 5,   # Need 1 year for 52-week low in fallback
            self.rsi_max + 5,
            self.atr_period + 5,
        )

        for ticker, df in data.items():
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
                current_date = df.index[-1]

                # SMA-200 filter (if enabled — loose for income stocks)
                if self.sma200_filter:
                    sma200 = float(close.rolling(200).mean().iloc[-1])
                    if pd.isna(sma200) or current_close < sma200:
                        continue

                # RSI — must not be overbought at entry
                rsi = calc_rsi(close, period=14)
                current_rsi = float(rsi.iloc[-1])
                if pd.isna(current_rsi) or current_rsi > self.rsi_max:
                    continue

                # ATR for position sizing
                atr = calc_atr(high, low, close, self.atr_period)
                atr_val = float(atr.iloc[-1])
                if pd.isna(atr_val) or atr_val <= 0:
                    continue

                entry_triggered = False
                signal_features: Dict[str, Any] = {}
                signal_confidence = 0.70
                signal_rationale = ""

                # ── Mode 1: Dividend calendar data ──────────────────────────
                upcoming = self._find_upcoming_exdate(ticker, current_date)
                if upcoming is not None:
                    div_amount = float(upcoming.get("amount", 0))
                    if div_amount > 0:
                        sector = get_sector_for_ticker(ticker)
                        franking_pct = estimate_franking_pct(ticker, sector)

                        # Apply franking filter
                        if franking_pct < self.min_franking_pct:
                            pass  # Fall through to fallback
                        else:
                            grossed_up_yield = calc_grossed_up_yield(
                                dividend_amount=div_amount,
                                share_price=current_close,
                                franking_pct=franking_pct,
                            )
                            if grossed_up_yield >= self.min_grossed_up_yield:
                                days_until_ex = (pd.Timestamp(upcoming["ex_date"]) - current_date).days
                                entry_triggered = True
                                signal_confidence = round(min(0.92, 0.70 + min(0.15, grossed_up_yield * 3)), 4)
                                signal_features = {
                                    "mode": "dividend_capture",
                                    "ex_date": upcoming["ex_date"],
                                    "div_amount": round(div_amount, 4),
                                    "franking_pct": round(franking_pct, 2),
                                    "grossed_up_yield": round(grossed_up_yield, 4),
                                    "days_until_ex": days_until_ex,
                                    "rsi": round(current_rsi, 2),
                                    "atr": round(atr_val, 4),
                                    "close": round(current_close, 4),
                                }
                                signal_rationale = (
                                    f"{ticker}: Dividend capture — ex-date {upcoming['ex_date']} in {days_until_ex}d, "
                                    f"div={div_amount:.4f}, grossed-up yield={grossed_up_yield:.1%} "
                                    f"(franking={franking_pct:.0%}). RSI={current_rsi:.1f}."
                                )

                # ── Mode 2: Fallback value proxy ────────────────────────────
                if not entry_triggered and self.use_fallback:
                    # Value proxy: stock near 52-week low (cheap) + oversold RSI
                    if len(close) >= 252:
                        low_52w = float(close.rolling(252).min().iloc[-1])
                        high_52w = float(close.rolling(252).max().iloc[-1])

                        if pd.isna(low_52w) or low_52w <= 0:
                            continue

                        # Within fallback_52w_low_pct of 52-week low
                        pct_from_low = (current_close - low_52w) / low_52w
                        if pct_from_low <= self.fallback_52w_low_pct and current_rsi <= self.fallback_rsi_oversold:
                            entry_triggered = True
                            # Confidence: closer to 52w low + more oversold = better
                            low_conf = min(0.10, (1 - pct_from_low / self.fallback_52w_low_pct) * 0.10)
                            rsi_conf = min(0.10, (self.fallback_rsi_oversold - current_rsi) / 30.0 * 0.10)
                            signal_confidence = round(min(0.85, 0.65 + low_conf + rsi_conf), 4)
                            signal_features = {
                                "mode": "value_proxy",
                                "pct_from_52w_low": round(pct_from_low, 4),
                                "low_52w": round(low_52w, 4),
                                "high_52w": round(high_52w, 4),
                                "rsi": round(current_rsi, 2),
                                "atr": round(atr_val, 4),
                                "close": round(current_close, 4),
                            }
                            signal_rationale = (
                                f"{ticker}: Value proxy (no dividend data) — price={current_close:.2f} "
                                f"is {pct_from_low:.1%} above 52w-low={low_52w:.2f}, "
                                f"RSI={current_rsi:.1f} (oversold). Likely cheap/value stock."
                            )

                if not entry_triggered:
                    continue

                entry_price = current_close
                stop_price = entry_price - self.atr_stop_mult * atr_val
                # No fixed take-profit: exit by time (post-ex) or stop
                take_profit = None

                if stop_price <= 0 or stop_price >= entry_price:
                    continue

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
                    confidence=signal_confidence,
                    rationale=signal_rationale,
                    features=signal_features,
                    timestamp=datetime.now(),
                ))

            except Exception as e:
                self._logger.error(f"{ticker}: error in dividend_capture signal gen: {e}", exc_info=True)
                continue

        self._logger.info(f"{self.name}: {len(signals)} signals from {len(data)} tickers")
        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check positions for exit conditions.

        Exit priority:
          1. Stop-loss hit
          2. Post-ex-date exit (M days after ex-date — dividend captured)
          3. Time exit (max_hold_days)
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

            try:
                current_price = float(df["close"].iloc[-1])
                stop_price = pos.get("stop_price", 0)
                current_date = df.index[-1]

                # 1. Stop-loss
                if stop_price and current_price <= stop_price:
                    exits.append({
                        "ticker": ticker,
                        "reason": "stop_hit",
                        "exit_price": current_price,
                        "details": f"Price {current_price:.2f} <= stop {stop_price:.2f}",
                    })
                    continue

                # 2. Post-ex-date exit: dividend has been captured, exit per plan
                entry_date = pos.get("entry_date")
                if entry_date:
                    if isinstance(entry_date, str):
                        entry_date = pd.Timestamp(entry_date)

                    post_ex_reason = self._is_post_exdate_exit(ticker, entry_date, current_date)
                    if post_ex_reason:
                        exits.append({
                            "ticker": ticker,
                            "reason": "signal_exit",
                            "exit_price": current_price,
                            "details": post_ex_reason,
                        })
                        continue

                    # 3. Time exit (fallback: no dividend data or max hold reached)
                    days_held = (current_date - entry_date).days
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
    "days_before_ex": [3, 5, 7],
    "days_after_ex": [3, 5, 7],
    "min_grossed_up_yield": [0.010, 0.015, 0.020],
    "atr_stop_mult": [2.5, 3.0, 3.5],
    "max_hold_days": [15, 20, 25],
    "fallback_52w_low_pct": [0.10, 0.20, 0.30],
    "fallback_rsi_oversold": [35, 40, 45],
}
