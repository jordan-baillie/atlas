"""Atlas Cross-OOS validation battery.

A reusable, strategy-agnostic toolkit that raises out-of-sample testing from a single
chronological IS/OOS split + walk-forward to a four-axis, selection-aware battery:

  - cpcv         : Combinatorial Purged Cross-Validation (purge + embargo) — cross-TIME
  - splitters    : leave-one-asset-out, leave-one-group-out, regime stratification
                   — cross-TICKER, cross-SECTOR, cross-REGIME
  - overfitting  : Probabilistic / Deflated Sharpe + PBO (CSCV) — multiple-testing control
  - metrics      : shared performance metrics (Sharpe, PF, max-DD, skew, kurtosis)
  - gates        : declarative hard-gate evaluation (missing measurement == FAIL)

The engine is pure functions over a per-period return series + group/regime labels, so it
bolts onto the existing Atlas BacktestEngine output without replacing the backtester.
Atlas-specific wiring (BacktestResult -> returns/attribution/regime, PBO config matrix,
equities-tuned gate table) lives in research.cross_oos.adapter.

Ported from the Midas #102 cross-OOS harness (2026-06).
"""
from __future__ import annotations

from . import cpcv, gates, metrics, overfitting, splitters

__all__ = ["cpcv", "gates", "metrics", "overfitting", "splitters"]
