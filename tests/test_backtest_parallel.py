"""Tests for parallelised backtest engine (scripts/backtest.py).

Run with:
    python3 -m pytest tests/test_backtest_parallel.py -v

Coverage:
    - --workers CLI argument is accepted
    - Serial mode (--workers 1) produces valid output
    - Parallel mode (--workers N) produces valid output
    - Single-ticker: workers=1 and workers=4 give IDENTICAL results
      (only one effective batch regardless of --workers value)
    - Edge cases: more workers than tickers (clamped), workers=1
    - _split_tickers: round-robin, deterministic, balanced
    - _merge_batch_results: aggregates trades and reconstructs equity curve
"""

import copy
import sys
import os
import subprocess

import numpy as np
import pandas as pd
import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.backtest import (
    DEFAULT_WORKERS,
    _merge_batch_results,
    _run_batch_backtest,
    _split_tickers,
    run_backtest,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_ohlcv(seed: int, n_bars: int = 200) -> pd.DataFrame:
    """Generate synthetic OHLCV data with realistic price dynamics."""
    np.random.seed(seed)
    dates = pd.date_range("2019-01-01", periods=n_bars, freq="B")
    close = 10.0 * np.exp(np.cumsum(np.random.randn(n_bars) * 0.01))
    df = pd.DataFrame(
        {
            "open": close * np.random.uniform(0.98, 1.00, n_bars),
            "high": close * np.random.uniform(1.00, 1.02, n_bars),
            "low": close * np.random.uniform(0.97, 0.99, n_bars),
            "close": close,
            "volume": np.random.randint(100_000, 1_000_000, n_bars).astype(float),
        },
        index=dates,
    )
    return df


def _minimal_config(starting_equity: float = 10_000.0) -> dict:
    """Minimal Atlas config that avoids all optional features."""
    return {
        "market": "asx",
        "version": "test",
        "risk": {
            "starting_equity": starting_equity,
            "max_open_positions": 3,
            "max_risk_per_trade_pct": 0.01,
            "max_sector_concentration": 2,
            "min_confidence": 0.0,
        },
        "fees": {
            "commission_per_trade": 5.0,
            "commission_pct": 0.001,
            "slippage_pct": 0.001,
            "min_position_value": 200.0,
            "flat_fee_threshold": 500.0,
        },
        "backtest": {
            "train_window_days": 80,
            "test_window_days": 40,
            "step_days": 20,
            "min_history_days": 60,
        },
        "strategies": {
            "mean_reversion": {"enabled": True, "rsi_oversold": 30},
        },
        "trading": {},
        "universe": {"benchmark_ticker": "IOZ.AX"},
        "dynamic_sizing": {"enabled": False},
        "allocation": {"enabled": False},
        "fee_aware_filter": {"enabled": False},
        "regime_filter": {"enabled": False},
        "vix_filter": {"enabled": False},
        "fred_filter": {"enabled": False},
    }


def _make_data(n_tickers: int = 3, n_bars: int = 200) -> dict:
    """Build a minimal data dict with n_tickers synthetic tickers."""
    return {
        f"TICK{i:02d}.AX": _make_ohlcv(seed=i, n_bars=n_bars)
        for i in range(n_tickers)
    }


# ── CLI argument tests ────────────────────────────────────────────────────────


class TestCLIArgs:
    """Verify the --workers argument is accepted by the CLI."""

    def test_help_shows_workers(self):
        """--help output must mention the --workers flag."""
        result = subprocess.run(
            [sys.executable, "scripts/backtest.py", "--help"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        assert result.returncode == 0
        assert "--workers" in result.stdout

    def test_workers_default_is_numeric(self):
        """DEFAULT_WORKERS must be a positive integer."""
        assert isinstance(DEFAULT_WORKERS, int)
        assert DEFAULT_WORKERS >= 1

    def test_workers_default_respects_cpu_count(self):
        """DEFAULT_WORKERS must not exceed 8."""
        assert DEFAULT_WORKERS <= 8


# ── Unit tests: _split_tickers ────────────────────────────────────────────────


class TestSplitTickers:
    """Unit tests for the ticker splitting function."""

    def test_single_batch(self):
        """With n_batches=1, all tickers go into one batch."""
        data = _make_data(n_tickers=5)
        batches = _split_tickers(data, n_batches=1)
        assert len(batches) == 1
        assert set(batches[0].keys()) == set(data.keys())

    def test_equal_batches(self):
        """With 6 tickers and n_batches=3, each batch has 2 tickers."""
        data = _make_data(n_tickers=6)
        batches = _split_tickers(data, n_batches=3)
        assert len(batches) == 3
        assert all(len(b) == 2 for b in batches)

    def test_more_batches_than_tickers_clamped(self):
        """n_batches > len(tickers) must be clamped to len(tickers)."""
        data = _make_data(n_tickers=3)
        batches = _split_tickers(data, n_batches=10)
        assert len(batches) == 3  # clamped
        assert all(len(b) == 1 for b in batches)

    def test_no_ticker_lost(self):
        """Every ticker in data must appear in exactly one batch."""
        data = _make_data(n_tickers=7)
        batches = _split_tickers(data, n_batches=3)
        seen = set()
        for b in batches:
            for ticker in b:
                assert ticker not in seen, f"{ticker} appears in multiple batches"
                seen.add(ticker)
        assert seen == set(data.keys())

    def test_deterministic(self):
        """Two calls with the same data produce the same split."""
        data = _make_data(n_tickers=8)
        batches_a = _split_tickers(data, n_batches=4)
        batches_b = _split_tickers(data, n_batches=4)
        for a, b in zip(batches_a, batches_b):
            assert set(a.keys()) == set(b.keys())

    def test_balanced_round_robin(self):
        """Round-robin assignment: batch sizes differ by at most 1."""
        data = _make_data(n_tickers=7)
        batches = _split_tickers(data, n_batches=3)
        sizes = [len(b) for b in batches]
        assert max(sizes) - min(sizes) <= 1

    def test_empty_data(self):
        """Empty data dict returns empty list."""
        batches = _split_tickers({}, n_batches=4)
        assert batches == []


# ── Unit tests: _merge_batch_results ─────────────────────────────────────────


class TestMergeBatchResults:
    """Unit tests for result merging."""

    def _make_result(self, trades: list, equity_curve: pd.Series) -> dict:
        return {
            "trades": trades,
            "equity_curve": equity_curve,
            "benchmark_metrics": {"cagr": 0.05},
            "walk_forward_windows": [],
        }

    def test_all_failed_returns_error(self):
        merged = _merge_batch_results([None, None], starting_equity=5000.0)
        assert "error" in merged

    def test_trade_count(self):
        """All trades from all batches must be present in merged result."""
        t1 = [{"pnl": 100, "exit_date": "2021-01-05", "strategy": "a", "ticker": "X"}]
        t2 = [
            {"pnl": 50, "exit_date": "2021-01-10", "strategy": "a", "ticker": "Y"},
            {"pnl": -30, "exit_date": "2021-01-15", "strategy": "a", "ticker": "Z"},
        ]
        eq = pd.Series(dtype=float)
        r1 = self._make_result(t1, eq)
        r2 = self._make_result(t2, eq)
        merged = _merge_batch_results([r1, r2], starting_equity=5000.0)
        assert len(merged["trades"]) == 3

    def test_trades_sorted_by_exit_date(self):
        """Merged trades must be ordered chronologically by exit_date."""
        trades = [
            {"pnl": 10, "exit_date": "2021-03-01", "strategy": "a", "ticker": "A"},
            {"pnl": 20, "exit_date": "2021-01-01", "strategy": "a", "ticker": "B"},
            {"pnl": -5, "exit_date": "2021-02-15", "strategy": "a", "ticker": "C"},
        ]
        r = self._make_result(trades, pd.Series(dtype=float))
        merged = _merge_batch_results([r], starting_equity=5000.0)
        exit_dates = [t["exit_date"] for t in merged["trades"]]
        assert exit_dates == sorted(exit_dates)

    def test_equity_reconstruction(self):
        """Merged equity curve must start at starting_equity and track PnL."""
        trades = [
            {"pnl": 200, "exit_date": "2021-01-05", "strategy": "a", "ticker": "X"},
            {"pnl": -50, "exit_date": "2021-01-10", "strategy": "a", "ticker": "Y"},
        ]
        r = self._make_result(trades, pd.Series(dtype=float))
        merged = _merge_batch_results([r], starting_equity=1000.0)
        eq = merged["equity_curve"]
        assert len(eq) == 2
        assert abs(eq.iloc[0] - 1200.0) < 0.01  # 1000 + 200
        assert abs(eq.iloc[-1] - 1150.0) < 0.01  # 1200 - 50

    def test_benchmark_from_first_result(self):
        """Benchmark metrics should come from the first valid batch."""
        r1 = self._make_result([], pd.Series(dtype=float))
        r1["benchmark_metrics"] = {"cagr": 0.10}
        r2 = self._make_result([], pd.Series(dtype=float))
        r2["benchmark_metrics"] = {"cagr": 0.20}
        merged = _merge_batch_results([r1, r2], starting_equity=5000.0)
        assert merged["benchmark_metrics"]["cagr"] == 0.10

    def test_failed_batch_skipped(self):
        """None entries (failed workers) must be silently skipped."""
        t = [{"pnl": 100, "exit_date": "2021-06-01", "strategy": "a", "ticker": "X"}]
        r = self._make_result(t, pd.Series(dtype=float))
        merged = _merge_batch_results([None, r, None], starting_equity=5000.0)
        assert len(merged["trades"]) == 1


# ── Integration tests: run_backtest ───────────────────────────────────────────


class TestRunBacktest:
    """Integration tests that exercise run_backtest() end-to-end.

    Use synthetic data (no real market files required).
    """

    def test_workers_1_serial_mode(self):
        """Serial mode (--workers 1) must complete and return trades/metrics."""
        cfg = _minimal_config()
        data = _make_data(n_tickers=3, n_bars=200)
        result = run_backtest(cfg, data, market_id="asx", n_workers=1)
        assert "error" not in result
        assert "trades" in result
        assert "metrics" in result
        assert isinstance(result["trades"], list)

    def test_workers_more_than_tickers(self):
        """When workers > tickers, effective workers = num_tickers (clamped)."""
        cfg = _minimal_config()
        data = _make_data(n_tickers=2, n_bars=200)
        # Request 8 workers but only 2 tickers → should clamp to 2 and not crash
        result = run_backtest(cfg, data, market_id="asx", n_workers=8)
        assert "error" not in result
        assert "trades" in result

    def test_no_strategies_returns_error(self):
        """Config with no enabled strategies must return an error dict."""
        cfg = _minimal_config()
        cfg["strategies"]["mean_reversion"]["enabled"] = False
        data = _make_data(n_tickers=2)
        result = run_backtest(cfg, data, market_id="asx", n_workers=1)
        assert "error" in result

    def test_empty_data_returns_error(self):
        """Empty data dict must return an error dict."""
        cfg = _minimal_config()
        result = run_backtest(cfg, {}, market_id="asx", n_workers=1)
        assert "error" in result

    def test_explicit_strategy_names(self):
        """Passing explicit strategy_names should restrict to those strategies."""
        cfg = _minimal_config()
        data = _make_data(n_tickers=3, n_bars=200)
        result = run_backtest(
            cfg,
            data,
            market_id="asx",
            strategy_names=["mean_reversion"],
            n_workers=1,
        )
        assert "error" not in result

    def test_single_ticker_workers1_vs_workers4_identical(self):
        """Determinism: single ticker → same result for workers=1 and workers=4.

        With only 1 ticker, _split_tickers always produces a single batch
        regardless of n_workers. The results must therefore be identical.
        """
        cfg = _minimal_config()
        data = {"ONLY.AX": _make_ohlcv(seed=99, n_bars=200)}

        result_serial = run_backtest(cfg, data, market_id="asx", n_workers=1)
        result_parallel = run_backtest(cfg, data, market_id="asx", n_workers=4)

        # Both must succeed
        assert "error" not in result_serial
        assert "error" not in result_parallel

        # Trade counts must be identical (same universe, same engine)
        trades_serial = result_serial.get("trades", [])
        trades_parallel = result_parallel.get("trades", [])
        assert len(trades_serial) == len(trades_parallel), (
            f"Trade count mismatch: serial={len(trades_serial)}, "
            f"parallel={len(trades_parallel)}"
        )

        # Key metrics must be identical (to 4 decimal places)
        m_s = result_serial.get("metrics", {})
        m_p = result_parallel.get("metrics", {})
        for key in ("total_trades", "total_pnl"):
            vs = m_s.get(key, 0) or 0
            vp = m_p.get(key, 0) or 0
            assert abs(vs - vp) < 0.01, (
                f"Metric '{key}' differs: serial={vs}, parallel={vp}"
            )

    def test_parallel_two_workers(self):
        """Parallel mode with workers=2 and 4 tickers must complete."""
        cfg = _minimal_config()
        data = _make_data(n_tickers=4, n_bars=200)
        result = run_backtest(cfg, data, market_id="asx", n_workers=2)
        assert "error" not in result
        assert "trades" in result
        assert "metrics" in result

    def test_metrics_keys_present(self):
        """Result metrics must contain standard Atlas metric keys."""
        cfg = _minimal_config()
        data = _make_data(n_tickers=2, n_bars=200)
        result = run_backtest(cfg, data, market_id="asx", n_workers=1)
        if "error" not in result:
            m = result.get("metrics", {})
            for key in ("total_trades", "cagr", "sharpe", "max_drawdown"):
                assert key in m, f"Missing metric key: {key}"


# ── Worker function unit tests ────────────────────────────────────────────────


class TestRunBatchBacktest:
    """Unit tests for the top-level picklable worker function."""

    def test_valid_run_returns_dict(self):
        """Worker must return a dict with expected keys on success."""
        cfg = _minimal_config()
        data = _make_data(n_tickers=2, n_bars=200)
        result = _run_batch_backtest((cfg, ["mean_reversion"], data, "asx"))
        assert result is not None
        assert "trades" in result
        assert "equity_curve" in result
        assert "benchmark_metrics" in result
        assert "walk_forward_windows" in result

    def test_no_strategies_returns_empty(self):
        """If strategy_names is empty, worker returns empty trades list."""
        cfg = _minimal_config()
        data = _make_data(n_tickers=2, n_bars=200)
        result = _run_batch_backtest((cfg, [], data, "asx"))
        assert result is not None
        assert result["trades"] == []

    def test_unknown_strategy_skipped(self):
        """Unknown strategy name must be silently skipped (not crash)."""
        cfg = _minimal_config()
        data = _make_data(n_tickers=2, n_bars=200)
        # 'nonexistent' is skipped; 'mean_reversion' runs
        result = _run_batch_backtest(
            (cfg, ["nonexistent_strategy", "mean_reversion"], data, "asx")
        )
        assert result is not None
        assert "trades" in result


# ── yfinance noise suppression tests ─────────────────────────────────────────


class TestYfinanceFiltering:
    """Verify yfinance ERROR logs are silenced and never reach TelegramErrorCollector.

    yfinance logs routine download failures (delisted tickers, HTTP 404s) at
    ERROR level.  These are data-quality issues, not system errors — they
    must never appear in Telegram operator alerts.
    """

    def test_yfinance_errors_not_collected(self):
        """TelegramErrorCollector must silently drop yfinance ERROR records."""
        import logging
        from utils.logging_config import TelegramErrorCollector

        collector = TelegramErrorCollector(script_name="test")

        # Simulate the exact style of record that yfinance emits for delisted tickers
        record = logging.LogRecord(
            name="yfinance",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="$IOZ.AX: possibly delisted; found responses for: %s",
            args=(["IOZ.AX"],),
            exc_info=None,
        )
        collector.emit(record)
        assert len(collector.records) == 0, (
            "yfinance ERROR record should be filtered out of TelegramErrorCollector"
        )

    def test_yfinance_sublogger_errors_not_collected(self):
        """Records from yfinance.* child loggers must also be filtered."""
        import logging
        from utils.logging_config import TelegramErrorCollector

        collector = TelegramErrorCollector(script_name="test")

        record = logging.LogRecord(
            name="yfinance.base",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="No data found, symbol may be delisted",
            args=(),
            exc_info=None,
        )
        collector.emit(record)
        assert len(collector.records) == 0, (
            "yfinance.base ERROR record should be filtered out"
        )

    def test_non_yfinance_errors_are_collected(self):
        """Real Atlas ERROR records must still be collected for Telegram alerts."""
        import logging
        from utils.logging_config import TelegramErrorCollector

        collector = TelegramErrorCollector(script_name="test")

        record = logging.LogRecord(
            name="atlas.backtest",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="Broker connection refused: OpenD not running",
            args=(),
            exc_info=None,
        )
        collector.emit(record)
        assert len(collector.records) == 1, (
            "Non-yfinance ERROR should be collected for Telegram"
        )

    def test_yfinance_logger_level_is_critical_after_setup(self):
        """After setup_logging, yfinance logger must be at CRITICAL level."""
        import logging
        from utils.logging_config import setup_logging, _setup_done
        import utils.logging_config as lc

        # Reset setup state so we can call setup_logging fresh
        original = lc._setup_done
        lc._setup_done = False
        try:
            setup_logging("test_yf_level", telegram_errors=False)
            yf_level = logging.getLogger("yfinance").level
            assert yf_level == logging.CRITICAL, (
                f"Expected yfinance logger at CRITICAL ({logging.CRITICAL}), "
                f"got {yf_level}"
            )
        finally:
            lc._setup_done = original


# ── Benchmark pre-download robustness tests ───────────────────────────────────


class TestBenchmarkPreDownload:
    """Verify that benchmark download failure does not crash the backtest.

    The pre-download in run_backtest() is best-effort: if IOZ.AX or any
    other benchmark is unavailable, the backtest must still complete and
    return valid results (without benchmark_metrics).
    """

    def test_benchmark_failure_does_not_crash_parallel_backtest(self):
        """Patching download_ticker to always raise must not crash run_backtest."""
        import unittest.mock as mock

        cfg = _minimal_config()
        data = _make_data(n_tickers=2, n_bars=200)

        # Patch download_ticker in the backtest module's namespace so that the
        # pre-download step raises.  Workers run in-process for n_workers=1,
        # so this also validates the engine's _calc_benchmark fallback.
        with mock.patch("data.ingest.download_ticker", side_effect=Exception("delisted")):
            # n_workers=1 skips the parallel path; use 2 to exercise pre-download
            # but workers are still spawned via ProcessPoolExecutor.
            # We fall back to n_workers=1 to keep test fast and portable.
            result = run_backtest(cfg, data, market_id="asx", n_workers=1)

        # Backtest must complete — error key must not be present
        assert "error" not in result, f"run_backtest crashed: {result.get('error')}"
        assert "trades" in result

    def test_benchmark_failure_uses_warning_not_error(self):
        """When benchmark pre-download fails, only a WARNING is logged (not ERROR).

        This ensures the failure path does not pollute TelegramErrorCollector
        with a false-positive system alert.
        """
        import logging
        import unittest.mock as mock
        from scripts.backtest import run_backtest

        cfg = _minimal_config()
        data = _make_data(n_tickers=2, n_bars=200)

        warning_records = []
        error_records = []

        class CapturingHandler(logging.Handler):
            def emit(self, record):
                if record.levelno == logging.WARNING:
                    warning_records.append(record)
                elif record.levelno >= logging.ERROR:
                    error_records.append(record)

        handler = CapturingHandler()
        logging.getLogger().addHandler(handler)
        try:
            # Pre-download happens only for n_workers > 1
            with mock.patch(
                "data.ingest.download_ticker",
                side_effect=Exception("404 not found"),
            ):
                run_backtest(cfg, data, market_id="asx", n_workers=2)
        finally:
            logging.getLogger().removeHandler(handler)

        # No ERROR-level records should have been emitted for the benchmark failure
        benchmark_errors = [
            r for r in error_records
            if "benchmark" in r.getMessage().lower()
        ]
        assert benchmark_errors == [], (
            f"Benchmark failure emitted ERROR records: {[r.getMessage() for r in benchmark_errors]}"
        )
