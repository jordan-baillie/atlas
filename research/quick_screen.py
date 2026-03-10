#!/usr/bin/env python3
"""Quick screen filter — kill dead-end strategies in <10 seconds.

Two-stage screen:
  Stage 1 (signal check, <1s): Run generate_signals() on cached data.
    Kill if the strategy throws an error or generates 0 signals across the
    full ticker universe (with position limits removed).
  Stage 2 (quick backtest, ~10s): Top 50 tickers by volume, single-pass,
    fixed sizing. Kill if clearly terrible by Sharpe / profit_factor / trades.

Usage:
    from research.quick_screen import screen_strategy, load_market_data, ScreenResult
    from utils.config import get_active_config

    config = get_active_config('sp500')
    result = screen_strategy('mean_reversion', config, market='sp500')
    print(result)
"""

import copy
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Atlas root on sys.path
# ---------------------------------------------------------------------------
ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

logger = logging.getLogger("quick_screen")


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ScreenResult:
    """Result of a quick screen.

    Attributes:
        experiment_id: Optional experiment ID for tracking.
        strategy_name: Name of the strategy that was screened.
        stage:         Which stage produced the final verdict:
                       "signal_check" | "quick_backtest"
        passed:        True if strategy should proceed to full backtest.
        signal_count:  Number of signals found in Stage 1.
        quick_sharpe:  Sharpe ratio from Stage 2 quick backtest.
        quick_trades:  Total trade count from Stage 2 quick backtest.
        quick_pf:      Profit factor from Stage 2 quick backtest.
        runtime_s:     Wall-clock seconds elapsed.
        reason:        Human-readable explanation of outcome.
    """
    experiment_id: str
    strategy_name: str
    stage: str          # "signal_check" or "quick_backtest"
    passed: bool
    signal_count: int = 0
    quick_sharpe: float = 0.0
    quick_trades: int = 0
    quick_pf: float = 0.0
    runtime_s: float = 0.0
    reason: str = ""

    def __str__(self) -> str:  # pragma: no cover
        status = "PASS" if self.passed else "FAIL"
        return (
            f"ScreenResult({status} @ {self.stage}) "
            f"strategy={self.strategy_name} "
            f"signals={self.signal_count} "
            f"sharpe={self.quick_sharpe:.3f} "
            f"trades={self.quick_trades} "
            f"pf={self.quick_pf:.3f} "
            f"runtime={self.runtime_s:.1f}s "
            f"reason={self.reason!r}"
        )


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def load_market_data(market: str = "sp500") -> Dict[str, pd.DataFrame]:
    """Load cached market data for screening.

    Delegates to the existing ``scripts.strategy_evaluator.load_market_data``
    which reads parquet files from ``data/cache/{market}/``.

    Args:
        market: Market identifier, e.g. 'sp500' or 'asx'.

    Returns:
        Dict mapping ticker -> DataFrame with OHLCV columns.
    """
    from scripts.strategy_evaluator import load_market_data as _load
    return _load(market)


def _top_n_tickers(data: Dict[str, pd.DataFrame], n: int = 50) -> Dict[str, pd.DataFrame]:
    """Return the top N tickers sorted by mean daily volume.

    Args:
        data: Full ticker -> DataFrame mapping.
        n:    How many tickers to keep (default 50).

    Returns:
        Filtered dict with at most *n* tickers.
    """
    if len(data) <= n:
        return data

    volumes: Dict[str, float] = {}
    for ticker, df in data.items():
        if "volume" in df.columns:
            vol_series = df["volume"].replace(0, np.nan).dropna()
            volumes[ticker] = float(vol_series.mean()) if len(vol_series) > 0 else 0.0
        else:
            volumes[ticker] = 0.0

    top_tickers = sorted(volumes, key=lambda t: volumes[t], reverse=True)[:n]
    return {t: data[t] for t in top_tickers}


# ---------------------------------------------------------------------------
# Stage 1 — Signal check
# ---------------------------------------------------------------------------

# Minimum signals that must be generated on a single pass across the universe
# to consider the strategy "alive".  We remove position caps so this reflects
# market conditions, not the risk-limit ceiling.
_SIGNAL_CHECK_MIN = 1


def signal_check(
    strategy_name: str,
    config: dict,
    data: Dict[str, pd.DataFrame],
) -> ScreenResult:
    """Stage 1: Signal-only pre-screen (<1 s).

    Instantiates the strategy with position limits removed, calls
    ``generate_signals()`` once against the full universe, and counts the
    returned signals.

    Kill criteria:
    - Strategy class cannot be found/loaded → FAIL
    - ``generate_signals()`` raises an exception → FAIL
    - signal_count < 1 (no setups found at all) → FAIL

    Pass criteria:
    - signal_count >= 1 and no errors

    Note: 10-signal threshold from the spec is appropriate for cumulative
    counts across walk-forward windows; here we run a single snapshot pass
    so the bar is set to 1 (any live setup = alive).

    Args:
        strategy_name: Strategy name to look up in registry or sandbox.
        config:        Full atlas config dict.
        data:          Preloaded ticker -> DataFrame mapping.

    Returns:
        ScreenResult with stage="signal_check".
    """
    t0 = time.time()

    try:
        from scripts.strategy_evaluator import get_strategy_class
        cls = get_strategy_class(strategy_name)
    except (ValueError, ImportError, ModuleNotFoundError) as exc:
        runtime = time.time() - t0
        reason = f"Strategy class not found: {exc}"
        logger.warning("[%s] signal_check: %s", strategy_name, reason)
        return ScreenResult(
            experiment_id="",
            strategy_name=strategy_name,
            stage="signal_check",
            passed=False,
            signal_count=0,
            runtime_s=round(runtime, 2),
            reason=reason,
        )

    try:
        # Remove position caps so we see the full signal output, not just 5
        screen_cfg = copy.deepcopy(config)
        risk = screen_cfg.setdefault("risk", {})
        risk["max_open_positions"] = 9999
        risk["max_sector_concentration"] = 9999

        strategy = cls(screen_cfg)
        signals = strategy.generate_signals(data, equity=100_000.0, existing_positions=[])
        signal_count = len(signals)

    except Exception as exc:
        runtime = time.time() - t0
        reason = f"generate_signals() raised an exception: {exc}"
        logger.warning("[%s] signal_check: %s", strategy_name, reason)
        return ScreenResult(
            experiment_id="",
            strategy_name=strategy_name,
            stage="signal_check",
            passed=False,
            signal_count=0,
            runtime_s=round(runtime, 2),
            reason=reason,
        )

    runtime = time.time() - t0

    if signal_count < _SIGNAL_CHECK_MIN:
        reason = (
            f"No signals generated on current universe "
            f"({signal_count} signals found, need >= {_SIGNAL_CHECK_MIN})"
        )
        logger.info("[%s] signal_check FAIL: %s", strategy_name, reason)
        return ScreenResult(
            experiment_id="",
            strategy_name=strategy_name,
            stage="signal_check",
            passed=False,
            signal_count=signal_count,
            runtime_s=round(runtime, 2),
            reason=reason,
        )

    reason = f"Signal check passed: {signal_count} signals generated in {runtime:.2f}s"
    logger.info("[%s] signal_check PASS: %s", strategy_name, reason)
    return ScreenResult(
        experiment_id="",
        strategy_name=strategy_name,
        stage="signal_check",
        passed=True,
        signal_count=signal_count,
        runtime_s=round(runtime, 2),
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Stage 2 — Quick backtest
# ---------------------------------------------------------------------------

# Kill thresholds — clearly bad strategies
_QB_MIN_TRADES = 5       # fewer trades → "dead"
_QB_MIN_SHARPE = -1.0    # below this → clearly terrible
_QB_MIN_PF = 0.5         # below this → clearly terrible

# Pass thresholds — need to clear all three to get a green light
_QB_PASS_SHARPE = -0.5
_QB_PASS_TRADES = 10
_QB_PASS_PF = 0.7


def quick_backtest(
    strategy_name: str,
    config: dict,
    data: Dict[str, pd.DataFrame],
) -> ScreenResult:
    """Stage 2: Quick backtest (~10 s).

    Uses the top 50 tickers by average volume, runs the full walk-forward
    backtest (via ``scripts.strategy_evaluator.run_backtest``) on the subset.
    This is a sanity check, NOT a definitive evaluation.

    Kill criteria (strategy is clearly dead):
    - total_trades < 5
    - Sharpe < -1.0
    - profit_factor < 0.5

    Pass criteria (all three must hold):
    - Sharpe > -0.5
    - total_trades >= 10
    - profit_factor > 0.7

    Args:
        strategy_name: Strategy name.
        config:        Full atlas config dict.
        data:          Preloaded ticker -> DataFrame mapping (full universe).

    Returns:
        ScreenResult with stage="quick_backtest".
    """
    t0 = time.time()

    try:
        from scripts.strategy_evaluator import make_config_with_strategy, run_backtest

        # Slice the universe to the 50 most-liquid tickers
        data_subset = _top_n_tickers(data, n=50)

        # Solo config: only the target strategy enabled
        screen_cfg = make_config_with_strategy(config, strategy_name, solo=True)
        # Override equity and position count for a quick, standardised run
        screen_cfg.setdefault("trading", {})["initial_equity"] = 100_000
        screen_cfg.setdefault("risk", {})["max_open_positions"] = 20

        metrics = run_backtest(screen_cfg, data_subset)

    except Exception as exc:
        runtime = time.time() - t0
        reason = f"quick_backtest raised an exception: {exc}"
        logger.warning("[%s] quick_backtest: %s", strategy_name, reason)
        return ScreenResult(
            experiment_id="",
            strategy_name=strategy_name,
            stage="quick_backtest",
            passed=False,
            runtime_s=round(runtime, 2),
            reason=reason,
        )

    sharpe = float(metrics.get("sharpe", 0.0) or 0.0)
    total_trades = int(metrics.get("total_trades", 0) or 0)
    profit_factor = float(metrics.get("profit_factor", 0.0) or 0.0)
    runtime = time.time() - t0

    # --- Kill criteria (checked first; order matters for clarity of reason) ---
    if total_trades < _QB_MIN_TRADES:
        reason = f"Too few trades: {total_trades} < {_QB_MIN_TRADES} (dead strategy)"
        logger.info("[%s] quick_backtest FAIL: %s", strategy_name, reason)
        return ScreenResult(
            experiment_id="",
            strategy_name=strategy_name,
            stage="quick_backtest",
            passed=False,
            quick_sharpe=round(sharpe, 4),
            quick_trades=total_trades,
            quick_pf=round(profit_factor, 4),
            runtime_s=round(runtime, 2),
            reason=reason,
        )

    if sharpe < _QB_MIN_SHARPE:
        reason = f"Sharpe ratio too low: {sharpe:.4f} < {_QB_MIN_SHARPE}"
        logger.info("[%s] quick_backtest FAIL: %s", strategy_name, reason)
        return ScreenResult(
            experiment_id="",
            strategy_name=strategy_name,
            stage="quick_backtest",
            passed=False,
            quick_sharpe=round(sharpe, 4),
            quick_trades=total_trades,
            quick_pf=round(profit_factor, 4),
            runtime_s=round(runtime, 2),
            reason=reason,
        )

    if profit_factor < _QB_MIN_PF:
        reason = f"Profit factor too low: {profit_factor:.4f} < {_QB_MIN_PF}"
        logger.info("[%s] quick_backtest FAIL: %s", strategy_name, reason)
        return ScreenResult(
            experiment_id="",
            strategy_name=strategy_name,
            stage="quick_backtest",
            passed=False,
            quick_sharpe=round(sharpe, 4),
            quick_trades=total_trades,
            quick_pf=round(profit_factor, 4),
            runtime_s=round(runtime, 2),
            reason=reason,
        )

    # --- Pass gate (all three conditions must hold) ---
    passed = (
        sharpe > _QB_PASS_SHARPE
        and total_trades >= _QB_PASS_TRADES
        and profit_factor > _QB_PASS_PF
    )

    if passed:
        reason = (
            f"Quick backtest passed: sharpe={sharpe:.4f}, "
            f"trades={total_trades}, pf={profit_factor:.4f}"
        )
    else:
        reason = (
            f"Quick backtest borderline (did not meet all pass thresholds): "
            f"sharpe={sharpe:.4f} (need>{_QB_PASS_SHARPE}), "
            f"trades={total_trades} (need>={_QB_PASS_TRADES}), "
            f"pf={profit_factor:.4f} (need>{_QB_PASS_PF})"
        )

    logger.info("[%s] quick_backtest %s: %s", strategy_name, "PASS" if passed else "BORDERLINE", reason)
    return ScreenResult(
        experiment_id="",
        strategy_name=strategy_name,
        stage="quick_backtest",
        passed=passed,
        quick_sharpe=round(sharpe, 4),
        quick_trades=total_trades,
        quick_pf=round(profit_factor, 4),
        runtime_s=round(runtime, 2),
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def screen_strategy(
    strategy_name: str,
    config: dict,
    data: Optional[Dict[str, pd.DataFrame]] = None,
    market: str = "sp500",
) -> ScreenResult:
    """Run the two-stage quick screen on a strategy.

    Stage 1 (signal_check) runs in <1 s and rejects strategies that cannot
    generate any signals on the current data.  If Stage 1 passes, Stage 2
    (quick_backtest) runs a real but abbreviated walk-forward on the top 50
    most-liquid tickers (~10 s) and applies quality thresholds.

    Args:
        strategy_name: Strategy name — must exist in the main registry
                       (``scripts/strategy_evaluator.py``) or as a sandbox
                       strategy under ``research/strategies/``.
        config:        Full atlas config dict (from ``get_active_config``).
        data:          Pre-loaded ticker -> DataFrame mapping.  If *None*, the
                       function loads the market cache automatically.
        market:        Market identifier used when *data* is None.

    Returns:
        ScreenResult indicating whether the strategy passed.  ``passed=True``
        means the strategy is not obviously dead and should proceed to a full
        backtest.  ``passed=False`` means it should be skipped / killed.
    """
    t0 = time.time()

    # --- Load data if not provided ---
    if data is None:
        logger.info("[%s] Loading market data for '%s' ...", strategy_name, market)
        try:
            data = load_market_data(market)
        except Exception as exc:
            runtime = time.time() - t0
            reason = f"Failed to load market data for '{market}': {exc}"
            logger.error("[%s] screen_strategy: %s", strategy_name, reason)
            return ScreenResult(
                experiment_id="",
                strategy_name=strategy_name,
                stage="signal_check",
                passed=False,
                runtime_s=round(runtime, 2),
                reason=reason,
            )

    if not data:
        runtime = time.time() - t0
        reason = f"Empty data for market '{market}' — nothing to screen against"
        logger.error("[%s] screen_strategy: %s", strategy_name, reason)
        return ScreenResult(
            experiment_id="",
            strategy_name=strategy_name,
            stage="signal_check",
            passed=False,
            runtime_s=round(runtime, 2),
            reason=reason,
        )

    logger.info(
        "[%s] Starting two-stage screen on %d tickers ...",
        strategy_name, len(data),
    )

    # --- Stage 1: Signal check ---
    sig_result = signal_check(strategy_name, config, data)

    if not sig_result.passed:
        # Preserve wall-clock total runtime
        sig_result.runtime_s = round(time.time() - t0, 2)
        return sig_result

    # --- Stage 2: Quick backtest ---
    qb_result = quick_backtest(strategy_name, config, data)

    # Carry forward the signal count from Stage 1 so callers always have it
    qb_result.signal_count = sig_result.signal_count
    # Overwrite runtime with total elapsed (load + stage1 + stage2)
    qb_result.runtime_s = round(time.time() - t0, 2)

    return qb_result


# ---------------------------------------------------------------------------
# CLI convenience
# ---------------------------------------------------------------------------

def _cli_main() -> int:  # pragma: no cover
    """Minimal CLI: ``python3 research/quick_screen.py STRATEGY [MARKET]``."""
    import argparse
    from utils.config import get_active_config

    parser = argparse.ArgumentParser(
        description="Quick-screen a strategy before running a full backtest."
    )
    parser.add_argument("strategy", help="Strategy name (e.g. mean_reversion)")
    parser.add_argument("market", nargs="?", default="sp500", help="Market ID (default: sp500)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    config = get_active_config(args.market)
    result = screen_strategy(args.strategy, config, market=args.market)

    print(result)
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(_cli_main())
