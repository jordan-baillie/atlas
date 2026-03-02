"""
Atlas Walk-Forward Backtesting Engine
==========================================
Simulates strategy execution over historical data using walk-forward
analysis to avoid look-ahead bias.

Walk-Forward Process:
    1. Train window (252 days) - strategies can use this data for calibration
    2. Test window (63 days) - simulate trading on out-of-sample data
    3. Step forward (21 days) - slide the window and repeat

Execution Model:
    - Signals generated on day T close
    - Orders filled at day T+1 open (market-on-open)
    - Commission: percentage-based for small accounts, max(flat, pct) for large
    - Slippage: applied to entry and exit prices
    - Positions carry across walk-forward windows (no artificial force-close)

Risk Enforcement:
    - Max positions, sector concentration, per-trade risk limits
    - Minimum position value filter
    - All enforced during simulation

Usage:
    from backtest.engine import BacktestEngine, BacktestResult
    engine = BacktestEngine(config)
    result = engine.run_walkforward(data, strategies)
"""

import logging
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from backtest.metrics import calc_all_metrics, calc_r_multiple
from data.ingest import download_ticker  # Phase1-Fix1: load benchmark independently
from strategies.base import BaseStrategy, Signal
from utils.market_breadth import MarketBreadth
from utils.relative_strength import RelativeStrength
from utils.dynamic_sizing import DynamicSizer

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """Container for backtest results."""

    trades: List[Dict[str, Any]] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    metrics: Dict[str, Any] = field(default_factory=dict)
    benchmark_metrics: Dict[str, Any] = field(default_factory=dict)
    walk_forward_windows: List[Dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        """Return a human-readable summary of backtest results."""
        lines = [
            "=" * 60,
            "ATLAS BACKTEST RESULTS",
            "=" * 60,
            f"Total Trades:    {self.metrics.get('total_trades', 0)}",
            f"Total P&L:       ${self.metrics.get('total_pnl', 0):,.2f}",
            f"CAGR:            {self.metrics.get('cagr', 0)*100:.2f}%",
            f"Max Drawdown:    {self.metrics.get('max_drawdown', 0)*100:.2f}%",
            f"Sharpe Ratio:    {self.metrics.get('sharpe', 0):.4f}",
            f"Sortino Ratio:   {self.metrics.get('sortino', 0):.4f}",
            f"Win Rate:        {self.metrics.get('win_rate', 0)*100:.1f}%",
            f"Profit Factor:   {self.metrics.get('profit_factor', 0):.2f}",
            f"Avg Trade:       ${self.metrics.get('avg_trade', 0):,.2f}",
            f"Exposure:        {self.metrics.get('exposure', 0)*100:.1f}%",
            f"Final Equity:    ${self.metrics.get('final_equity', 0):,.2f}",
            f"Calmar Ratio:    {self.metrics.get('calmar', 0):.4f}",
            "-" * 60,
            "RISK METRICS",
            "-" * 60,
            f"VaR 95% (hist):  {self.metrics.get('var_95', 0)*100:.2f}%",
            f"VaR 99% (hist):  {self.metrics.get('var_99', 0)*100:.2f}%",
            f"CVaR 95% (ES):   {self.metrics.get('cvar_95', 0)*100:.2f}%",
            f"VaR 95% (param): {self.metrics.get('var_95_parametric', 0)*100:.2f}%",
            f"MC p95 Drawdown: {self.metrics.get('mc_p95_drawdown', 0)*100:.2f}%"
            f"{'  ⚠ FRAGILE' if self.metrics.get('mc_fragile') else ''}",
            "-" * 60,
            "EDGE (R-Multiples)",
            "-" * 60,
            f"Expectancy (R):  {self.metrics.get('expectancy_r', 0):+.4f}",
            f"Avg R:           {self.metrics.get('avg_r', 0):+.4f}",
            f"R Count:         {self.metrics.get('r_count', 0)}",
            f"Edge p-value:    {self.metrics.get('edge_p_value', 1.0):.4f}"
            f"{'  ✓ significant' if self.metrics.get('edge_significant') else '  ✗ not significant'}",
            "-" * 60,
            "BENCHMARK (Buy & Hold)",
            "-" * 60,
            f"CAGR:            {self.benchmark_metrics.get('cagr', 0)*100:.2f}%",
            f"Max Drawdown:    {self.benchmark_metrics.get('max_drawdown', 0)*100:.2f}%",
            f"Sharpe Ratio:    {self.benchmark_metrics.get('sharpe', 0):.4f}",
            f"Total Return:    {self.benchmark_metrics.get('total_return', 0)*100:.2f}%",
            "=" * 60,
        ]
        # Strategy correlation warnings
        corr = self.metrics.get("strategy_correlation", {})
        pairs = corr.get("concentrated_pairs", [])
        if pairs:
            lines.append("⚠ CONCENTRATED RISK (|r| > 0.6):")
            for a, b, r in pairs:
                lines.append(f"  {a} ↔ {b}: r={r:+.3f}")
            lines.append("=" * 60)
        return "\n".join(lines)


class BacktestEngine:
    """Walk-forward backtesting engine for Atlas strategies."""

    def __init__(self, config: Dict[str, Any], market_id: Optional[str] = None):
        self.config = config
        self.market_id = market_id or config.get("market", "asx")
        self.risk_config = config.get("risk", {})
        self.fees_config = config.get("fees", {})
        self.backtest_config = config.get("backtest", {})
        self.trading_config = config.get("trading", {})

        # Walk-forward parameters — derive defaults from market profile
        try:
            from markets import get_market
            _bt_defaults = get_market(self.market_id).get_backtest_defaults()
        except (ImportError, KeyError):
            _bt_defaults = {
                "train_window_days": 252, "test_window_days": 63,
                "step_days": 21, "min_history_days": 60,
            }
        self.train_window = self.backtest_config.get("train_window_days", _bt_defaults["train_window_days"])
        self.test_window = self.backtest_config.get("test_window_days", _bt_defaults["test_window_days"])
        self.step_days = self.backtest_config.get("step_days", _bt_defaults["step_days"])
        self.min_history = self.backtest_config.get("min_history_days", _bt_defaults["min_history_days"])

        # Risk parameters
        self.starting_equity = self.risk_config.get("starting_equity", 5000)
        self.max_positions = self.risk_config.get("max_open_positions", 5)

        # Trailing stop configuration
        _trail_cfg = config.get("risk", {}).get("trailing_stop", {})
        self.trailing_stop_enabled = _trail_cfg.get("enabled", False)
        self.trail_activation_pct = _trail_cfg.get("activation_pct", 0.03)   # activate at +3%
        self.trail_atr_multiplier = _trail_cfg.get("atr_multiplier", 1.5)    # trail 1.5×ATR below peak
        self.max_sector = self.risk_config.get("max_sector_concentration", 2)
        self.max_risk_per_trade = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        # Phase 9A: Portfolio-wide max loss cap per trade (dollars)
        self.max_loss_per_trade = self.risk_config.get("max_loss_per_trade", None)  # e.g. 35.0
        # Phase 8D: Dynamic position sizing
        self.dynamic_sizer = DynamicSizer(config)

        # Fee parameters - Phase1-Fix2: smart commission model
        self.commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        self.commission_pct = self.fees_config.get("commission_pct", 0.0008)
        self.slippage_pct = self.fees_config.get("slippage_pct", 0.001)
        # Threshold below which flat fee is waived (pct-only mode)
        self.flat_fee_threshold = self.fees_config.get("flat_fee_threshold", 2000.0)
        # Minimum position value to avoid commission-dominated micro-trades
        self.min_position_value = self.fees_config.get("min_position_value", 500.0)
        # Maximum position value (e.g. for equal-weight allocation with small equity)
        self.max_position_value = self.config.get("trading", {}).get(
            "live_safety", {}
        ).get("max_order_value", 0)

        # Phase1-Fix5: Minimum confidence threshold
        self.min_confidence = self.risk_config.get("min_confidence", 0.0)

        # Fee-Aware Signal Filter config
        _faf = config.get("fee_aware_filter", {})
        self.fee_aware_enabled = _faf.get("enabled", False)
        self.fee_aware_rrr = _faf.get("reward_risk_ratio", 1.5)  # default reward:risk if no TP
        self.fee_aware_min_pnl = _faf.get("min_expected_pnl", 0.0)  # min expected net PnL

        # VIX regime filter — skip entries when VIX is above threshold
        _vix_cfg = config.get("vix_filter", {})
        self.vix_filter_enabled = _vix_cfg.get("enabled", False)
        self.vix_max_entry = _vix_cfg.get("max_entry", 30.0)  # skip entries when VIX > this

        # FRED macro regime filter — skip entries during adverse macro conditions
        # Uses yield curve slope (T10Y2Y), unemployment claims (ICSA), fed funds
        # Requires fred_api_key in ~/.atlas-secrets.json
        _fred_cfg = config.get("fred_filter", {})
        self.fred_filter_enabled = _fred_cfg.get("enabled", False)
        self.fred_yield_curve_min = _fred_cfg.get("yield_curve_min", None)  # e.g. -0.5 (skip if T10Y2Y < this)
        self.fred_claims_max = _fred_cfg.get("claims_max", None)  # e.g. 300000 (skip if ICSA > this)
        # Benchmark — use config value, fall back to market profile
        self.benchmark_ticker = config.get("universe", {}).get("benchmark_ticker")
        if not self.benchmark_ticker:
            try:
                from markets import get_market
                self.benchmark_ticker = get_market(self.market_id).benchmark_ticker
            except (ImportError, KeyError):
                self.benchmark_ticker = "IOZ.AX"

        # Risk-free rate from market profile
        try:
            from markets import get_market
            self._risk_free_rate = get_market(self.market_id).risk_free_rate
        except (ImportError, KeyError):
            self._risk_free_rate = 0.04

        logger.info(
            f"BacktestEngine initialized: train={self.train_window}, "
            f"test={self.test_window}, step={self.step_days}, "
            f"equity=${self.starting_equity:,.0f}, "
            f"flat_fee_threshold=${self.flat_fee_threshold:,.0f}"
        )

    def _calc_commission(self, value: float) -> float:
        """Calculate commission for one side of a trade.

        Phase1-Fix2: For positions below flat_fee_threshold, use
        percentage-only commission to avoid flat fees destroying
        small account equity. Above threshold, use max(flat, pct).
        """
        pct_commission = value * self.commission_pct
        if value < self.flat_fee_threshold:
            # Small position: percentage only (no flat minimum)
            return pct_commission
        return max(self.commission_per_trade, pct_commission)

    def _apply_slippage(self, price: float, direction: str) -> float:
        """Apply slippage to a price.

        For buys: price goes up (adverse).
        For sells: price goes down (adverse).
        """
        if direction == "buy":
            return price * (1 + self.slippage_pct)
        else:
            return price * (1 - self.slippage_pct)

    def _get_data_window(
        self, data: Dict[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp
    ) -> Dict[str, pd.DataFrame]:
        """Slice all ticker DataFrames to a date window."""
        windowed = {}
        for ticker, df in data.items():
            mask = (df.index >= start) & (df.index <= end)
            sliced = df.loc[mask]
            if len(sliced) >= self.min_history:
                windowed[ticker] = sliced
        return windowed

    def _simulate_day(
        self,
        day_idx: int,
        trading_dates: pd.DatetimeIndex,
        data: Dict[str, pd.DataFrame],
        strategies: List[BaseStrategy],
        equity: float,
        open_positions: List[Dict[str, Any]],
        closed_trades: List[Dict[str, Any]],
        breadth_series: Optional[pd.DataFrame] = None,
        rs_data: Optional[Dict[str, pd.DataFrame]] = None,
        equity_history: Optional[List[float]] = None,
        vix_series: Optional[pd.Series] = None,
        fred_yield_curve: Optional[pd.Series] = None,
        fred_claims: Optional[pd.Series] = None,
    ) -> float:
        """Simulate one trading day.

        Process:
            1. Update open position P&L with today's prices
            2. Check exit conditions on open positions
            3. Execute exits (fill at today's open, since signal was yesterday)
            4. Generate new entry signals using data up to yesterday's close
            5. Execute entries (fill at today's open)

        Returns:
            Updated equity value.
        """
        today = trading_dates[day_idx]

        # --- Process exits first ---
        # Check exits using data up to yesterday (signal day)
        if day_idx > 0:
            yesterday = trading_dates[day_idx - 1]
            # Build data windows up to yesterday for exit checks
            exit_data = {}
            for ticker, df in data.items():
                mask = df.index <= yesterday
                if mask.any():
                    exit_data[ticker] = df.loc[mask]

            for strategy in strategies:
                exit_recs = strategy.check_exits(exit_data, open_positions)
                for rec in exit_recs:
                    ticker = rec["ticker"]
                    # Find the position
                    pos_idx = None
                    for i, pos in enumerate(open_positions):
                        if pos["ticker"] == ticker and pos["strategy"] == strategy.name:
                            pos_idx = i
                            break

                    if pos_idx is None:
                        continue

                    pos = open_positions[pos_idx]

                    # Fill at today's open with slippage
                    today_df = data.get(ticker)
                    if today_df is None or today not in today_df.index:
                        # Use the recommended exit price if we can't get today's open
                        fill_price = self._apply_slippage(
                            rec["exit_price"], "sell"
                        )
                    else:
                        today_open = today_df.loc[today, "open"]
                        fill_price = self._apply_slippage(today_open, "sell")

                    # Calculate exit commission
                    exit_value = pos["shares"] * fill_price
                    exit_commission = self._calc_commission(exit_value)

                    # Calculate P&L
                    gross_pnl = (fill_price - pos["fill_price"]) * pos["shares"]
                    total_commission = pos["entry_commission"] + exit_commission
                    net_pnl = gross_pnl - total_commission

                    # Calculate hold days
                    hold_days = (today - pd.Timestamp(pos["entry_date"])).days

                    # Record closed trade
                    trade = {
                        "ticker": ticker,
                        "strategy": pos["strategy"],
                        "direction": "long",
                        "entry_date": pos["entry_date"],
                        "entry_price": pos["fill_price"],
                        "exit_date": today,
                        "exit_price": fill_price,
                        "shares": pos["shares"],
                        "position_value": pos["position_value"],
                        "gross_pnl": round(gross_pnl, 2),
                        "commission": round(total_commission, 2),
                        "pnl": round(net_pnl, 2),
                        "return_pct": round(
                            net_pnl / pos["position_value"] * 100, 2
                        )
                        if pos["position_value"] > 0
                        else 0.0,
                        "hold_days": hold_days,
                        "exit_reason": rec["reason"],
                        "mae": pos.get("mae", 0.0),
                        "mfe": pos.get("mfe", 0.0),
                        "stop_price": pos.get("stop_price", 0.0),
                        "confidence": pos.get("confidence", 0.0),
                        "features": pos.get("features", {}),
                    }
                    trade["r_multiple"] = calc_r_multiple(trade)
                    closed_trades.append(trade)

                    # Update equity
                    equity += net_pnl

                    # Remove from open positions
                    open_positions.pop(pos_idx)

                    logger.debug(
                        f"EXIT {ticker} ({rec['reason']}): "
                        f"pnl=${net_pnl:.2f}, equity=${equity:.2f}"
                    )

        # --- Update MAE/MFE for open positions ---
        for pos in open_positions:
            ticker = pos["ticker"]
            today_df = data.get(ticker)
            if today_df is None or today not in today_df.index:
                continue

            today_low = today_df.loc[today, "low"]
            today_high = today_df.loc[today, "high"]
            fill_price = pos["fill_price"]

            # MAE: max adverse excursion (worst unrealized loss per share)
            adverse = (today_low - fill_price) / fill_price
            pos["mae"] = min(pos.get("mae", 0.0), adverse)

            # MFE: max favorable excursion (best unrealized gain per share)
            favorable = (today_high - fill_price) / fill_price
            pos["mfe"] = max(pos.get("mfe", 0.0), favorable)


        # --- Phase 9A: Max loss per trade cap ---
        if self.max_loss_per_trade is not None and open_positions:
            _maxloss_exits = []  # (pos_idx, exit_price)
            # Audit C1: use yesterday's close for trigger decision to avoid look-ahead bias
            _yest_date_mlc = trading_dates[day_idx - 1] if day_idx > 0 else None
            for _pi, _pos in enumerate(open_positions):
                _ticker = _pos["ticker"]
                _today_df = data.get(_ticker)
                if _today_df is None or today not in _today_df.index:
                    continue
                _today_close = _today_df.loc[today, "close"]  # used for fill price only
                # Audit C1: skip day 0 (no yesterday) or if yesterday not in this ticker's data
                if _yest_date_mlc is None or _yest_date_mlc not in _today_df.index:
                    continue
                _yest_close = _today_df.loc[_yest_date_mlc, "close"]  # Audit C1: trigger on yesterday
                _unrealized_pnl = (_yest_close - _pos["fill_price"]) * _pos["shares"]
                if _unrealized_pnl <= -abs(self.max_loss_per_trade):
                    _maxloss_exits.append((_pi, _today_close))  # fill at today's close
                    logger.debug(
                        f"MAX_LOSS_CAP {_ticker}: unrealized ${_unrealized_pnl:.2f} "
                        f"<= -${abs(self.max_loss_per_trade):.2f}"
                    )

            # Process max loss exits (reverse order to preserve indices)
            for _pi, _exit_price in reversed(_maxloss_exits):
                _pos = open_positions[_pi]
                _ticker = _pos["ticker"]
                _fill_price = self._apply_slippage(_exit_price, "sell")
                _exit_commission = self._calc_commission(_pos["shares"] * _fill_price)
                _gross_pnl = (_fill_price - _pos["fill_price"]) * _pos["shares"]
                _net_pnl = _gross_pnl - _pos["entry_commission"] - _exit_commission
                _hold_days = (today - pd.Timestamp(_pos["entry_date"])).days
                _trade = {
                    "ticker": _ticker,
                    "strategy": _pos["strategy"],
                    "direction": "long",
                    "entry_date": _pos["entry_date"],
                    "entry_price": _pos["fill_price"],
                    "exit_date": today,
                    "exit_price": _fill_price,
                    "shares": _pos["shares"],
                    "position_value": _pos["position_value"],
                    "gross_pnl": round(_gross_pnl, 2),
                    "commission": round(_pos["entry_commission"] + _exit_commission, 2),
                    "pnl": round(_net_pnl, 2),
                    "return_pct": round(_net_pnl / _pos["position_value"] * 100, 2)
                        if _pos["position_value"] > 0 else 0.0,
                    "hold_days": _hold_days,
                    "exit_reason": "max_loss_cap",
                    "mae": _pos.get("mae", 0.0),
                    "mfe": _pos.get("mfe", 0.0),
                    "stop_price": _pos.get("stop_price", 0.0),
                    "confidence": _pos.get("confidence", 0.0),
                    "features": _pos.get("features", {}),
                }
                _trade["r_multiple"] = calc_r_multiple(_trade)
                closed_trades.append(_trade)
                equity += _net_pnl
                open_positions.pop(_pi)
                logger.debug(
                    f"MAX_LOSS_CAP EXIT {_ticker}: pnl=${_net_pnl:.2f}, equity=${equity:.2f}"
                )

        # --- Trailing stop: update state and check exits ---
        if self.trailing_stop_enabled and open_positions:
            _trail_exits = []  # (pos_idx, exit_price)
            # Audit C1: use yesterday's close for exit trigger to avoid look-ahead bias
            _yest_date_trail = trading_dates[day_idx - 1] if day_idx > 0 else None
            for _pi, _pos in enumerate(open_positions):
                _ticker = _pos["ticker"]
                _today_df = data.get(_ticker)
                if _today_df is None or today not in _today_df.index:
                    continue

                _today_high = _today_df.loc[today, "high"]
                _today_low  = _today_df.loc[today, "low"]
                _today_close = _today_df.loc[today, "close"]
                _fill = _pos["fill_price"]

                # Audit C1: yesterday's close for exit trigger (avoid look-ahead bias)
                _yest_close_trail = (
                    _today_df.loc[_yest_date_trail, "close"]
                    if _yest_date_trail is not None and _yest_date_trail in _today_df.index
                    else None
                )

                # Get ATR: prefer live calculation, fall back to entry feature
                _atr = _pos.get("features", {}).get("atr", 0.0) or 0.0
                if _atr <= 0:
                    # Quick 14-bar ATR from today's data window
                    _mask = _today_df.index <= today
                    _w = _today_df.loc[_mask].tail(15)
                    if len(_w) >= 3:
                        _tr = (_w["high"] - _w["low"]).abs()
                        _atr = float(_tr.rolling(14, min_periods=3).mean().iloc[-1])
                    if _atr <= 0:
                        continue

                # Check activation: did price reach +activation_pct above entry?
                _unrealised_pct = (_today_high - _fill) / _fill
                _trail_active = _pos.get("trailing_stop_active", False)

                if not _trail_active and _unrealised_pct >= self.trail_activation_pct:
                    _trail_active = True
                    _pos["trailing_stop_active"] = True
                    _pos["highest_price"] = _today_high
                    _pos["trailing_stop_price"] = _today_high - self.trail_atr_multiplier * _atr
                    logger.debug(
                        f"TRAIL ACTIVATED {_ticker}: high={_today_high:.3f}, "
                        f"trail_stop={_pos['trailing_stop_price']:.3f}"
                    )

                if _trail_active:
                    # Ratchet highest price upward
                    _prev_high = _pos.get("highest_price", _today_high)
                    _new_high = max(_prev_high, _today_high)
                    _pos["highest_price"] = _new_high

                    # New trail stop = highest - multiplier × ATR  (ratchet up only)
                    _new_trail = _new_high - self.trail_atr_multiplier * _atr
                    _prev_trail = _pos.get("trailing_stop_price", _pos["stop_price"])
                    _pos["trailing_stop_price"] = max(_prev_trail, _new_trail)

                    # Also enforce: trail stop >= initial stop
                    _pos["trailing_stop_price"] = max(
                        _pos["trailing_stop_price"], _pos["stop_price"]
                    )

                    # Audit C1: trigger on yesterday's close; fill at today's close
                    if _yest_close_trail is not None and _yest_close_trail <= _pos["trailing_stop_price"]:
                        _trail_exits.append((_pi, _today_close))  # fill at today's close
                        logger.debug(
                            f"TRAIL EXIT {_ticker}: yest_close={_yest_close_trail:.3f} "
                            f"<= trail_stop={_pos['trailing_stop_price']:.3f}"
                        )

            # Process trailing stop exits (reverse order to preserve indices)
            for _pi, _exit_price in reversed(_trail_exits):
                _pos = open_positions[_pi]
                _ticker = _pos["ticker"]
                _fill_price = self._apply_slippage(_exit_price, "sell")
                _exit_commission = self._calc_commission(_pos["shares"] * _fill_price)
                _gross_pnl = (_fill_price - _pos["fill_price"]) * _pos["shares"]
                _net_pnl = _gross_pnl - _pos["entry_commission"] - _exit_commission
                _hold_days = (today - pd.Timestamp(_pos["entry_date"])).days
                _trade = {
                    "ticker": _ticker,
                    "strategy": _pos["strategy"],
                    "direction": "long",
                    "entry_date": _pos["entry_date"],
                    "entry_price": _pos["fill_price"],
                    "exit_date": today,
                    "exit_price": _fill_price,
                    "shares": _pos["shares"],
                    "position_value": _pos["position_value"],
                    "gross_pnl": round(_gross_pnl, 2),
                    "commission": round(_pos["entry_commission"] + _exit_commission, 2),
                    "pnl": round(_net_pnl, 2),
                    "return_pct": round(_net_pnl / _pos["position_value"] * 100, 2)
                        if _pos["position_value"] > 0 else 0.0,
                    "hold_days": _hold_days,
                    "exit_reason": "trailing_stop",
                    "mae": _pos.get("mae", 0.0),
                    "mfe": _pos.get("mfe", 0.0),
                    "stop_price": _pos.get("trailing_stop_price", _pos.get("stop_price", 0.0)),
                    "confidence": _pos.get("confidence", 0.0),
                    "features": _pos.get("features", {}),
                }
                _trade["r_multiple"] = calc_r_multiple(_trade)
                closed_trades.append(_trade)
                equity += _net_pnl
                open_positions.pop(_pi)
                logger.debug(
                    f"TRAIL EXIT {_ticker}: pnl=${_net_pnl:.2f}, equity=${equity:.2f}"
                )

        # --- Phase 3: Simple Regime Filter (3-state: Bull/Neutral/Bear) ---
        # Uses benchmark MA slope + market breadth pct_above_200ma to scale position sizes.
        # Bull  (both signals positive): full position size (scale=1.0)
        # Neutral (mixed signals):       reduced position size (scale=0.75)
        # Bear  (both signals negative): half position size (scale=0.5)
        _rf_cfg = self.config.get("regime_filter", {})
        _rf_enabled = _rf_cfg.get("enabled", False)
        regime = "neutral"   # default when regime_filter disabled or data unavailable
        regime_scale = 1.0   # default: full size
        if _rf_enabled:
            _rf_ma = _rf_cfg.get("benchmark_ma_period", 50)
            _rf_bull_thresh = _rf_cfg.get("breadth_bull_threshold", 50.0)
            _rf_bear_thresh = _rf_cfg.get("breadth_bear_threshold", 40.0)
            _rf_bull_scale = _rf_cfg.get("bull_scale", 1.0)
            _rf_neutral_scale = _rf_cfg.get("neutral_scale", 0.75)
            _rf_bear_scale = _rf_cfg.get("bear_scale", 0.5)
            # Signal 1: Benchmark above/below MA (trend direction)
            _bench_df = data.get(self.benchmark_ticker)
            _bench_above_ma = None
            if _bench_df is not None and today in _bench_df.index:
                _bench_close = _bench_df.loc[:today, "close"]
                if len(_bench_close) >= _rf_ma:
                    _bench_ma = _bench_close.rolling(window=_rf_ma).mean().iloc[-1]
                    if not pd.isna(_bench_ma):
                        _bench_above_ma = bool(_bench_close.iloc[-1] >= _bench_ma)
            # Signal 2: Market breadth — % stocks above 200-day MA
            _breadth_pct200 = None
            if breadth_series is not None and today in breadth_series.index:
                _brow = breadth_series.loc[today]
                _raw = _brow.get("pct_above_200ma", None)
                if _raw is not None and not pd.isna(_raw):
                    _breadth_pct200 = float(_raw)
            # Classify regime using both signals
            if _bench_above_ma is not None and _breadth_pct200 is not None:
                if _bench_above_ma and _breadth_pct200 >= _rf_bull_thresh:
                    regime, regime_scale = "bull", _rf_bull_scale
                elif not _bench_above_ma and _breadth_pct200 < _rf_bear_thresh:
                    regime, regime_scale = "bear", _rf_bear_scale
                else:
                    regime, regime_scale = "neutral", _rf_neutral_scale
            elif _bench_above_ma is not None:
                # Only MA signal available (no breadth data)
                if _bench_above_ma:
                    regime, regime_scale = "bull", _rf_bull_scale
                else:
                    regime, regime_scale = "bear", _rf_bear_scale
            _b200_str = f"{_breadth_pct200:.1f}%" if _breadth_pct200 is not None else "N/A"
            logger.debug(
                f"REGIME {today.date()}: {regime.upper()} "
                f"(IOZ_above_{_rf_ma}MA={_bench_above_ma}, "
                f"breadth200={_b200_str}, scale={regime_scale:.2f})"
            )

        # --- Generate new entry signals ---
        if day_idx > 0 and len(open_positions) < self.max_positions:  # regime_scale applied in sizing
            yesterday = trading_dates[day_idx - 1]

            # VIX regime filter: skip all entries when VIX is too high
            vix_blocked = False
            if vix_series is not None and yesterday in vix_series.index:
                current_vix = float(vix_series.loc[yesterday])
                if current_vix > self.vix_max_entry:
                    vix_blocked = True

            # FRED macro regime filter: skip entries during adverse macro
            fred_blocked = False
            if fred_yield_curve is not None and self.fred_yield_curve_min is not None:
                # Use latest available value on or before yesterday
                yc_mask = fred_yield_curve.index <= yesterday
                if yc_mask.any():
                    yc_val = float(fred_yield_curve.loc[yc_mask].iloc[-1])
                    if yc_val < self.fred_yield_curve_min:
                        fred_blocked = True
            if fred_claims is not None and self.fred_claims_max is not None and not fred_blocked:
                cl_mask = fred_claims.index <= yesterday
                if cl_mask.any():
                    cl_val = float(fred_claims.loc[cl_mask].iloc[-1])
                    if cl_val > self.fred_claims_max:
                        fred_blocked = True
            # Build data windows up to yesterday for signal generation
            signal_data = {}
            for ticker, df in data.items():
                mask = df.index <= yesterday
                if mask.any() and mask.sum() >= self.min_history:
                    signal_data[ticker] = df.loc[mask]

            for strategy in strategies:
                if len(open_positions) >= self.max_positions:
                    break
                if vix_blocked or fred_blocked:
                    break

                signals = strategy.generate_signals(
                    signal_data, equity, open_positions
                )

                # Phase 7C: Inject market breadth features (info-only)
                if breadth_series is not None and today in breadth_series.index:
                    _brd = breadth_series.loc[today]
                    for _sig in signals:
                        _sig.features["breadth_pct_above_50ma"] = float(_brd.get("pct_above_50ma", 0))
                        _sig.features["breadth_pct_above_200ma"] = float(_brd.get("pct_above_200ma", 0))
                        _sig.features["breadth_ad_ratio"] = float(_brd.get("ad_ratio", 0))
                        _sig.features["breadth_thrust"] = float(_brd.get("breadth_thrust", 0))
                        _sig.features["breadth_momentum"] = float(_brd.get("breadth_momentum", 0)) if not pd.isna(_brd.get("breadth_momentum", 0)) else 0.0
                        _sig.features["breadth_net_new_highs_pct"] = float(_brd.get("net_new_highs_pct", 0))
                        _sig.features["regime"] = regime
                        _sig.features["regime_scale"] = regime_scale

                # Phase 7C: Apply breadth-based confidence modifiers
                for _sig in signals:
                    _strat_key = _sig.strategy  # e.g. 'trend_following', 'mean_reversion'
                    _breadth_cfg = self.config.get("strategies", {}).get(_strat_key, {}).get("breadth", {})
                    if _breadth_cfg.get("enabled", False):
                        _metric = _breadth_cfg.get("metric", "pct_above_50ma")
                        _breadth_val = _sig.features.get(f"breadth_{_metric}", None)
                        if _breadth_val is not None:
                            _low_thresh = _breadth_cfg.get("low_threshold", 0.48)
                            _high_thresh = _breadth_cfg.get("high_threshold", 0.58)
                            _low_boost = _breadth_cfg.get("low_boost", 0.0)
                            _high_penalty = _breadth_cfg.get("high_penalty", 0.0)
                            _orig_conf = _sig.confidence
                            _breadth_adj = 0.0
                            if _breadth_val < _low_thresh:
                                _breadth_adj = _low_boost
                            elif _breadth_val > _high_thresh:
                                _breadth_adj = -_high_penalty
                            if _breadth_adj != 0.0:
                                _sig.confidence = max(0.0, min(1.0, _sig.confidence + _breadth_adj))
                                _sig.features["breadth_confidence_adj"] = round(_breadth_adj, 4)
                                _sig.features["breadth_confidence_orig"] = round(_orig_conf, 4)
                                logger.debug(
                                    f"BREADTH {_sig.ticker} ({_strat_key}): "
                                    f"breadth={_breadth_val:.2f}, adj={_breadth_adj:+.3f}, "
                                    f"conf {_orig_conf:.3f} -> {_sig.confidence:.3f}"
                                )

                # Phase 7B: Inject relative strength features (info-only)
                if rs_data is not None:
                    for _sig in signals:
                        _ticker = _sig.ticker
                        if _ticker in rs_data:
                            _rs_df = rs_data[_ticker]
                            # Use yesterday's date for RS lookup (signal generation date)
                            _rs_dates = _rs_df.index[_rs_df.index <= yesterday]
                            if len(_rs_dates) > 0:
                                _rs_date = _rs_dates[-1]
                                _rs_row = _rs_df.loc[_rs_date]
                                _sig.features["rs_percentile"] = float(_rs_row.get("rs_percentile", 50.0)) if not pd.isna(_rs_row.get("rs_percentile", 50.0)) else 50.0
                                _sig.features["rs_score"] = float(_rs_row.get("rs_score", 0.0)) if not pd.isna(_rs_row.get("rs_score", 0.0)) else 0.0
                                _sig.features["rs_momentum"] = float(_rs_row.get("rs_momentum", 0.0)) if not pd.isna(_rs_row.get("rs_momentum", 0.0)) else 0.0
                                _sig.features["roc_20"] = float(_rs_row.get("roc_20", 0.0)) if not pd.isna(_rs_row.get("roc_20", 0.0)) else 0.0
                                _sig.features["roc_60"] = float(_rs_row.get("roc_60", 0.0)) if not pd.isna(_rs_row.get("roc_60", 0.0)) else 0.0
                                _sig.features["roc_120"] = float(_rs_row.get("roc_120", 0.0)) if not pd.isna(_rs_row.get("roc_120", 0.0)) else 0.0

                # Phase 7B: Apply RS-based confidence modifiers
                for _sig in signals:
                    _strat_key = _sig.strategy
                    _rs_cfg = self.config.get("strategies", {}).get(_strat_key, {}).get("relative_strength", {})
                    if _rs_cfg.get("enabled", False):
                        _rs_metric = _rs_cfg.get("metric", "rs_percentile")
                        _rs_val = _sig.features.get(_rs_metric, None)
                        if _rs_val is not None:
                            _rs_low_thresh = _rs_cfg.get("low_threshold", 40.0)
                            _rs_high_thresh = _rs_cfg.get("high_threshold", 60.0)
                            _rs_low_penalty = _rs_cfg.get("low_penalty", 0.0)
                            _rs_high_boost = _rs_cfg.get("high_boost", 0.0)
                            _rs_orig_conf = _sig.confidence
                            _rs_adj = 0.0
                            if _rs_val < _rs_low_thresh:
                                _rs_adj = -_rs_low_penalty
                            elif _rs_val > _rs_high_thresh:
                                _rs_adj = _rs_high_boost
                            if _rs_adj != 0.0:
                                _sig.confidence = max(0.0, min(1.0, _sig.confidence + _rs_adj))
                                _sig.features["rs_confidence_adj"] = round(_rs_adj, 4)
                                _sig.features["rs_confidence_orig"] = round(_rs_orig_conf, 4)
                                logger.debug(
                                    f"RS {_sig.ticker} ({_strat_key}): "
                                    f"rs={_rs_val:.1f}, adj={_rs_adj:+.3f}, "
                                    f"conf {_rs_orig_conf:.3f} -> {_sig.confidence:.3f}"
                                )

                for signal in signals:
                    if len(open_positions) >= self.max_positions:
                        break

                    ticker = signal.ticker

                    # Phase1-Fix5: Skip low-confidence signals
                    # Phase 8C: Per-strategy min_confidence override
                    strat_cfg = self.config.get("strategies", {}).get(signal.strategy, {})
                    min_conf = strat_cfg.get("min_confidence", self.min_confidence)
                    if signal.confidence < min_conf:
                        logger.debug(
                            f"SKIP {ticker}: confidence {signal.confidence:.2f} "
                            f"< min {min_conf} ({signal.strategy})"
                        )
                        continue

                    # Check if we already hold this ticker
                    if any(p["ticker"] == ticker for p in open_positions):
                        continue

                    # Audit H1: sector concentration check
                    signal_sector = (
                        signal.features.get("sector", "Unknown")
                        if hasattr(signal, "features") and signal.features
                        else "Unknown"
                    )
                    sector_count = sum(
                        1 for p in open_positions
                        if p.get("features", {}).get("sector", "Unknown") == signal_sector
                    )
                    if sector_count >= self.max_sector:
                        logger.debug(
                            f"SKIP {ticker}: sector '{signal_sector}' already has "
                            f"{sector_count} positions (max={self.max_sector})"
                        )
                        continue

                    # Get today's open for fill
                    today_df = data.get(ticker)
                    if today_df is None or today not in today_df.index:
                        continue

                    today_open = today_df.loc[today, "open"]
                    fill_price = self._apply_slippage(today_open, "buy")

                    if fill_price <= 0:
                        continue

                    # Recalculate position size at actual fill price
                    # Adjust stop proportionally
                    price_ratio = fill_price / signal.entry_price
                    adjusted_stop = signal.stop_price * price_ratio

                    if fill_price <= adjusted_stop:
                        continue  # stop would be above entry

                    risk_per_share = fill_price - adjusted_stop
                    # Phase 8D: Dynamic position sizing
                    _atr = signal.features.get('atr', 0.0) if hasattr(signal, 'features') and signal.features else 0.0
                    _risk_pct = self.dynamic_sizer.calculate_risk_pct(
                        confidence=signal.confidence,
                        atr=_atr,
                        price=fill_price,
                        equity_history=equity_history,
                    )
                    risk_budget = equity * _risk_pct * regime_scale  # Phase 3: regime scaling
                    shares = int(risk_budget / risk_per_share)

                    if shares <= 0:
                        continue

                    position_value = shares * fill_price

                    # Cap at max position value (equal-weight allocation)
                    if self.max_position_value > 0 and position_value > self.max_position_value:
                        shares = int(self.max_position_value / fill_price)
                        position_value = shares * fill_price

                    if shares <= 0:
                        continue

                    # Phase1-Fix2: Skip positions below minimum value
                    if position_value < self.min_position_value:
                        logger.debug(
                            f"SKIP {ticker}: position value ${position_value:.0f} "
                            f"< min ${self.min_position_value:.0f}"
                        )
                        continue

                    # Don't exceed available equity
                    invested = sum(
                        p["position_value"] for p in open_positions
                    )
                    available = equity - invested
                    if position_value > available:
                        shares = int(available / fill_price)
                        if shares <= 0:
                            continue
                        position_value = shares * fill_price
                        # Re-check minimum after adjustment
                        if position_value < self.min_position_value:
                            continue

                    # --- Fee-Aware Signal Filter ---
                    # Filter out signals where expected net PnL (after round-trip commission)
                    # is below the configured minimum threshold.
                    if self.fee_aware_enabled:
                        _rt_commission = 2.0 * self._calc_commission(position_value)
                        _risk_amt = (fill_price - adjusted_stop) * shares
                        if signal.take_profit and signal.take_profit > signal.entry_price:
                            _adj_tp = signal.take_profit * price_ratio
                            _reward_amt = max(0.0, (_adj_tp - fill_price) * shares)
                        else:
                            _reward_amt = _risk_amt * self.fee_aware_rrr
                        _conf = signal.confidence
                        _exp_gross = _conf * _reward_amt - (1.0 - _conf) * _risk_amt
                        _exp_net = _exp_gross - _rt_commission
                        if _exp_net < self.fee_aware_min_pnl:
                            logger.debug(
                                f"SKIP {ticker} (fee_aware): exp_net=${_exp_net:.2f} "
                                f"< min=${self.fee_aware_min_pnl:.2f} "
                                f"(rt_comm=${_rt_commission:.2f}, conf={_conf:.2f}, "
                                f"reward=${_reward_amt:.2f}, risk=${_risk_amt:.2f}) "
                                f"[{signal.strategy}]"
                            )
                            continue

                    # Entry commission
                    entry_commission = self._calc_commission(position_value)

                    # Create position record
                    position = {
                        "ticker": ticker,
                        "strategy": signal.strategy,
                        "direction": "long",
                        "entry_date": today,
                        "fill_price": round(fill_price, 4),
                        "entry_price": round(fill_price, 4),  # alias for strategy compat
                        "stop_price": round(adjusted_stop, 4),
                        "take_profit": (
                            round(signal.take_profit * price_ratio, 4)
                            if signal.take_profit
                            else None
                        ),
                        "shares": shares,
                        "position_value": round(position_value, 2),
                        "entry_commission": round(entry_commission, 2),
                        "confidence": signal.confidence,
                        "features": signal.features if hasattr(signal, 'features') else {},
                        "mae": 0.0,
                        "mfe": 0.0,
                    }
                    open_positions.append(position)

                    logger.debug(
                        f"ENTRY {ticker} ({signal.strategy}): "
                        f"{shares} shares @ ${fill_price:.2f}, "
                        f"stop=${adjusted_stop:.2f}"
                    )

        return equity

    def _force_close_all(
        self,
        open_positions: List[Dict[str, Any]],
        data: Dict[str, pd.DataFrame],
        close_date: pd.Timestamp,
        closed_trades: List[Dict[str, Any]],
        equity: float,
    ) -> float:
        """Force-close all open positions (used only at end of entire backtest)."""
        for pos in list(open_positions):
            ticker = pos["ticker"]
            today_df = data.get(ticker)

            if today_df is not None and close_date in today_df.index:
                close_price = today_df.loc[close_date, "close"]
            elif today_df is not None and len(today_df) > 0:
                close_price = today_df["close"].iloc[-1]
            else:
                close_price = pos["fill_price"]  # fallback

            fill_price = self._apply_slippage(close_price, "sell")
            exit_value = pos["shares"] * fill_price
            exit_commission = self._calc_commission(exit_value)

            gross_pnl = (fill_price - pos["fill_price"]) * pos["shares"]
            total_commission = pos["entry_commission"] + exit_commission
            net_pnl = gross_pnl - total_commission
            hold_days = (close_date - pd.Timestamp(pos["entry_date"])).days

            trade = {
                "ticker": ticker,
                "strategy": pos["strategy"],
                "direction": "long",
                "entry_date": pos["entry_date"],
                "entry_price": pos["fill_price"],
                "exit_date": close_date,
                "exit_price": fill_price,
                "shares": pos["shares"],
                "position_value": pos["position_value"],
                "gross_pnl": round(gross_pnl, 2),
                "commission": round(total_commission, 2),
                "pnl": round(net_pnl, 2),
                "return_pct": round(
                    net_pnl / pos["position_value"] * 100, 2
                )
                if pos["position_value"] > 0
                else 0.0,
                "hold_days": hold_days,
                "exit_reason": "backtest_end",
                "mae": pos.get("mae", 0.0),
                "mfe": pos.get("mfe", 0.0),
                "stop_price": pos.get("stop_price", 0.0),
                "confidence": pos.get("confidence", 0.0),
                "features": pos.get("features", {}),
            }
            trade["r_multiple"] = calc_r_multiple(trade)
            closed_trades.append(trade)
            equity += net_pnl

        open_positions.clear()
        return equity

    def _calc_benchmark(
        self, start: pd.Timestamp, end: pd.Timestamp
    ) -> Dict[str, Any]:
        """Calculate buy-and-hold benchmark metrics.

        Phase1-Fix1: Loads benchmark data directly via download_ticker
        instead of relying on the universe data dict (which doesn't
        include the benchmark ETF).
        """
        try:
            bench_df = download_ticker(
                self.benchmark_ticker,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                use_cache=True,
            )
        except Exception as e:
            logger.warning(f"Failed to load benchmark {self.benchmark_ticker}: {e}")
            bench_df = pd.DataFrame()

        if bench_df is None or bench_df.empty:
            logger.warning(
                f"Benchmark {self.benchmark_ticker} data unavailable, "
                f"returning empty metrics"
            )
            return {"cagr": 0, "max_drawdown": 0, "sharpe": 0, "total_return": 0}

        mask = (bench_df.index >= start) & (bench_df.index <= end)
        bench = bench_df.loc[mask, "close"]

        if len(bench) < 10:
            return {"cagr": 0, "max_drawdown": 0, "sharpe": 0, "total_return": 0}

        # Simulate buy-and-hold: invest starting_equity at first close
        initial_price = bench.iloc[0]
        shares = self.starting_equity / initial_price
        bench_equity = bench * shares

        bench_returns = bench.pct_change().dropna()

        from backtest.metrics import (
            calc_cagr,
            calc_max_drawdown,
            calc_sharpe,
            calc_sortino,
        )

        return {
            "cagr": calc_cagr(bench_equity),
            "max_drawdown": calc_max_drawdown(bench_equity),
            "sharpe": calc_sharpe(bench_returns),
            "sortino": calc_sortino(bench_returns),
            "total_return": round(
                (bench.iloc[-1] / bench.iloc[0]) - 1, 4
            ),
            "final_equity": round(bench_equity.iloc[-1], 2),
        }

    def run_walkforward(
        self,
        data: Dict[str, pd.DataFrame],
        strategies: List[BaseStrategy],
    ) -> BacktestResult:
        """Run walk-forward backtest across all strategies.

        Phase1-Fix3: Positions now carry across walk-forward windows
        instead of being force-closed at window boundaries. This
        eliminates artificial losses from premature exits.

        Walk-forward process:
            1. Start at the earliest date where we have train_window of data
            2. Use train_window for strategy calibration (data available to strategies)
            3. Simulate trading on the next test_window days
            4. Step forward by step_days and repeat
            5. Positions carry across windows naturally
            6. Force-close only at the very end of the backtest

        Args:
            data: Dict mapping ticker -> DataFrame with OHLCV data.
            strategies: List of strategy instances to run.

        Returns:
            BacktestResult with trades, equity curve, and metrics.
        """
        logger.info("Starting walk-forward backtest...")
        logger.info(f"Strategies: {[s.name for s in strategies]}")
        logger.info(f"Tickers: {len(data)}")

        # Find common date range across all tickers
        all_dates = set()
        for df in data.values():
            all_dates.update(df.index)
        all_dates = sorted(all_dates)

        if len(all_dates) < self.train_window + self.test_window:
            logger.error(
                f"Insufficient data: {len(all_dates)} days, "
                f"need {self.train_window + self.test_window}"
            )
            return BacktestResult()

        all_dates = pd.DatetimeIndex(all_dates)
        total_days = len(all_dates)

        logger.info(
            f"Date range: {all_dates[0].date()} to {all_dates[-1].date()} "
            f"({total_days} trading days)"
        )

        # Initialize state
        equity = float(self.starting_equity)
        all_trades: List[Dict[str, Any]] = []
        equity_records: List[Dict[str, Any]] = []
        equity_history: List[float] = []  # Phase 8D: track equity for dynamic sizing
        walk_forward_windows: List[Dict[str, Any]] = []

        # Phase1-Fix3: Persistent positions across windows
        open_positions: List[Dict[str, Any]] = []
        # Track which dates have been simulated to avoid double-processing
        simulated_dates: set = set()

        # Phase 7C: Pre-compute market breadth series from universe data
        logger.info("Computing market breadth indicators...")
        try:
            mb = MarketBreadth(data)
            breadth_series = mb.compute_series()
            logger.info(
                f"Breadth series: {len(breadth_series)} days, "
                f"cols={list(breadth_series.columns)}"
            )
        except Exception as e:
            logger.warning(f"Failed to compute breadth: {e}. Continuing without breadth.")
            breadth_series = None

        # Phase 7B: Pre-compute relative strength rankings from universe data
        logger.info("Computing relative strength rankings...")
        try:
            rs_engine = RelativeStrength(data)
            rs_data = rs_engine.compute_series()
            logger.info(
                f"RS series: {len(rs_data)} tickers computed"
            )
        except Exception as e:
            logger.warning(f"Failed to compute RS: {e}. Continuing without RS.")
            rs_data = None

        # VIX data for regime filtering
        vix_series = None
        if self.vix_filter_enabled:
            try:
                vix_df = download_ticker('^VIX', use_cache=True, market_id='sp500')
                if vix_df is not None and not vix_df.empty:
                    vix_series = vix_df['close']
                    logger.info(f"VIX filter enabled: max_entry={self.vix_max_entry}, "
                                f"VIX data {len(vix_series)} days")
                else:
                    logger.warning("VIX filter enabled but no VIX data — filter disabled")
            except Exception as e:
                logger.warning(f"VIX data load failed: {e} — filter disabled")

        # FRED macro data for regime filtering
        fred_yield_curve = None
        fred_claims = None
        if self.fred_filter_enabled:
            try:
                from data.fred import FREDClient
                _fred = FREDClient()
                if _fred.available:
                    if self.fred_yield_curve_min is not None:
                        fred_yield_curve = _fred.get_yield_curve_slope()
                        if len(fred_yield_curve) > 0:
                            logger.info(f"FRED yield curve filter: min={self.fred_yield_curve_min}, "
                                        f"data {len(fred_yield_curve)} obs")
                    if self.fred_claims_max is not None:
                        fred_claims = _fred.get_unemployment_claims()
                        if len(fred_claims) > 0:
                            logger.info(f"FRED claims filter: max={self.fred_claims_max}, "
                                        f"data {len(fred_claims)} obs")
                else:
                    logger.warning("FRED filter enabled but no API key configured")
            except Exception as e:
                logger.warning(f"FRED data load failed: {e} — filter disabled")

        # Walk-forward loop
        window_start = 0
        window_num = 0

        while window_start + self.train_window + self.test_window <= total_days:
            window_num += 1
            train_start_idx = window_start
            train_end_idx = window_start + self.train_window - 1
            test_start_idx = train_end_idx + 1
            test_end_idx = min(
                test_start_idx + self.test_window - 1, total_days - 1
            )

            train_start = all_dates[train_start_idx]
            train_end = all_dates[train_end_idx]
            test_start = all_dates[test_start_idx]
            test_end = all_dates[test_end_idx]

            logger.info(
                f"Window {window_num}: "
                f"train {train_start.date()}-{train_end.date()}, "
                f"test {test_start.date()}-{test_end.date()}"
            )

            # Get test period trading dates
            test_dates = all_dates[test_start_idx : test_end_idx + 1]

            # Phase1-Fix3: Filter out already-simulated dates
            new_test_dates = pd.DatetimeIndex(
                [d for d in test_dates if d not in simulated_dates]
            )

            if len(new_test_dates) == 0:
                logger.info(f"Window {window_num}: all dates already simulated, skipping")
                window_start += self.step_days
                continue

            # Get full data up to and including test period
            # (strategies see data up to "yesterday" during simulation)
            window_data = self._get_data_window(
                data, train_start, test_end
            )

            if not window_data:
                logger.warning(f"Window {window_num}: no data, skipping")
                window_start += self.step_days
                continue

            # Simulate each NEW day in the test window
            window_trades: List[Dict[str, Any]] = []
            window_start_equity = equity

            for i, test_date in enumerate(new_test_dates):
                equity = self._simulate_day(
                    day_idx=i,
                    trading_dates=new_test_dates,
                    data=window_data,
                    strategies=strategies,
                    equity=equity,
                    open_positions=open_positions,
                    closed_trades=window_trades,
                    breadth_series=breadth_series,
                    rs_data=rs_data,
                    equity_history=equity_history,
                    vix_series=vix_series,
                    fred_yield_curve=fred_yield_curve,
                    fred_claims=fred_claims,
                )

                # Record daily equity (mark-to-market)
                mtm_value = equity
                for pos in open_positions:
                    ticker = pos["ticker"]
                    df = window_data.get(ticker)
                    if df is not None and test_date in df.index:
                        current_price = df.loc[test_date, "close"]
                        unrealized = (
                            (current_price - pos["fill_price"]) * pos["shares"]
                        )
                        mtm_value += unrealized

                equity_records.append(
                    {"date": test_date, "equity": round(mtm_value, 2)}
                )
                equity_history.append(mtm_value)  # Phase 8D

                simulated_dates.add(test_date)

            # Phase1-Fix3: NO force-close at window boundary!
            # Positions carry to next window naturally.

            all_trades.extend(window_trades)

            # Record window stats
            window_pnl = sum(t["pnl"] for t in window_trades)
            walk_forward_windows.append(
                {
                    "window": window_num,
                    "train_start": train_start,
                    "train_end": train_end,
                    "test_start": test_start,
                    "test_end": test_end,
                    "trades": len(window_trades),
                    "pnl": round(window_pnl, 2),
                    "equity_start": round(window_start_equity, 2),
                    "equity_end": round(equity, 2),
                    "open_positions": len(open_positions),
                }
            )

            logger.info(
                f"Window {window_num} complete: "
                f"{len(window_trades)} trades, "
                f"P&L=${window_pnl:.2f}, equity=${equity:.2f}, "
                f"open_positions={len(open_positions)}"
            )

            # Step forward
            window_start += self.step_days

        # Phase1-Fix3: Force-close remaining positions at END of entire backtest
        if open_positions:
            final_trades: List[Dict[str, Any]] = []
            equity = self._force_close_all(
                open_positions,
                data,  # use full data for final close
                all_dates[-1],
                final_trades,
                equity,
            )
            all_trades.extend(final_trades)
            logger.info(
                f"Final close: {len(final_trades)} positions closed at backtest end"
            )

        # Build equity curve
        if equity_records:
            eq_df = pd.DataFrame(equity_records)
            # Remove duplicate dates (from overlapping windows), keep last
            eq_df = eq_df.drop_duplicates(subset="date", keep="last")
            eq_df = eq_df.sort_values("date").set_index("date")
            equity_curve = eq_df["equity"]
        else:
            equity_curve = pd.Series(dtype=float)

        # Calculate strategy metrics
        metrics = calc_all_metrics(
            equity_curve=equity_curve,
            trades=all_trades,
            positions_log=all_trades,
            rf=self._risk_free_rate,
        )

        # Phase1-Fix1: Calculate benchmark metrics (loads data independently)
        if all_dates is not None and len(all_dates) > 0:
            bench_start = all_dates[self.train_window]  # same start as first test
            bench_end = all_dates[-1]
            benchmark_metrics = self._calc_benchmark(bench_start, bench_end)
        else:
            benchmark_metrics = {}

        result = BacktestResult(
            trades=all_trades,
            equity_curve=equity_curve,
            metrics=metrics,
            benchmark_metrics=benchmark_metrics,
            walk_forward_windows=walk_forward_windows,
        )

        logger.info(f"Backtest complete: {len(all_trades)} total trades")
        logger.info(f"\n{result.summary()}")

        return result
