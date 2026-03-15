"""Day simulation pipeline — orchestrates _simulate_day steps.

Provides DayContext (mutable state for one day) and helper functions
that the engine delegates to for entry gate checks and signal enrichment.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

from backtest.filters import (
    check_fred_macro,
    check_macro_regime,
    check_turn_of_month,
    check_vix_gate,
)
from backtest.enrichment import (
    apply_breadth_confidence,
    apply_macro_confidence,
    apply_rs_confidence,
    apply_tom_confidence,
    inject_breadth_features,
    inject_rs_features,
)

logger = logging.getLogger(__name__)


@dataclass
class DayContext:
    """Mutable state carried through the simulation pipeline for one day.

    Populated in stages:
      1. Created with core market data by the engine.
      2. Gate results filled by run_entry_gates().
      3. Signals attached and enriched by enrich_signals().
    """

    # ── Core temporal state ──────────────────────────────────────────────────
    today: pd.Timestamp
    yesterday: pd.Timestamp
    day_idx: int
    equity: float
    open_positions: List[Dict[str, Any]]
    closed_trades: List[Dict[str, Any]]

    # ── Market data ──────────────────────────────────────────────────────────
    data: Dict[str, pd.DataFrame]
    vix_series: Optional[pd.Series] = None
    breadth_series: Optional[pd.DataFrame] = None
    rs_data: Optional[Dict] = None
    macro_signals: Optional[pd.DataFrame] = None
    fred_yield_curve: Optional[pd.Series] = None
    fred_claims: Optional[pd.Series] = None
    equity_history: Optional[List] = None

    # ── Regime state (populated by _compute_regime before gates) ─────────────
    regime: str = "neutral"
    regime_scale: float = 1.0

    # ── Gate results (populated by run_entry_gates) ───────────────────────────
    vix_blocked: bool = False
    fred_blocked: bool = False
    tom_blocked: bool = False
    macro_blocked: bool = False
    macro_scale: float = 1.0
    macro_boost: float = 0.0
    current_vix: float = 0.0
    tom_in_window: bool = False

    # ── Signals (populated and enriched per-strategy) ────────────────────────
    all_signals: List = field(default_factory=list)

    @property
    def any_gate_blocked(self) -> bool:
        """True if any entry gate is blocking new entries."""
        return self.vix_blocked or self.fred_blocked or self.tom_blocked or self.macro_blocked


def run_entry_gates(ctx: DayContext, config: Dict[str, Any]) -> None:
    """Run all entry gate filters and populate ctx gate fields.

    Calls check_vix_gate, check_fred_macro, check_turn_of_month, and
    check_macro_regime.  Results are written directly into ctx.

    This is a thin orchestration helper — the actual logic lives in
    backtest.filters.  Callers can inspect ctx.any_gate_blocked to
    decide whether to proceed with signal generation.

    Args:
        ctx:    DayContext populated with today/yesterday and market data.
        config: Full strategy config dict (the same dict passed to BacktestEngine).
    """
    # VIX gate
    vix_blocked, _, vix_meta = check_vix_gate(
        ctx.vix_series,
        ctx.yesterday,
        config.get("vix_filter", {}).get("max_entry", 30.0),
    )
    ctx.vix_blocked = vix_blocked
    ctx.current_vix = vix_meta.get("current_vix", 0.0)

    # FRED macro gate
    fred_blocked, _, _ = check_fred_macro(
        ctx.fred_yield_curve,
        ctx.fred_claims,
        ctx.yesterday,
        config.get("fred_filter", {}),
    )
    ctx.fred_blocked = fred_blocked

    # Turn-of-Month gate — needs trading_dates from data (approximation: use ctx.data index union)
    # Callers that have trading_dates should call check_turn_of_month directly;
    # this helper builds a best-effort index from ctx.data for convenience.
    _all_dates: pd.DatetimeIndex = pd.DatetimeIndex(
        sorted({d for df in ctx.data.values() for d in df.index})
    )
    tom_cfg = _build_tom_cfg(config)
    tom_blocked, _, tom_meta = check_turn_of_month(ctx.today, _all_dates, tom_cfg)
    ctx.tom_blocked = tom_blocked
    ctx.tom_in_window = tom_meta.get("tom_in_window", False)

    # Macro regime gate
    macro_blocked, _, macro_meta = check_macro_regime(
        ctx.macro_signals,
        ctx.yesterday,
        ctx.today,
        config.get("macro_regime", {}),
    )
    ctx.macro_blocked = macro_blocked
    ctx.macro_scale = macro_meta.get("macro_scale", 1.0)
    ctx.macro_boost = macro_meta.get("macro_boost", 0.0)


def enrich_signals(
    signals: list,
    ctx: DayContext,
    config: Dict[str, Any],
) -> None:
    """Enrich a batch of signals with macro/breadth/RS features and confidence adjustments.

    Calls all enrichment functions in the same order as _simulate_day:
      1. apply_macro_confidence  (inject macro features + optional boost)
      2. apply_tom_confidence    (TOM boost + tag)
      3. inject_breadth_features (breadth feature injection)
      4. apply_breadth_confidence (breadth confidence modifier)
      5. inject_rs_features       (RS feature injection)
      6. apply_rs_confidence      (RS confidence modifier)

    All mutations are in-place.  ctx.all_signals is extended with the
    enriched signals.

    Args:
        signals: List of Signal objects from strategy.generate_signals().
        ctx:     DayContext with gate results and market data populated.
        config:  Full strategy config dict.
    """
    strategies_cfg = config.get("strategies", {})
    tom_cfg = _build_tom_cfg(config)

    apply_macro_confidence(
        signals,
        ctx.macro_signals,
        ctx.yesterday,
        ctx.today,
        config.get("macro_regime", {}),
        ctx.macro_boost,
    )
    apply_tom_confidence(signals, tom_cfg, ctx.tom_in_window)
    inject_breadth_features(signals, ctx.breadth_series, ctx.today, ctx.regime, ctx.regime_scale)
    apply_breadth_confidence(signals, strategies_cfg)
    inject_rs_features(signals, ctx.rs_data, ctx.yesterday)
    apply_rs_confidence(signals, strategies_cfg)

    ctx.all_signals.extend(signals)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_tom_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    """Build a normalised TOM config dict from the raw engine config.

    Mirrors the TOM config parsing in BacktestEngine.__init__ so that
    run_entry_gates / enrich_signals work without an engine instance.
    """
    _tom_cfg = config.get("turn_of_month", False)
    if isinstance(_tom_cfg, dict):
        return {
            "mode": _tom_cfg.get("mode", False),
            "days_before_month_end": _tom_cfg.get("days_before_month_end", 5),
            "days_after_month_start": _tom_cfg.get("days_after_month_start", 3),
            "confidence_boost": _tom_cfg.get("confidence_boost", 0.05),
        }
    elif _tom_cfg in (True, "boost"):
        return {
            "mode": _tom_cfg,
            "days_before_month_end": 5,
            "days_after_month_start": 3,
            "confidence_boost": 0.05,
        }
    else:
        return {
            "mode": False,
            "days_before_month_end": 5,
            "days_after_month_start": 3,
            "confidence_boost": 0.05,
        }
