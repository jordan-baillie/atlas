"""
regime/backtest.py — Regime-aware backtest wrapper for Atlas.

Wraps BacktestEngine with macro-regime awareness: for each contiguous block
of the same regime state (from ``regime_history``), runs a sub-backtest using
only the universes and strategies permitted by that regime, with position
sizes scaled by the regime's ``sizing_multiplier``.

Approach
--------
**(a) Sequential sub-backtests** — one BacktestEngine run per regime window.
    Positions do **not** carry across regime boundaries in this approach.
    This is the chosen implementation: simpler, sufficient for validation.

Future improvement: (b) single BacktestEngine run with per-day data
filtering injected inside the walk-forward loop — more accurate but
requires modifying BacktestEngine internals.

IMPORTANT: start_date defaults to 2019-01-01 (ETF data availability).
Do not go back to 2015 — ETF universe data (sector_etfs, gold_etfs, etc.)
only starts from ~2019.

Usage
-----
    import json
    from regime.backtest import RegimeAwareBacktest

    config = json.load(open("config/active/sp500.json"))
    bt = RegimeAwareBacktest(config, start_date="2019-01-01")
    result = bt.run()
    print(result.regime_distribution)

    # Or compare with SP500-only (no regime filtering)
    comparison = bt.compare_with_sp500_only()
    print(comparison["delta"])
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from backtest.engine import BacktestEngine, BacktestResult
from regime.states import REGIME_CONFIGS, RegimeState

logger = logging.getLogger(__name__)

# Project root — two levels up from regime/backtest.py
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class RegimeBacktestResult:
    """Full output of a regime-aware backtest run.

    Attributes
    ----------
    result : BacktestResult
        Aggregated result across all regime windows (trades, equity curve,
        metrics).
    regime_windows : list
        One dict per regime window::

            {start, end, regime, universes, sizing_multiplier, trades}

    regime_distribution : dict
        ``{state_value: window_count}`` — how many regime windows fell into
        each state.
    comparison_vs_sp500 : dict
        Populated by :meth:`RegimeAwareBacktest.compare_with_sp500_only`.
        Contains ``{delta: {metric: float}, sp500_metrics: dict}``.
    """

    result: BacktestResult
    regime_windows: List[Dict[str, Any]] = field(default_factory=list)
    regime_distribution: Dict[str, int] = field(default_factory=dict)
    comparison_vs_sp500: Dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Main class
# ─────────────────────────────────────────────────────────────────────────────


class RegimeAwareBacktest:
    """Regime-aware backtest wrapper.

    For each contiguous block of the same macro regime in ``regime_history``,
    loads data only for that regime's permitted universes, filters strategies
    to the regime's allowed types, applies the sizing multiplier, and runs a
    sub-backtest via ``BacktestEngine.run_walkforward()``.

    Results are aggregated into a single :class:`RegimeBacktestResult`.

    Parameters
    ----------
    config : dict
        Trading config dict (e.g. loaded from ``config/active/sp500.json``).
    start_date : str
        Backtest start date (ISO).  Defaults to ``"2019-01-01"`` — the
        earliest date for which ETF universe data is available.
    end_date : str, optional
        Backtest end date (ISO).  Defaults to today.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        start_date: str = "2019-01-01",
        end_date: Optional[str] = None,
    ) -> None:
        self.config = config
        self.start_date = start_date
        self.end_date = end_date or datetime.today().strftime("%Y-%m-%d")
        self.market_id = config.get("market", "sp500")

        # Resolve the OHLCV cache directory from config (default: data/cache)
        cache_rel = config.get("data", {}).get("cache_dir", "data/cache")
        self._cache_dir = _PROJECT_ROOT / cache_rel

    # ── Data loading ──────────────────────────────────────────────────────────

    def _load_universe_data(
        self,
        universes: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Load OHLCV data for all tickers in the given universes from cache.

        Reads ``data/cache/<universe>/<TICKER>.parquet`` for each universe.
        If the same ticker appears in multiple universes, the last occurrence
        wins (they should be identical files for overlapping tickers).

        Parameters
        ----------
        universes : list[str]
            Universe names matching cache subdirectory names,
            e.g. ``["sp500", "sector_etfs", "gold_etfs"]``.
        start_date, end_date : str, optional
            Filter rows to this date range.  Defaults to ``self.start_date``
            / ``self.end_date``.

        Returns
        -------
        dict mapping ticker symbol → OHLCV DataFrame (DatetimeIndex).
        """
        start_date = start_date or self.start_date
        end_date = end_date or self.end_date
        data: Dict[str, pd.DataFrame] = {}

        for universe in universes:
            universe_dir = self._cache_dir / universe
            if not universe_dir.exists():
                logger.warning("Universe cache directory not found: %s", universe_dir)
                continue

            parquet_files = list(universe_dir.glob("*.parquet"))
            if not parquet_files:
                logger.warning("No parquet files in universe cache: %s", universe_dir)
                continue

            for parquet_path in parquet_files:
                # File name is the ticker symbol (e.g. AAPL.parquet → ticker=AAPL)
                ticker = parquet_path.stem

                try:
                    df = pd.read_parquet(parquet_path)
                    if df is None or df.empty:
                        continue
                    # Ensure DatetimeIndex for slicing
                    if not isinstance(df.index, pd.DatetimeIndex):
                        df.index = pd.to_datetime(df.index)
                    # Filter to the regime window date range
                    mask = (df.index >= start_date) & (df.index <= end_date)
                    df = df.loc[mask]
                    if df.empty:
                        continue
                    data[ticker] = df
                except Exception as exc:
                    logger.warning("Failed to load %s: %s", parquet_path, exc)

        logger.info(
            "Loaded %d tickers for universes=%s  range=[%s, %s]",
            len(data), universes, start_date, end_date,
        )
        return data

    # ── Regime window extraction ──────────────────────────────────────────────

    def _get_regime_windows(self) -> List[Dict[str, Any]]:
        """Read regime_history and return contiguous same-state blocks.

        Each window dict has::

            {start, end, regime, universes, sizing_multiplier, enabled_strategies}

        Falls back to a single BULL_RISK_ON window covering the full date
        range if regime_history is unavailable or empty.
        """
        try:
            from db.atlas_db import get_regime_history

            all_history = get_regime_history()  # returns DESC order
        except Exception as exc:
            logger.warning(
                "Could not read regime_history (%s) — using BULL_RISK_ON default", exc
            )
            all_history = []

        # Reverse to ASC and filter to our date range
        history = [
            r for r in reversed(all_history)
            if self.start_date <= r["date"] <= self.end_date
        ]

        if not history:
            logger.info(
                "No regime history for [%s, %s] — defaulting to BULL_RISK_ON",
                self.start_date, self.end_date,
            )
            default_cfg = REGIME_CONFIGS[RegimeState.BULL_RISK_ON]
            return [
                {
                    "start": self.start_date,
                    "end": self.end_date,
                    "regime": RegimeState.BULL_RISK_ON.value,
                    "universes": list(default_cfg["active_universes"]),
                    "sizing_multiplier": float(default_cfg["sizing_multiplier"]),
                    "enabled_strategies": list(default_cfg["strategy_types"]),
                }
            ]

        # Group consecutive rows into contiguous same-state windows
        windows: List[Dict[str, Any]] = []
        curr = dict(history[0])
        curr_start = curr["date"]

        for i in range(1, len(history)):
            row = history[i]
            if row["regime_state"] != curr["regime_state"]:
                # Close current window at the previous row's date
                windows.append(self._make_window(curr, curr_start, history[i - 1]["date"]))
                curr = dict(row)
                curr_start = row["date"]

        # Close the final window at the last history row's date
        windows.append(self._make_window(curr, curr_start, history[-1]["date"]))

        logger.info(
            "Found %d regime windows in [%s, %s]",
            len(windows), self.start_date, self.end_date,
        )
        return windows

    @staticmethod
    def _make_window(row: Dict[str, Any], start: str, end: str) -> Dict[str, Any]:
        """Construct a regime window dict from a regime_history row."""
        return {
            "start": start,
            "end": end,
            "regime": row["regime_state"],
            "universes": list(row.get("active_universes") or ["sp500"]),
            "sizing_multiplier": float(row.get("sizing_multiplier") or 1.0),
            "enabled_strategies": list(row.get("enabled_strategies") or ["all"]),
        }

    # ── Config helpers ────────────────────────────────────────────────────────

    def _apply_sizing(
        self,
        config: Dict[str, Any],
        multiplier: float,
    ) -> Dict[str, Any]:
        """Return a deep copy of config with risk parameters scaled.

        Scales:
            ``risk.max_risk_per_trade_pct`` — proportionally
            ``risk.max_open_positions``     — proportionally, floored at 1

        Parameters
        ----------
        config : dict
            Base trading config (not mutated).
        multiplier : float
            Regime sizing multiplier (e.g. 0.5 for BEAR_RISK_OFF).
        """
        cfg = copy.deepcopy(config)
        risk = cfg.get("risk", {})

        if "max_risk_per_trade_pct" in risk:
            risk["max_risk_per_trade_pct"] = risk["max_risk_per_trade_pct"] * multiplier

        if "max_open_positions" in risk:
            risk["max_open_positions"] = max(1, int(risk["max_open_positions"] * multiplier))

        cfg["risk"] = risk
        return cfg

    # ── Strategy factory ──────────────────────────────────────────────────────

    def _build_strategies(self, enabled_types: List[str]) -> List:
        """Instantiate strategies from config filtered to allowed types.

        Parameters
        ----------
        enabled_types : list[str]
            Regime-permitted strategy types.  ``["all"]`` means all
            config-enabled strategies are instantiated; otherwise only the
            named types are included (e.g. ``["mean_reversion", "trend_following"]``).

        Returns
        -------
        list of BaseStrategy instances.
        """
        # Lazy imports to avoid circular import at module load
        from strategies.momentum_breakout import MomentumBreakout
        from strategies.mean_reversion import MeanReversion
        from strategies.trend_following import TrendFollowing

        use_all = "all" in enabled_types
        sc = self.config.get("strategies", {})

        def _include(name: str) -> bool:
            return use_all or name in enabled_types

        strats = []

        if _include("momentum_breakout") and sc.get("momentum_breakout", {}).get("enabled", False):
            strats.append(MomentumBreakout(self.config))

        if _include("mean_reversion") and sc.get("mean_reversion", {}).get("enabled", False):
            strats.append(MeanReversion(self.config))

        if _include("trend_following") and sc.get("trend_following", {}).get("enabled", False):
            strats.append(TrendFollowing(self.config))

        # Optional strategies — only load if the module exists in config
        _optional = [
            ("sector_rotation", "strategies.sector_rotation", "SectorRotation"),
            ("short_term_mr", "strategies.short_term_mr", "ShortTermMR"),
            ("opening_gap", "strategies.opening_gap", "OpeningGap"),
            ("connors_rsi2", "strategies.connors_rsi2", "ConnorsRSI2"),
            ("mtf_momentum", "strategies.mtf_momentum", "MTFMomentum"),
        ]
        for strat_key, module_path, class_name in _optional:
            if not _include(strat_key):
                continue
            if not sc.get(strat_key, {}).get("enabled", False):
                continue
            try:
                import importlib
                mod = importlib.import_module(module_path)
                cls = getattr(mod, class_name)
                strats.append(cls(self.config))
            except (ImportError, AttributeError) as exc:
                logger.debug("Optional strategy %s not available: %s", strat_key, exc)

        logger.debug(
            "Built %d strategies for enabled_types=%s", len(strats), enabled_types
        )
        return strats

    # ── Aggregation ───────────────────────────────────────────────────────────

    @staticmethod
    def _aggregate_results(
        sub_results: List[BacktestResult],
        config: Dict[str, Any],
    ) -> BacktestResult:
        """Combine multiple sequential BacktestResult objects into one.

        Concatenates all trades, merges equity curves (last value wins on
        date overlap), and recalculates aggregate metrics over the combined
        series.

        Parameters
        ----------
        sub_results : list[BacktestResult]
            One result per regime window (in chronological order).
        config : dict
            Trading config (used to extract risk_free_rate for metrics).
        """
        from backtest.metrics import calc_all_metrics

        all_trades: List[Dict[str, Any]] = []
        equity_curves: List[pd.Series] = []

        for r in sub_results:
            all_trades.extend(r.trades)
            if r.equity_curve is not None and not r.equity_curve.empty:
                equity_curves.append(r.equity_curve)

        if equity_curves:
            combined = pd.concat(equity_curves).sort_index()
            # Remove duplicate dates — keep last (most recent sub-backtest)
            combined = combined[~combined.index.duplicated(keep="last")]
        else:
            combined = pd.Series(dtype=float)

        rf = config.get("backtest", {}).get("risk_free_rate", 0.04)
        try:
            metrics = calc_all_metrics(
                equity_curve=combined,
                trades=all_trades,
                positions_log=all_trades,
                rf=rf,
            )
        except Exception as exc:
            logger.warning("Aggregate metric calculation failed: %s", exc)
            metrics = {}

        return BacktestResult(
            trades=all_trades,
            equity_curve=combined,
            metrics=metrics,
            benchmark_metrics={},
            walk_forward_windows=[],
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self) -> RegimeBacktestResult:
        """Run a regime-aware backtest over the full date range.

        Algorithm (approach a — sequential sub-backtests):

        1. Read ``regime_history`` and identify contiguous same-state windows.
        2. For each window:
           a. Load OHLCV data only for that regime's active universes.
           b. Scale config risk parameters by the regime's sizing_multiplier.
           c. Instantiate strategies permitted by the regime.
           d. Run ``BacktestEngine.run_walkforward()`` on the filtered data.
        3. Aggregate all sub-results into one :class:`RegimeBacktestResult`.

        Notes
        -----
        - Positions do **not** carry across regime boundaries.  This is a
          known simplification of approach (a).
        - Windows shorter than ``train_window + test_window`` produce no
          trades (BacktestEngine returns an empty BacktestResult for them).
        - If a window has no data or no strategies, it is skipped.

        Returns
        -------
        RegimeBacktestResult
        """
        regime_windows = self._get_regime_windows()

        sub_results: List[BacktestResult] = []
        regime_window_records: List[Dict[str, Any]] = []
        regime_distribution: Dict[str, int] = {}

        for window in regime_windows:
            regime_state = window["regime"]
            logger.info(
                "Regime window: %s  [%s → %s]  sizing=%.2f×  universes=%s",
                regime_state, window["start"], window["end"],
                window["sizing_multiplier"], window["universes"],
            )

            # Load OHLCV data for the active universes in this window
            data = self._load_universe_data(
                window["universes"],
                start_date=window["start"],
                end_date=window["end"],
            )

            if not data:
                logger.warning(
                    "No data for regime window %s [%s, %s] — skipping",
                    regime_state, window["start"], window["end"],
                )
                regime_window_records.append(
                    {**window, "trades": 0, "skipped": True, "skip_reason": "no_data"}
                )
                continue

            # Apply regime sizing to config
            regime_config = self._apply_sizing(self.config, window["sizing_multiplier"])

            # Build strategies filtered to this regime's allowed types
            strategies = self._build_strategies(window["enabled_strategies"])

            if not strategies:
                logger.warning(
                    "No strategies for regime %s (enabled_types=%s) — skipping",
                    regime_state, window["enabled_strategies"],
                )
                regime_window_records.append(
                    {**window, "trades": 0, "skipped": True, "skip_reason": "no_strategies"}
                )
                continue

            # Run sub-backtest for this regime window
            try:
                engine = BacktestEngine(regime_config, market_id=self.market_id)
                sub_result = engine.run_walkforward(data, strategies)
            except Exception as exc:
                logger.error(
                    "Sub-backtest failed for regime %s [%s, %s]: %s",
                    regime_state, window["start"], window["end"], exc,
                )
                regime_window_records.append(
                    {**window, "trades": 0, "error": str(exc)}
                )
                continue

            sub_results.append(sub_result)
            regime_distribution[regime_state] = (
                regime_distribution.get(regime_state, 0) + 1
            )
            regime_window_records.append(
                {
                    "start": window["start"],
                    "end": window["end"],
                    "regime": regime_state,
                    "universes": window["universes"],
                    "sizing_multiplier": window["sizing_multiplier"],
                    "trades": len(sub_result.trades),
                }
            )

        # Aggregate all successful sub-results
        aggregated = self._aggregate_results(sub_results, self.config)

        logger.info(
            "Regime-aware backtest complete — windows=%d  total_trades=%d  "
            "regime_distribution=%s",
            len(regime_windows), len(aggregated.trades), regime_distribution,
        )

        return RegimeBacktestResult(
            result=aggregated,
            regime_windows=regime_window_records,
            regime_distribution=regime_distribution,
            comparison_vs_sp500={},
        )

    def compare_with_sp500_only(self) -> Dict[str, Any]:
        """Run both regime-aware and SP500-only backtests and return comparison.

        The SP500-only baseline uses all config-enabled strategies on SP500
        data only (no regime filtering, no sizing adjustment).

        Returns
        -------
        dict with keys:
            ``regime_aware``  — :class:`RegimeBacktestResult`
            ``sp500_only``    — :class:`BacktestResult`
            ``delta``         — ``{metric: regime_value - sp500_value}``
        """
        logger.info("compare_with_sp500_only: running regime-aware backtest...")
        regime_result = self.run()

        logger.info("compare_with_sp500_only: running SP500-only baseline...")
        sp500_data = self._load_universe_data(["sp500"])
        strategies = self._build_strategies(["all"])

        engine = BacktestEngine(self.config, market_id=self.market_id)
        sp500_result = engine.run_walkforward(sp500_data, strategies)

        # Compute signed delta for key metrics
        ra_metrics = regime_result.result.metrics
        sp_metrics = sp500_result.metrics if sp500_result else {}

        delta: Dict[str, float] = {}
        _metric_keys = [
            "sharpe", "cagr", "max_drawdown", "win_rate",
            "profit_factor", "sortino", "calmar",
        ]
        for key in _metric_keys:
            ra_val = float(ra_metrics.get(key) or 0.0)
            sp_val = float(sp_metrics.get(key) or 0.0)
            delta[key] = round(ra_val - sp_val, 6)

        # Attach to the regime result for convenience
        regime_result.comparison_vs_sp500 = {
            "delta": delta,
            "sp500_metrics": sp_metrics,
        }

        return {
            "regime_aware": regime_result,
            "sp500_only": sp500_result,
            "delta": delta,
        }
