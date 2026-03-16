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
from backtest.vol_scaling import VolatilityScaler
from utils.allocation import build_allocation_pool, StrategyAllocationPool
from data.macro import download_macro_data, compute_macro_signals
from backtest.pipeline import DayContext, run_entry_gates, enrich_signals

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
            "-" * 60,
            "ALPHA DECOMPOSITION",
            "-" * 60,
            f"Alpha (ann):     {self.metrics.get('alpha', 0)*100:.2f}%",
            f"Beta:            {self.metrics.get('beta', 0):.4f}",
            f"R²:              {self.metrics.get('r_squared', 0)*100:.1f}%",
            f"Info Ratio:      {self.metrics.get('information_ratio', 0):.4f}",
            f"Up Capture:      {self.metrics.get('up_capture', 0)*100:.1f}%",
            f"Down Capture:    {self.metrics.get('down_capture', 0)*100:.1f}%",
            "-" * 60,
            "SHUFFLE ROBUSTNESS",
            "-" * 60,
            f"Shuffle DD p50:  {self.metrics.get('shuffle_p50_dd', 0)*100:.2f}%",
            f"Shuffle DD p95:  {self.metrics.get('shuffle_p95_dd', 0)*100:.2f}%",
            f"Shuffle pctile:  {self.metrics.get('shuffle_percentile', 0)*100:.0f}%"
            f" {self.metrics.get('shuffle_impact', '')}",
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
        # Task: Volume participation limit — skip entries where order > limit% of daily volume
        # Default 0.0 = disabled (backward-compatible)
        self.volume_participation_limit = self.backtest_config.get("volume_participation_limit", 0.0)

        # Risk parameters
        self.starting_equity = self.risk_config.get("starting_equity", 5000)
        self.leverage = self.risk_config.get("leverage", 1.0)
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
        # Task #124: Conditional portfolio volatility scaling
        self.vol_scaler = VolatilityScaler(config)

        # Fee parameters - Phase1-Fix2: smart commission model
        self.commission_per_trade = self.fees_config.get("commission_per_trade", 5.0)
        self.commission_pct = self.fees_config.get("commission_pct", 0.0008)
        self.slippage_pct = self.fees_config.get("slippage_pct", 0.001)
        # Task 3: Volume-aware slippage model
        self.slippage_model = self.fees_config.get("slippage_model", "fixed")
        self.slippage_impact_exponent = self.fees_config.get("slippage_impact_exponent", 0.5)
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

        # Per-strategy allocation pool (optional, disabled by default)
        self.allocation_pool: StrategyAllocationPool = build_allocation_pool(config)
        if self.allocation_pool.is_enabled():
            logger.info(
                "Allocation pools enabled: mode=%s, pools=%s",
                self.allocation_pool.mode,
                self.allocation_pool.pools,
            )

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

        # Macro regime filter — gold/copper, VIX ROC, yield curve (yfinance-sourced)
        # Mode "sizing": multiplies position sizes by macro_regime_scale
        # Mode "gate":   blocks entries when macro_regime_scale < 0.7
        # Mode "boost":  adds +0.05 confidence when macro_regime_scale > 1.2
        _macro_cfg = config.get("macro_regime", {})
        self.macro_regime_enabled = _macro_cfg.get("enabled", False)
        self.macro_gc_enabled = _macro_cfg.get("gold_copper", True)
        self.macro_vix_roc_enabled = _macro_cfg.get("vix_roc", True)
        self.macro_vix_roc_threshold = _macro_cfg.get("vix_roc_threshold", 0.30)
        self.macro_yc_enabled = _macro_cfg.get("yield_curve", True)
        self.macro_yc_threshold = _macro_cfg.get("yc_flattening_threshold", -0.10)
        self.macro_mode = _macro_cfg.get("mode", "sizing")  # "sizing" | "gate" | "boost"

        # Turn-of-Month (TOM) calendar filter
        # Supports: false (disabled), true (only trade in TOM window),
        #           "boost" (confidence boost during TOM window)
        _tom_cfg = config.get("turn_of_month", False)
        if isinstance(_tom_cfg, dict):
            self.tom_mode = _tom_cfg.get("mode", False)  # false/true/"boost"
            self.tom_days_before_end = _tom_cfg.get("days_before_month_end", 5)
            self.tom_days_after_start = _tom_cfg.get("days_after_month_start", 3)
            self.tom_confidence_boost = _tom_cfg.get("confidence_boost", 0.05)
        elif _tom_cfg in (True, "boost"):
            self.tom_mode = _tom_cfg
            self.tom_days_before_end = 5
            self.tom_days_after_start = 3
            self.tom_confidence_boost = 0.05
        else:
            self.tom_mode = False
            self.tom_days_before_end = 5
            self.tom_days_after_start = 3
            self.tom_confidence_boost = 0.05

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

        # FIX-4: EventCalendar integration
        if self.config.get("event_calendar", {}).get("enabled", False):
            try:
                from data.events import EventCalendar
                self.event_calendar = EventCalendar()
            except Exception as e:
                logger.warning(f"EventCalendar init failed: {e} — continuing without event calendar")
                self.event_calendar = None
        else:
            self.event_calendar = None

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

    def _apply_slippage(
        self, price: float, direction: str,
        order_shares: int = 0, bar_volume: float = 0,
    ) -> float:
        """Apply slippage to a price.

        For buys: price goes up (adverse).
        For sells: price goes down (adverse).

        In volume_aware mode, slippage scales with market participation:
            effective_slippage = slippage_pct * (participation ** impact_exponent)
        where participation = order_shares / bar_volume.  Falls back to fixed
        when bar_volume=0 or order_shares=0.
        """
        if (
            self.slippage_model == "volume_aware"
            and bar_volume > 0
            and order_shares > 0
        ):
            participation = order_shares / bar_volume
            effective_slippage = self.slippage_pct * (
                participation ** self.slippage_impact_exponent
            )
            effective_slippage = max(0.0001, min(0.02, effective_slippage))
        else:
            effective_slippage = self.slippage_pct

        if direction == "buy":
            return price * (1 + effective_slippage)
        else:
            return price * (1 - effective_slippage)

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

    def _build_trade_record(
        self, pos: Dict[str, Any], fill_price: float,
        today: pd.Timestamp, exit_reason: str,
    ) -> Dict[str, Any]:
        """Build a closed-trade record dict from a position and fill price."""
        # FIX-1: Direction-aware P&L — short profits when price falls
        if pos.get("direction", "long") == "short":
            gross_pnl = (pos["fill_price"] - fill_price) * pos["shares"]
        else:
            gross_pnl = (fill_price - pos["fill_price"]) * pos["shares"]
        exit_commission = self._calc_commission(pos["shares"] * fill_price)
        total_commission = pos["entry_commission"] + exit_commission
        net_pnl = gross_pnl - total_commission
        hold_days = (today - pd.Timestamp(pos["entry_date"])).days
        trade = {
            "ticker": pos["ticker"],
            "strategy": pos["strategy"],
            "direction": pos.get("direction", "long"),  # FIX-1: preserve actual direction
            "entry_date": pos["entry_date"],
            "entry_price": pos["fill_price"],
            "exit_date": today,
            "exit_price": fill_price,
            "shares": pos["shares"],
            "position_value": pos["position_value"],
            "gross_pnl": round(gross_pnl, 2),
            "commission": round(total_commission, 2),
            "pnl": round(net_pnl, 2),
            "return_pct": round(net_pnl / pos["position_value"] * 100, 2)
            if pos["position_value"] > 0 else 0.0,
            "hold_days": hold_days,
            "exit_reason": exit_reason,
            "mae": pos.get("mae", 0.0),
            "mfe": pos.get("mfe", 0.0),
            "stop_price": pos.get("stop_price", 0.0),
            "confidence": pos.get("confidence", 0.0),
            "features": pos.get("features", {}),
            "entry_regime": pos.get("entry_regime", "neutral"),
        }
        trade["r_multiple"] = calc_r_multiple(trade)
        return trade

    def _process_strategy_exits(
        self, day_idx: int, today: pd.Timestamp,
        trading_dates: pd.DatetimeIndex,
        data: Dict[str, pd.DataFrame],
        strategies: List[BaseStrategy],
        equity: float, open_positions: List[Dict[str, Any]],
        closed_trades: List[Dict[str, Any]],
    ) -> float:
        """Check strategy exit conditions and execute exits. Returns updated equity."""
        if day_idx <= 0:
            return equity
        yesterday = trading_dates[day_idx - 1]
        exit_data = {}
        for t, df in data.items():
            n = df.index.searchsorted(yesterday, side='right')
            if n > 0:
                exit_data[t] = df.iloc[:n]

        for strategy in strategies:
            exit_recs = strategy.check_exits(exit_data, open_positions)
            for rec in exit_recs:
                ticker = rec["ticker"]
                pos_idx = next(
                    (i for i, p in enumerate(open_positions)
                     if p["ticker"] == ticker and p["strategy"] == strategy.name),
                    None,
                )
                if pos_idx is None:
                    continue
                pos = open_positions[pos_idx]
                today_df = data.get(ticker)
                if today_df is None or today not in today_df.index:
                    fill_price = self._apply_slippage(rec["exit_price"], "sell")
                else:
                    _bar_vol_exit = (
                        float(today_df.loc[today, "volume"])
                        if "volume" in today_df.columns else 0.0
                    )
                    fill_price = self._apply_slippage(
                        today_df.loc[today, "open"], "sell",
                        order_shares=pos["shares"], bar_volume=_bar_vol_exit,
                    )

                trade = self._build_trade_record(pos, fill_price, today, rec["reason"])
                closed_trades.append(trade)
                equity += trade["pnl"]
                open_positions.pop(pos_idx)
                logger.debug(
                    f"EXIT {ticker} ({rec['reason']}): "
                    f"pnl=${trade['pnl']:.2f}, equity=${equity:.2f}"
                )
        return equity

    def _update_mae_mfe(
        self, today: pd.Timestamp, data: Dict[str, pd.DataFrame],
        open_positions: List[Dict[str, Any]],
    ) -> None:
        """Update MAE/MFE excursions for all open positions.

        MAE (Maximum Adverse Excursion) stored as a negative fraction (worst loss).
        MFE (Maximum Favorable Excursion) stored as a positive fraction (best gain).

        FIX-1: Direction-aware — for shorts, price rising is adverse (MAE)
        and price falling is favorable (MFE).
        """
        for pos in open_positions:
            ticker = pos["ticker"]
            today_df = data.get(ticker)
            if today_df is None or today not in today_df.index:
                continue
            fill_price = pos["fill_price"]
            if fill_price <= 0:
                continue
            bar = today_df.loc[today]
            # FIX-1: direction-aware adverse/favorable excursions
            if pos.get("direction", "long") == "short":
                # Short: adverse = price rose above entry (high > fill), stored negative
                adverse = (fill_price - bar["high"]) / fill_price
                # Short: favorable = price fell below entry (low < fill), stored positive
                favorable = (fill_price - bar["low"]) / fill_price
            else:
                # Long: adverse = price fell below entry (low < fill), stored negative
                adverse = (bar["low"] - fill_price) / fill_price
                # Long: favorable = price rose above entry (high > fill), stored positive
                favorable = (bar["high"] - fill_price) / fill_price
            pos["mae"] = min(pos.get("mae", 0.0), adverse)
            pos["mfe"] = max(pos.get("mfe", 0.0), favorable)

    def _process_max_loss_exits(
        self, day_idx: int, today: pd.Timestamp,
        trading_dates: pd.DatetimeIndex,
        data: Dict[str, pd.DataFrame],
        equity: float, open_positions: List[Dict[str, Any]],
        closed_trades: List[Dict[str, Any]],
    ) -> float:
        """Apply max-loss-per-trade cap. Returns updated equity."""
        if self.max_loss_per_trade is None or not open_positions:
            return equity
        _yest = trading_dates[day_idx - 1] if day_idx > 0 else None
        exits = []  # (pos_idx, exit_price)
        for pi, pos in enumerate(open_positions):
            ticker = pos["ticker"]
            df = data.get(ticker)
            if df is None or today not in df.index:
                continue
            if _yest is None or _yest not in df.index:
                continue
            yest_close = df.loc[_yest, "close"]
            # FIX-1: direction-aware unrealized P&L
            if pos.get("direction", "long") == "short":
                unrealized = (pos["fill_price"] - yest_close) * pos["shares"]
            else:
                unrealized = (yest_close - pos["fill_price"]) * pos["shares"]
            if unrealized <= -abs(self.max_loss_per_trade):
                _exit_vol = float(df.loc[today, "volume"]) if "volume" in df.columns else 0.0
                exits.append((pi, df.loc[today, "close"], _exit_vol))
                logger.debug(
                    f"MAX_LOSS_CAP {ticker}: unrealized ${unrealized:.2f} "
                    f"<= -${abs(self.max_loss_per_trade):.2f}"
                )

        for pi, exit_price, _bar_vol in reversed(exits):
            pos = open_positions[pi]
            # FIX-1: shorts exit by buying to cover (adverse slippage = price rises)
            _exit_side = "sell" if pos.get("direction", "long") == "long" else "buy"
            fill = self._apply_slippage(
                exit_price, _exit_side,
                order_shares=pos["shares"], bar_volume=_bar_vol,
            )
            trade = self._build_trade_record(pos, fill, today, "max_loss_cap")
            closed_trades.append(trade)
            equity += trade["pnl"]
            open_positions.pop(pi)
            logger.debug(f"MAX_LOSS_CAP EXIT {pos['ticker']}: pnl=${trade['pnl']:.2f}, equity=${equity:.2f}")
        return equity

    def _process_trailing_stops(
        self, day_idx: int, today: pd.Timestamp,
        trading_dates: pd.DatetimeIndex,
        data: Dict[str, pd.DataFrame],
        equity: float, open_positions: List[Dict[str, Any]],
        closed_trades: List[Dict[str, Any]],
    ) -> float:
        """Update trailing stop state and execute triggered exits. Returns updated equity."""
        if not self.trailing_stop_enabled or not open_positions:
            return equity
        _yest = trading_dates[day_idx - 1] if day_idx > 0 else None
        trail_exits = []  # (pos_idx, exit_price)
        for pi, pos in enumerate(open_positions):
            ticker = pos["ticker"]
            df = data.get(ticker)
            if df is None or today not in df.index:
                continue
            today_high = df.loc[today, "high"]
            today_low = df.loc[today, "low"]
            today_close = df.loc[today, "close"]
            today_volume = float(df.loc[today, "volume"]) if "volume" in df.columns else 0.0
            fill = pos["fill_price"]
            yest_close = (df.loc[_yest, "close"]
                          if _yest is not None and _yest in df.index else None)
            direction = pos.get("direction", "long")

            # Get ATR
            atr = pos.get("features", {}).get("atr", 0.0) or 0.0
            if atr <= 0:
                mask = df.index <= today
                w = df.loc[mask].tail(15)
                if len(w) >= 3:
                    atr = float((w["high"] - w["low"]).abs().rolling(14, min_periods=3).mean().iloc[-1])
                if atr <= 0:
                    continue

            trail_active = pos.get("trailing_stop_active", False)

            if direction == "short":
                # FIX-1: Short trailing stop — track LOWEST price, trigger on RISE
                # Activation: price dropped below entry by activation_pct
                if not trail_active and fill > 0 and (fill - today_low) / fill >= self.trail_activation_pct:
                    trail_active = True
                    pos["trailing_stop_active"] = True
                    pos["lowest_price"] = today_low
                    pos["trailing_stop_price"] = today_low + self.trail_atr_multiplier * atr
                    logger.debug(f"TRAIL ACTIVATED (short) {ticker}: low={today_low:.3f}, "
                                 f"trail_stop={pos['trailing_stop_price']:.3f}")

                if trail_active:
                    new_low = min(pos.get("lowest_price", today_low), today_low)
                    pos["lowest_price"] = new_low
                    # Trail stop moves DOWN as price falls (tracks ATR above lowest)
                    new_trail = new_low + self.trail_atr_multiplier * atr
                    # For shorts, trailing stop is a ceiling — take the LOWER of existing and new
                    # (stop moves down with price, protecting profit)
                    pos["trailing_stop_price"] = min(
                        pos.get("trailing_stop_price", pos["stop_price"]),
                        new_trail, pos["stop_price"],
                    )
                    # Trigger when yesterday's close RISES above the trailing stop ceiling
                    if yest_close is not None and yest_close >= pos["trailing_stop_price"]:
                        trail_exits.append((pi, today_close, today_volume))
                        logger.debug(f"TRAIL EXIT (short) {ticker}: yest_close={yest_close:.3f} "
                                     f">= trail_stop={pos['trailing_stop_price']:.3f}")
            else:
                # Existing long logic — unchanged
                # Check activation
                if not trail_active and (today_high - fill) / fill >= self.trail_activation_pct:
                    trail_active = True
                    pos["trailing_stop_active"] = True
                    pos["highest_price"] = today_high
                    pos["trailing_stop_price"] = today_high - self.trail_atr_multiplier * atr
                    logger.debug(f"TRAIL ACTIVATED {ticker}: high={today_high:.3f}, "
                                 f"trail_stop={pos['trailing_stop_price']:.3f}")

                if trail_active:
                    new_high = max(pos.get("highest_price", today_high), today_high)
                    pos["highest_price"] = new_high
                    new_trail = new_high - self.trail_atr_multiplier * atr
                    pos["trailing_stop_price"] = max(
                        pos.get("trailing_stop_price", pos["stop_price"]),
                        new_trail, pos["stop_price"],
                    )
                    if yest_close is not None and yest_close <= pos["trailing_stop_price"]:
                        trail_exits.append((pi, today_close, today_volume))
                        logger.debug(f"TRAIL EXIT {ticker}: yest_close={yest_close:.3f} "
                                     f"<= trail_stop={pos['trailing_stop_price']:.3f}")

        for pi, exit_price, _bar_vol in reversed(trail_exits):
            pos = open_positions[pi]
            # FIX-1: shorts exit by buying to cover (adverse slippage = price rises)
            _exit_side = "sell" if pos.get("direction", "long") == "long" else "buy"
            fill = self._apply_slippage(
                exit_price, _exit_side,
                order_shares=pos["shares"], bar_volume=_bar_vol,
            )
            # Override stop_price to show trailing stop value
            saved_stop = pos.get("stop_price", 0.0)
            pos["stop_price"] = pos.get("trailing_stop_price", saved_stop)
            trade = self._build_trade_record(pos, fill, today, "trailing_stop")
            pos["stop_price"] = saved_stop  # restore
            closed_trades.append(trade)
            equity += trade["pnl"]
            open_positions.pop(pi)
            logger.debug(f"TRAIL EXIT {pos['ticker']}: pnl=${trade['pnl']:.2f}, equity=${equity:.2f}")
        return equity

    def _compute_regime(
        self, today: pd.Timestamp, data: Dict[str, pd.DataFrame],
        breadth_series: Optional[pd.DataFrame],
    ) -> tuple:
        """Compute regime filter state. Returns (regime_str, regime_scale).

        Always classifies regime (bull/neutral/bear) for trade tagging.
        Only applies scaling when regime_filter.enabled is True.
        """
        _rf_cfg = self.config.get("regime_filter", {})
        filter_enabled = _rf_cfg.get("enabled", False)

        # Phase 3: 3-state regime (Bull/Neutral/Bear) using benchmark MA + breadth.
        regime, regime_scale = "neutral", 1.0
        ma_period = _rf_cfg.get("benchmark_ma_period", 50)
        bull_thresh = _rf_cfg.get("breadth_bull_threshold", 50.0)
        bear_thresh = _rf_cfg.get("breadth_bear_threshold", 40.0)
        scales = {
            "bull": _rf_cfg.get("bull_scale", 1.0),
            "neutral": _rf_cfg.get("neutral_scale", 0.75),
            "bear": _rf_cfg.get("bear_scale", 0.5),
        }

        # Signal 1: Benchmark above/below MA
        bench_df = data.get(self.benchmark_ticker)
        bench_above_ma = None
        if bench_df is not None and today in bench_df.index:
            bench_close = bench_df.loc[:today, "close"]
            if len(bench_close) >= ma_period:
                bench_ma = bench_close.rolling(window=ma_period).mean().iloc[-1]
                if not pd.isna(bench_ma):
                    bench_above_ma = bool(bench_close.iloc[-1] >= bench_ma)

        # Signal 2: Market breadth — % stocks above 200-day MA
        breadth_pct200 = None
        if breadth_series is not None and today in breadth_series.index:
            raw = breadth_series.loc[today].get("pct_above_200ma", None)
            if raw is not None and not pd.isna(raw):
                breadth_pct200 = float(raw)

        # Classify
        if bench_above_ma is not None and breadth_pct200 is not None:
            if bench_above_ma and breadth_pct200 >= bull_thresh:
                regime = "bull"
            elif not bench_above_ma and breadth_pct200 < bear_thresh:
                regime = "bear"
            else:
                regime = "neutral"
        elif bench_above_ma is not None:
            regime = "bull" if bench_above_ma else "bear"

        # Only apply scaling when filter is enabled; otherwise info-only
        if filter_enabled:
            regime_scale = scales[regime]
        else:
            regime_scale = 1.0

        b200_str = f"{breadth_pct200:.1f}%" if breadth_pct200 is not None else "N/A"
        logger.debug(
            f"REGIME {today.date()}: {regime.upper()} "
            f"(bench_above_{ma_period}MA={bench_above_ma}, "
            f"breadth200={b200_str}, scale={regime_scale:.2f})"
        )
        return regime, regime_scale

    def _is_tom_window(self, date: pd.Timestamp, trading_dates: pd.DatetimeIndex) -> bool:
        """Check if date falls within the Turn-of-Month window.

        TOM window = last N trading days of month + first M trading days of next month.
        Uses the actual trading calendar (trading_dates), not calendar days.
        """
        month = date.month
        year = date.year

        # Trading days in the same month as `date`
        same_month = trading_dates[(trading_dates.month == month) & (trading_dates.year == year)]
        if len(same_month) == 0:
            return False

        # Check: is date within the last N trading days of its month?
        last_n = same_month[-self.tom_days_before_end:]
        if date in last_n:
            return True

        # Check: is date within the first M trading days of its month?
        first_m = same_month[:self.tom_days_after_start]
        if date in first_m:
            return True

        return False

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
        macro_signals: Optional[pd.DataFrame] = None,
    ) -> float:
        """Simulate one trading day — orchestrator calling sub-methods.

        Returns updated equity value.
        """
        today = trading_dates[day_idx]

        # 1. Strategy-driven exits
        equity = self._process_strategy_exits(
            day_idx, today, trading_dates, data, strategies,
            equity, open_positions, closed_trades)

        # 2. MAE/MFE tracking
        self._update_mae_mfe(today, data, open_positions)

        # 3. Max-loss-per-trade cap
        equity = self._process_max_loss_exits(
            day_idx, today, trading_dates, data,
            equity, open_positions, closed_trades)

        # 4. Trailing stops
        equity = self._process_trailing_stops(
            day_idx, today, trading_dates, data,
            equity, open_positions, closed_trades)

        # 5. Regime filter
        regime, regime_scale = self._compute_regime(today, data, breadth_series)

        # 6. Generate new entry signals and execute entries
        # --- Generate new entry signals ---
        if day_idx > 0 and len(open_positions) < self.max_positions:  # regime_scale applied in sizing
            yesterday = trading_dates[day_idx - 1]

            # ── FIX-3: Entry gate filters via pipeline orchestrator ───────────
            ctx = DayContext(
                today=today,
                yesterday=yesterday,
                day_idx=day_idx,
                equity=equity,
                open_positions=open_positions,
                closed_trades=closed_trades,
                data=data,
                trading_dates=trading_dates,   # FIX-3: pass authoritative calendar
                vix_series=vix_series,
                breadth_series=breadth_series,
                rs_data=rs_data,
                macro_signals=macro_signals,
                fred_yield_curve=fred_yield_curve,
                fred_claims=fred_claims,
                equity_history=equity_history,
                regime=regime,
                regime_scale=regime_scale,
            )
            run_entry_gates(ctx, self.config)

            # Build data windows up to yesterday for signal generation
            signal_data = {}
            for ticker, df in data.items():
                n = df.index.searchsorted(yesterday, side='right')
                if n >= self.min_history:
                    signal_data[ticker] = df.iloc[:n]

            for strategy in strategies:
                if len(open_positions) >= self.max_positions:
                    break
                if ctx.any_gate_blocked:
                    break

                signals = strategy.generate_signals(
                    signal_data, equity * self.leverage, open_positions
                )

                # ── FIX-3: Signal enrichment via pipeline orchestrator ────────
                enrich_signals(signals, ctx, self.config)

                # FIX-4: EventCalendar feature injection
                if self.event_calendar is not None:
                    try:
                        from backtest.enrichment import inject_event_features
                        inject_event_features(signals, today, self.event_calendar)
                    except ImportError:
                        pass  # Builder 3 creates inject_event_features; safe to skip until then

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

                    # Per-strategy allocation pool check
                    if self.allocation_pool.is_enabled():
                        pool_ok, pool_reason = self.allocation_pool.can_accept(
                            signal.strategy, open_positions
                        )
                        if not pool_ok:
                            logger.debug(
                                f"SKIP {ticker} ({signal.strategy}): {pool_reason}"
                            )
                            continue

                    # Get today's open for fill
                    today_df = data.get(ticker)
                    if today_df is None or today not in today_df.index:
                        continue

                    today_open = today_df.loc[today, "open"]
                    # FIX-1: direction-aware entry slippage
                    # Long entry (buy): slippage raises price (adverse)
                    # Short entry (sell): slippage lowers price (adverse)
                    _entry_side = "buy" if signal.direction == "long" else "sell"
                    # order_shares=0 at entry: shares not computed yet, falls back to fixed
                    _bar_vol_entry = (
                        float(today_df.loc[today, "volume"])
                        if "volume" in today_df.columns else 0.0
                    )
                    fill_price = self._apply_slippage(
                        today_open, _entry_side, order_shares=0, bar_volume=_bar_vol_entry
                    )

                    if fill_price <= 0:
                        continue

                    # Recalculate position size at actual fill price
                    # Adjust stop proportionally
                    price_ratio = fill_price / signal.entry_price
                    adjusted_stop = signal.stop_price * price_ratio

                    # FIX-1: direction-aware stop check
                    # Long: stop below entry — skip if fill already below stop
                    # Short: stop above entry — skip if fill already above stop
                    if signal.direction == "short":
                        if fill_price >= adjusted_stop:
                            continue  # stop already hit for short
                    else:
                        if fill_price <= adjusted_stop:
                            continue  # stop would be above entry for long

                    # FIX-1: direction-aware risk-per-share (always positive)
                    if signal.direction == "short":
                        risk_per_share = adjusted_stop - fill_price
                    else:
                        risk_per_share = fill_price - adjusted_stop
                    # Phase 8D: Dynamic position sizing
                    _atr = signal.features.get('atr', 0.0) if hasattr(signal, 'features') and signal.features else 0.0
                    _risk_pct = self.dynamic_sizer.calculate_risk_pct(
                        confidence=signal.confidence,
                        atr=_atr,
                        price=fill_price,
                        equity_history=equity_history,
                    )
                    # Phase 3: regime scaling + macro regime scaling (task #77)
                    # FIX-3: use ctx.macro_scale from pipeline gate results
                    _effective_macro_scale = (
                        ctx.macro_scale
                        if self.macro_regime_enabled and self.macro_mode == "sizing"
                        else 1.0
                    )
                    vol_scale = self.vol_scaler.scale_factor() if self.vol_scaler.enabled else 1.0
                    risk_budget = equity * self.leverage * _risk_pct * regime_scale * _effective_macro_scale * vol_scale
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

                    # Don't exceed available buying power (equity × leverage)
                    invested = sum(
                        p["position_value"] for p in open_positions
                    )
                    available = equity * self.leverage - invested
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

                    # Volume participation check — skip if order too large vs daily volume
                    if self.volume_participation_limit > 0:
                        bar_volume = today_df.loc[today, "volume"]
                        if bar_volume > 0 and shares > bar_volume * self.volume_participation_limit:
                            logger.debug(
                                f"SKIP {ticker}: position {shares} shares > "
                                f"{self.volume_participation_limit*100:.1f}% of volume "
                                f"({bar_volume:.0f})"
                            )
                            continue

                    # Entry commission
                    entry_commission = self._calc_commission(position_value)

                    # Create position record
                    position = {
                        "ticker": ticker,
                        "strategy": signal.strategy,
                        "direction": signal.direction,  # FIX-1: preserve actual direction
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
                        "entry_regime": regime,
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
                _fc_bar_vol = (
                    float(today_df.loc[close_date, "volume"])
                    if "volume" in today_df.columns else 0.0
                )
            elif today_df is not None and len(today_df) > 0:
                close_price = today_df["close"].iloc[-1]
                _fc_bar_vol = (
                    float(today_df["volume"].iloc[-1])
                    if "volume" in today_df.columns else 0.0
                )
            else:
                close_price = pos["fill_price"]  # fallback
                _fc_bar_vol = 0.0

            # FIX-1: shorts exit by buying to cover (adverse slippage = price rises)
            _exit_side = "sell" if pos.get("direction", "long") == "long" else "buy"
            fill_price = self._apply_slippage(
                close_price, _exit_side,
                order_shares=pos["shares"], bar_volume=_fc_bar_vol,
            )
            exit_value = pos["shares"] * fill_price
            exit_commission = self._calc_commission(exit_value)

            # FIX-1: Direction-aware P&L
            if pos.get("direction", "long") == "short":
                gross_pnl = (pos["fill_price"] - fill_price) * pos["shares"]
            else:
                gross_pnl = (fill_price - pos["fill_price"]) * pos["shares"]
            total_commission = pos["entry_commission"] + exit_commission
            net_pnl = gross_pnl - total_commission
            hold_days = (close_date - pd.Timestamp(pos["entry_date"])).days

            trade = {
                "ticker": ticker,
                "strategy": pos["strategy"],
                "direction": pos.get("direction", "long"),  # FIX-1: preserve actual direction
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
                "entry_regime": pos.get("entry_regime", "neutral"),
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
            return {"cagr": 0, "max_drawdown": 0, "sharpe": 0, "total_return": 0}, pd.Series(dtype=float)

        mask = (bench_df.index >= start) & (bench_df.index <= end)
        bench = bench_df.loc[mask, "close"]

        if len(bench) < 10:
            return {"cagr": 0, "max_drawdown": 0, "sharpe": 0, "total_return": 0}, pd.Series(dtype=float)

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

        metrics_dict = {
            "cagr": calc_cagr(bench_equity),
            "max_drawdown": calc_max_drawdown(bench_equity),
            "sharpe": calc_sharpe(bench_returns),
            "sortino": calc_sortino(bench_returns),
            "total_return": round(
                (bench.iloc[-1] / bench.iloc[0]) - 1, 4
            ),
            "final_equity": round(bench_equity.iloc[-1], 2),
        }
        return metrics_dict, bench_returns

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

        # Macro regime data (gold/copper, VIX ROC, yield curve) — task #77
        macro_signals_df = None
        if self.macro_regime_enabled:
            try:
                _macro_start = all_dates[0].strftime("%Y-%m-%d")
                logger.info(
                    f"Macro regime enabled (mode={self.macro_mode}): "
                    f"downloading macro data from {_macro_start}"
                )
                _macro_raw = download_macro_data(
                    start_date=_macro_start,
                    use_cache=True,
                    cache_max_age_hours=24,
                )
                if not _macro_raw.empty:
                    macro_signals_df = compute_macro_signals(
                        _macro_raw,
                        vix_roc_threshold=self.macro_vix_roc_threshold,
                        yc_flattening_threshold=self.macro_yc_threshold,
                    )
                    logger.info(
                        f"Macro signals ready: {len(macro_signals_df)} days, "
                        f"scale range [{macro_signals_df['macro_regime_scale'].min():.2f}, "
                        f"{macro_signals_df['macro_regime_scale'].max():.2f}]"
                    )
                else:
                    logger.warning("Macro regime enabled but download returned empty — disabled")
            except Exception as e:
                logger.warning(f"Macro regime setup failed: {e} — continuing without macro")
                macro_signals_df = None

        # Inject benchmark ticker into data dict for regime classification
        if self.benchmark_ticker and self.benchmark_ticker not in data:
            try:
                bench_df = download_ticker(
                    self.benchmark_ticker, use_cache=True, market_id=self.market_id
                )
                if bench_df is not None and not bench_df.empty:
                    data[self.benchmark_ticker] = bench_df
                    logger.info(
                        f"Loaded benchmark {self.benchmark_ticker} for regime "
                        f"classification ({len(bench_df)} rows)"
                    )
            except Exception as e:
                logger.warning(f"Could not load benchmark {self.benchmark_ticker}: {e}")

        # Pre-compute strategy indicators once on full data
        for strategy in strategies:
            strategy.precompute(data)

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

            # Point-in-time universe filtering (survivorship bias elimination)
            if self.config.get("universe", {}).get("point_in_time", False):
                try:
                    from data.sp500_history import get_members_at_date
                    pit_members = get_members_at_date(test_start.date() if hasattr(test_start, 'date') else test_start)
                    pre_filter = len(window_data)
                    window_data = {t: df for t, df in window_data.items() if t in pit_members}
                    logger.info(
                        f"PIT filter: {len(window_data)}/{pre_filter} tickers "
                        f"for window starting {test_start.date() if hasattr(test_start, 'date') else test_start}"
                    )
                except Exception as e:
                    logger.warning(f"PIT filtering failed, using full data: {e}")

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
                    macro_signals=macro_signals_df,
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
                # Task #124: Feed daily return into volatility scaler
                if len(equity_history) >= 2 and equity_history[-2] > 0:
                    self.vol_scaler.update(
                        (mtm_value - equity_history[-2]) / equity_history[-2]
                    )

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

        # Phase1-Fix1: Calculate benchmark metrics first (needed for alpha/beta calc)
        bench_returns = None
        if all_dates is not None and len(all_dates) > 0:
            bench_start = all_dates[self.train_window]  # same start as first test
            bench_end = all_dates[-1]
            benchmark_metrics, bench_returns = self._calc_benchmark(bench_start, bench_end)
        else:
            benchmark_metrics = {}

        # Calculate strategy metrics (pass benchmark_returns for alpha/beta decomposition)
        # benchmark_returns is optional — handled gracefully if metrics version doesn't
        # support it yet (backward-compatible with older calc_all_metrics signature)
        _metrics_extra = {}
        if bench_returns is not None:
            _metrics_extra["benchmark_returns"] = bench_returns
        try:
            metrics = calc_all_metrics(
                equity_curve=equity_curve,
                trades=all_trades,
                positions_log=all_trades,
                rf=self._risk_free_rate,
                **_metrics_extra,
            )
        except TypeError:
            # calc_all_metrics doesn't accept benchmark_returns yet
            metrics = calc_all_metrics(
                equity_curve=equity_curve,
                trades=all_trades,
                positions_log=all_trades,
                rf=self._risk_free_rate,
            )

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

    def run_walkforward_multioffset(
        self,
        data: Dict[str, pd.DataFrame],
        strategies: List[BaseStrategy],
        offsets: Optional[List[int]] = None,
        n_offsets: int = 5,
    ) -> Dict[str, Any]:
        """Run walk-forward backtests with multiple start-date offsets for stability testing.

        Measures how sensitive backtest results are to the exact data start date
        by trimming different amounts from the beginning and checking how much
        key metrics vary (especially trade count, which should be stable in a
        robust strategy).

        Args:
            data:       Dict mapping ticker -> DataFrame with OHLCV data.
            strategies: List of strategy instances to run.
            offsets:    Explicit list of integer day offsets to trim from the
                        start of every ticker's data.  If None, defaults to
                        ``[0, 5, 10, …]`` with ``n_offsets`` steps of 5 days.
            n_offsets:  Number of 5-day offsets to use when ``offsets`` is None.
                        Default 5 → offsets [0, 5, 10, 15, 20].

        Returns:
            Dict with stability metrics::

                {
                    'median_sharpe': float,
                    'mean_sharpe':   float,
                    'std_sharpe':    float,
                    'median_trades': float,
                    'mean_trades':   float,
                    'std_trades':    float,
                    'cv_sharpe':     float,  # std / |mean|
                    'cv_trades':     float,  # std / mean
                    'stable':        bool,   # True if cv_trades < 0.30
                    'per_offset':    list,   # per-run result dicts
                }
        """
        if offsets is None:
            offsets = [i * 5 for i in range(n_offsets)]  # [0, 5, 10, 15, 20]

        # Inject benchmark ticker into data dict for regime classification
        if self.benchmark_ticker and self.benchmark_ticker not in data:
            try:
                bench_df = download_ticker(
                    self.benchmark_ticker, use_cache=True, market_id=self.market_id
                )
                if bench_df is not None and not bench_df.empty:
                    data[self.benchmark_ticker] = bench_df
                    logger.info(
                        f"Loaded benchmark {self.benchmark_ticker} for regime "
                        f"classification ({len(bench_df)} rows)"
                    )
            except Exception as e:
                logger.warning(f"Could not load benchmark {self.benchmark_ticker}: {e}")

        # Pre-compute strategy indicators once on full data
        for strategy in strategies:
            strategy.precompute(data)

        per_offset: List[Dict[str, Any]] = []

        for offset in offsets:
            # Trim `offset` trading days from the START of every ticker's data
            trimmed: Dict[str, pd.DataFrame] = {}
            for ticker, df in data.items():
                if len(df) > offset:
                    trimmed[ticker] = df.iloc[offset:].copy()
                # If offset >= len(df) that ticker is silently dropped

            if not trimmed:
                logger.warning(
                    f"MultiOffset offset={offset}: no data remaining after trim, skipping"
                )
                per_offset.append({
                    "offset": offset,
                    "sharpe": 0.0,
                    "trades": 0,
                    "cagr": 0.0,
                    "max_drawdown": 0.0,
                    "error": "no data after trim",
                })
                continue

            try:
                result = self.run_walkforward(trimmed, strategies)
                m = result.metrics
                per_offset.append({
                    "offset": offset,
                    "sharpe": float(m.get("sharpe", 0.0)),
                    "trades": int(m.get("total_trades", 0)),
                    "cagr": float(m.get("cagr", 0.0)),
                    "max_drawdown": float(m.get("max_drawdown", 0.0)),
                })
            except Exception as exc:
                logger.warning(f"MultiOffset offset={offset}: backtest failed: {exc}")
                per_offset.append({
                    "offset": offset,
                    "sharpe": 0.0,
                    "trades": 0,
                    "cagr": 0.0,
                    "max_drawdown": 0.0,
                    "error": str(exc),
                })

        # Aggregate statistics across all offsets
        sharpes = [r["sharpe"] for r in per_offset]
        trades = [r["trades"] for r in per_offset]

        median_sharpe = float(np.median(sharpes))
        mean_sharpe = float(np.mean(sharpes))
        std_sharpe = float(np.std(sharpes))

        median_trades = float(np.median(trades))
        mean_trades = float(np.mean(trades))
        std_trades = float(np.std(trades))

        # Coefficient of variation (lower = more stable)
        cv_sharpe = (std_sharpe / abs(mean_sharpe)
                     if abs(mean_sharpe) > 1e-9 else float("inf"))
        cv_trades = (std_trades / mean_trades
                     if mean_trades > 1e-9 else float("inf"))

        return {
            "median_sharpe": round(median_sharpe, 4),
            "mean_sharpe": round(mean_sharpe, 4),
            "std_sharpe": round(std_sharpe, 4),
            "median_trades": round(median_trades, 1),
            "mean_trades": round(mean_trades, 1),
            "std_trades": round(std_trades, 1),
            "cv_sharpe": round(cv_sharpe, 4),
            "cv_trades": round(cv_trades, 4),
            "stable": bool(cv_trades < 0.30),
            "per_offset": per_offset,
        }
