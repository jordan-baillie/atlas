#!/usr/bin/env python3
"""Atlas Backtest CLI — Parallelised per-stock backtest runner.

Runs a full walk-forward backtest for a given market, optionally
splitting the ticker universe across multiple parallel workers.

Parallelisation strategy:
    The ticker universe is split into N batches via round-robin assignment.
    Each batch is handed to a worker process that runs a complete
    BacktestEngine.run_walkforward() on that subset of tickers.
    Results (trades, equity curves) are merged into a single BacktestResult.

With --workers 1, the entire universe goes into one batch and is
processed serially — identical to running strategy_evaluator.py.

With --workers N>1, tickers are split across N processes. Note:
each batch computes breadth/RS from its own subset, so results will
differ from a single-engine run. Use --workers 1 for an exact
apples-to-apples comparison with strategy_evaluator.py.

Determinism guarantee: for a fixed --workers value and fixed data,
the round-robin split is deterministic (sorted tickers), so results
are reproducible across runs.

Usage:
    python3 scripts/backtest.py --market sp500
    python3 scripts/backtest.py --market sp500 --workers 4
    python3 scripts/backtest.py --market sp500 --workers 1   # serial debug
    python3 scripts/backtest.py --market asx --strategy mean_reversion
    python3 scripts/backtest.py --market sp500 --output results.json
"""

import copy
import json
import logging
import os
import signal
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Project root setup ──────────────────────────────────────────────────────
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
os.chdir(str(PROJECT))

import pandas as pd

from utils.logging_config import setup_logging

setup_logging("backtest", level=logging.WARNING)
logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

DEFAULT_WORKERS: int = min(8, os.cpu_count() or 4)

# ── Strategy helpers ─────────────────────────────────────────────────────────


def _get_strategy_registry() -> dict:
    """Return the strategy name → class registry.

    Imported lazily inside helper to avoid circular imports when the
    module is imported from worker processes.
    """
    from strategies.bb_squeeze import BBSqueeze
    from strategies.dividend_capture import DividendCapture
    from strategies.mean_reversion import MeanReversion
    from strategies.momentum_breakout import MomentumBreakout
    from strategies.mtf_momentum import MTFMomentum
    from strategies.opening_gap import OpeningGap
    from strategies.sector_rotation import SectorRotation
    from strategies.short_term_mr import ShortTermMR
    from strategies.trend_following import TrendFollowing

    return {
        "mean_reversion": MeanReversion,
        "trend_following": TrendFollowing,
        "bb_squeeze": BBSqueeze,
        "opening_gap": OpeningGap,
        "momentum_breakout": MomentumBreakout,
        "short_term_mr": ShortTermMR,
        "sector_rotation": SectorRotation,
        "mtf_momentum": MTFMomentum,
        "dividend_capture": DividendCapture,
    }


def _build_strategies(cfg: dict, strategy_names: List[str]) -> list:
    """Instantiate strategy objects from names + config."""
    registry = _get_strategy_registry()
    strategies = []
    for name in strategy_names:
        cls = registry.get(name)
        if cls is not None:
            strategies.append(cls(cfg))
        else:
            logger.warning("Unknown strategy '%s' — skipping", name)
    return strategies


# ── Top-level picklable worker ───────────────────────────────────────────────
# Must be defined at module level so ProcessPoolExecutor can pickle it.


def _run_batch_backtest(args: tuple) -> Optional[Dict[str, Any]]:
    """Run a complete walk-forward backtest on a subset of tickers.

    This is the top-level worker function dispatched to each process.
    It must be module-level (not a closure or local function) to be
    picklable for use with ProcessPoolExecutor.

    Args:
        args: Tuple of (config, strategy_names, batch_data, market_id).

    Returns:
        Dict with keys: trades, equity_curve, benchmark_metrics,
        walk_forward_windows. Returns None on unrecoverable error.
    """
    config, strategy_names, batch_data, market_id = args

    # Ensure project path is set up in forked/spawned process
    _project = Path(__file__).resolve().parent.parent
    if str(_project) not in sys.path:
        sys.path.insert(0, str(_project))
    os.chdir(str(_project))

    try:
        from backtest.engine import BacktestEngine  # noqa: F401 (lazy import)

        strategies = _build_strategies(config, strategy_names)
        if not strategies:
            logger.warning("Batch has no enabled strategies — returning empty result")
            return {
                "trades": [],
                "equity_curve": pd.Series(dtype=float),
                "benchmark_metrics": {},
                "walk_forward_windows": [],
            }

        engine = BacktestEngine(copy.deepcopy(config), market_id=market_id)
        result = engine.run_walkforward(batch_data, strategies)

        return {
            "trades": result.trades,
            "equity_curve": result.equity_curve,
            "metrics": result.metrics,
            "benchmark_metrics": result.benchmark_metrics,
            "walk_forward_windows": result.walk_forward_windows,
        }

    except Exception as exc:
        logger.error("Batch backtest failed: %s", exc, exc_info=True)
        return None


# ── Ticker splitting ─────────────────────────────────────────────────────────


def _split_tickers(
    data: Dict[str, pd.DataFrame],
    n_batches: int,
) -> List[Dict[str, pd.DataFrame]]:
    """Split a ticker universe dict into N balanced batches.

    Uses round-robin assignment over sorted ticker names so that the
    split is deterministic (reproducible) regardless of dict insertion
    order.

    Args:
        data: Full data dict mapping ticker → OHLCV DataFrame.
        n_batches: Desired number of batches.

    Returns:
        List of data dicts, one per batch. The list has
        min(n_batches, len(tickers)) entries — never more batches
        than tickers.
    """
    tickers = sorted(data.keys())
    n = min(n_batches, len(tickers))  # Can't have more batches than tickers
    if n <= 0:
        return []

    batches: List[Dict[str, pd.DataFrame]] = [{} for _ in range(n)]
    for i, ticker in enumerate(tickers):
        batches[i % n][ticker] = data[ticker]

    return [b for b in batches if b]  # drop any empty batches (shouldn't happen)


# ── Result merging ───────────────────────────────────────────────────────────


def _merge_batch_results(
    batch_results: List[Optional[Dict[str, Any]]],
    starting_equity: float,
    risk_free_rate: float = 0.04,
) -> Dict[str, Any]:
    """Merge results from parallel batch backtests into a single result.

    Merging strategy:
      - Trades: concatenate all batches and sort by exit date.
      - Equity curve: reconstruct by applying each trade's PnL in
        chronological exit-date order against a single equity pool.
      - Metrics: recompute from the merged trade list and reconstructed
        equity curve.
      - Benchmark: taken from the first successful batch (all batches
        use the same benchmark data).
      - Walk-forward windows: concatenated (informational only).

    Args:
        batch_results: One dict per batch (from _run_batch_backtest).
                       None entries represent failed workers.
        starting_equity: Initial portfolio equity (from config).
        risk_free_rate: Annual risk-free rate for Sharpe / Sortino.

    Returns:
        Merged result dict ready for display / JSON serialisation.
    """
    from backtest.metrics import calc_all_metrics

    valid_results = [r for r in batch_results if r is not None]

    if not valid_results:
        return {"error": "All batch backtests failed", "total_trades": 0}

    # ── Combine trades ──────────────────────────────────────────────────────
    all_trades: List[Dict[str, Any]] = []
    for r in valid_results:
        all_trades.extend(r.get("trades", []))

    # Sort by exit date for chronological equity reconstruction
    def _exit_key(t: dict) -> str:
        ed = t.get("exit_date", "")
        return str(ed) if ed else ""

    all_trades.sort(key=_exit_key)

    # ── Reconstruct equity curve ────────────────────────────────────────────
    # Apply each trade's net PnL to a single shared equity pool, recording
    # the equity after each exit.
    equity = starting_equity
    equity_records: Dict[pd.Timestamp, float] = {}

    for trade in all_trades:
        pnl = trade.get("pnl", 0) or 0
        equity += pnl
        exit_date = trade.get("exit_date")
        if exit_date is not None:
            ts = pd.Timestamp(exit_date)
            equity_records[ts] = equity

    if equity_records:
        equity_series = pd.Series(equity_records).sort_index()
        # Remove duplicate dates (keep last — same as engine does for daily records)
        equity_series = equity_series[~equity_series.index.duplicated(keep="last")]
    else:
        equity_series = pd.Series(dtype=float)

    # ── Recompute metrics ───────────────────────────────────────────────────
    metrics = calc_all_metrics(
        equity_curve=equity_series,
        trades=all_trades,
        positions_log=all_trades,
        rf=risk_free_rate,
    )

    # ── Collect ancillary data ──────────────────────────────────────────────
    benchmark_metrics = valid_results[0].get("benchmark_metrics", {})
    all_windows: List[Dict[str, Any]] = []
    for r in valid_results:
        all_windows.extend(r.get("walk_forward_windows", []))

    return {
        "trades": all_trades,
        "equity_curve": equity_series,
        "metrics": metrics,
        "benchmark_metrics": benchmark_metrics,
        "walk_forward_windows": all_windows,
    }


# ── Core importable API ──────────────────────────────────────────────────────


def run_backtest(
    cfg: dict,
    data: Dict[str, pd.DataFrame],
    market_id: str = "asx",
    strategy_names: Optional[List[str]] = None,
    n_workers: int = 1,
) -> Dict[str, Any]:
    """Run a (optionally parallelised) walk-forward backtest.

    This is the public importable API for ``scripts.backtest``.

    With ``n_workers=1`` all tickers are processed in a single
    BacktestEngine instance — identical to calling
    ``strategy_evaluator.run_backtest()``.

    With ``n_workers > 1`` the ticker universe is split into batches
    and each batch runs in a separate process; results are merged
    afterwards.

    Args:
        cfg: Full config dict (from ``utils.config.get_active_config``).
        data: Mapping of ticker → OHLCV DataFrame.
        market_id: Market identifier used by BacktestEngine.
        strategy_names: Strategy names to run. If ``None``, all
            enabled strategies from ``cfg`` are used.
        n_workers: Number of parallel worker processes.
            1 = serial mode (no subprocesses spawned).

    Returns:
        Dict with at minimum:
        ``{'trades': [...], 'equity_curve': Series,
           'metrics': {...}, 'benchmark_metrics': {...},
           'walk_forward_windows': [...]}``.
        On failure returns ``{'error': str, 'total_trades': 0}``.
    """
    # ── Resolve active strategy names ───────────────────────────────────────
    if strategy_names is None:
        strategy_names = [
            name
            for name, scfg in cfg.get("strategies", {}).items()
            if scfg.get("enabled", False)
        ]

    if not strategy_names:
        logger.warning("No enabled strategies found in config")
        return {"error": "No strategies enabled", "total_trades": 0}

    if not data:
        logger.warning("No ticker data provided")
        return {"error": "No data", "total_trades": 0}

    starting_equity = float(cfg.get("risk", {}).get("starting_equity", 5000.0))
    risk_free = float(cfg.get("risk", {}).get("risk_free_rate", 0.04))

    # ── Serial mode ─────────────────────────────────────────────────────────
    if n_workers <= 1:
        logger.info(
            "Serial mode: %d tickers, strategies=%s", len(data), strategy_names
        )
        result = _run_batch_backtest((cfg, strategy_names, data, market_id))
        if result is None:
            return {"error": "Backtest failed", "total_trades": 0}
        # metrics is already populated by the engine (included in _run_batch_backtest return)
        return result

    # ── Parallel mode ────────────────────────────────────────────────────────
    batches = _split_tickers(data, n_workers)
    actual_workers = len(batches)

    print(
        f"[backtest] {actual_workers} worker(s) | {len(data)} tickers | "
        f"strategies: {', '.join(strategy_names)}"
    )
    logger.info(
        "Parallel mode: %d tickers → %d batch(es), strategies=%s",
        len(data),
        actual_workers,
        strategy_names,
    )

    batch_args = [
        (copy.deepcopy(cfg), strategy_names, batch, market_id)
        for batch in batches
    ]

    batch_results: List[Optional[Dict[str, Any]]] = [None] * actual_workers

    try:
        with ProcessPoolExecutor(max_workers=actual_workers) as executor:
            future_to_idx = {
                executor.submit(_run_batch_backtest, args): i
                for i, args in enumerate(batch_args)
            }
            try:
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        batch_results[idx] = future.result()
                        logger.info("Worker %d completed", idx)
                    except Exception as exc:
                        logger.error("Worker %d raised: %s", idx, exc)
                        batch_results[idx] = None
            except KeyboardInterrupt:
                print("\n[backtest] Interrupted — shutting down workers…")
                executor.shutdown(wait=False, cancel_futures=True)
                raise

    except KeyboardInterrupt:
        sys.exit(1)

    return _merge_batch_results(batch_results, starting_equity, risk_free)


# ── Data loading ─────────────────────────────────────────────────────────────


def load_market_data(market_id: str) -> Dict[str, pd.DataFrame]:
    """Load cached OHLCV data for all tickers in a market.

    Delegates to ``strategy_evaluator.load_market_data`` so loading
    logic is maintained in one place.
    """
    # Prefer the version in strategy_evaluator to avoid duplication
    try:
        from scripts.strategy_evaluator import load_market_data as _load

        return _load(market_id)
    except ImportError:
        pass

    # Minimal fallback (mirrors strategy_evaluator logic)
    try:
        from markets import get_market

        market = get_market(market_id)
        benchmark = market.benchmark_ticker
        yf_suffix = market.yfinance_suffix
        valid_universe: Optional[set] = set(market.get_formatted_tickers())
        valid_universe.add(benchmark)
    except (ImportError, KeyError):
        benchmark = "IOZ.AX" if market_id == "asx" else "SPY"
        yf_suffix = ".AX" if market_id == "asx" else ""
        valid_universe = None

    cache_dir = PROJECT / "data" / "cache" / market_id
    data_dict: Dict[str, pd.DataFrame] = {}

    for pf in sorted(cache_dir.glob("*.parquet")):
        stem = pf.stem
        if yf_suffix:
            suffix_under = yf_suffix.replace(".", "_")
            if not stem.endswith(suffix_under):
                continue
            ticker = stem.replace(suffix_under, yf_suffix)
        else:
            if "_AX" in stem:
                continue
            ticker = stem

        if ticker == benchmark:
            continue
        if valid_universe is not None and ticker not in valid_universe:
            continue

        try:
            df = pd.read_parquet(pf)
            df.columns = [c.lower() for c in df.columns]
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date")
            df.index = pd.to_datetime(df.index)
            if len(df) >= 100:
                data_dict[ticker] = df
        except Exception:
            pass

    return data_dict


# ── CLI ──────────────────────────────────────────────────────────────────────


def _parse_args(argv: Optional[List[str]] = None) -> Any:
    parser = argparse.ArgumentParser(
        prog="backtest.py",
        description="Atlas Backtest CLI — parallelised walk-forward backtesting",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--market",
        required=True,
        help="Market identifier: asx, sp500, hk",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        metavar="N",
        help=(
            "Number of parallel worker processes. "
            "1 = serial mode (useful for debugging). "
            f"Default: min(8, cpu_count) = {DEFAULT_WORKERS}."
        ),
    )
    parser.add_argument(
        "--strategy",
        type=str,
        default=None,
        metavar="NAME",
        help="Run only this strategy (default: all enabled strategies in config).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        metavar="PATH",
        help="Save JSON result to this file path (default: stdout summary only).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress human-readable summary output.",
    )
    return parser.parse_args(argv)


def _print_summary(result: dict, market_id: str) -> None:
    """Print a human-readable metrics summary."""
    m = result.get("metrics", {})
    print(f"\n{'='*60}")
    print(f"ATLAS BACKTEST — {market_id.upper()}")
    print(f"{'='*60}")
    print(f"  Trades:      {m.get('total_trades', 0)}")
    print(f"  CAGR:        {(m.get('cagr', 0) or 0) * 100:.2f}%")
    print(f"  Sharpe:      {m.get('sharpe', 0):.4f}")
    print(f"  Sortino:     {m.get('sortino', 0):.4f}")
    print(f"  Max DD:      {(m.get('max_drawdown', 0) or 0) * 100:.2f}%")
    print(f"  Win Rate:    {(m.get('win_rate', 0) or 0) * 100:.1f}%")
    print(f"  PF:          {m.get('profit_factor', 0):.4f}")
    print(f"  Total PnL:   ${m.get('total_pnl', 0):.2f}")
    print(f"  Final Eq:    ${m.get('final_equity', 0):.2f}")
    bm = result.get("benchmark_metrics", {})
    if bm:
        print(f"{'─'*60}")
        print(f"  Benchmark CAGR: {(bm.get('cagr', 0) or 0) * 100:.2f}%")
        print(f"  Benchmark Sharpe: {bm.get('sharpe', 0):.4f}")
    print(f"{'='*60}\n")


def main(argv: Optional[List[str]] = None) -> int:
    import argparse  # already imported at top, but kept here for clarity

    args = _parse_args(argv)

    t0 = time.time()

    # Load config
    from utils.config import get_active_config

    cfg = get_active_config(args.market)

    # Load data
    print(f"[backtest] Loading market data for {args.market}…")
    data = load_market_data(args.market)
    if not data:
        print(f"[backtest] ERROR: No data found for market '{args.market}'", file=sys.stderr)
        return 1
    print(f"[backtest] Loaded {len(data)} tickers")

    # Resolve strategy names
    strategy_names: Optional[List[str]] = None
    if args.strategy:
        strategy_names = [args.strategy]

    n_workers = max(1, args.workers)
    print(f"[backtest] Starting backtest with {n_workers} worker(s)…")

    result = run_backtest(
        cfg=cfg,
        data=data,
        market_id=args.market,
        strategy_names=strategy_names,
        n_workers=n_workers,
    )

    elapsed = time.time() - t0

    if "error" in result:
        print(f"[backtest] ERROR: {result['error']}", file=sys.stderr)
        return 1

    trades = result.get("trades", [])
    print(f"[backtest] Complete in {elapsed:.1f}s — {len(trades)} trades")

    if not args.quiet:
        _print_summary(result, args.market)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Serialise non-JSON-serialisable types
        def _serialise(obj: Any) -> Any:
            if isinstance(obj, pd.Timestamp):
                return obj.isoformat()
            if isinstance(obj, pd.Series):
                return obj.to_dict()
            if hasattr(obj, "item"):  # numpy scalars
                return obj.item()
            raise TypeError(f"Not serialisable: {type(obj)}")

        with open(output_path, "w") as fh:
            json.dump(result, fh, indent=2, default=_serialise)
        print(f"[backtest] Saved to {output_path}")

    return 0


if __name__ == "__main__":
    import argparse

    sys.exit(main())
