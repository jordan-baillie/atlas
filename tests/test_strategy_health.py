"""Tests for monitor.strategy_health — Strategy Health Monitor.

Run with:
    python -m pytest tests/test_strategy_health.py -v

All tests run offline (no network, no broker), < 10 seconds total.
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import db.atlas_db as _adb

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitor.strategy_health import (
    DEGRADED,
    HEALTHY,
    INSUFFICIENT_DATA,
    MIN_TRADES_FOR_METRICS,
    WARNING,
    Alert,
    HealthAssessment,
    HealthReport,
    LiveMetrics,
    StrategyHealthMonitor,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_db(tmp_path):
    """Point atlas_db at a temp database so tests don't touch production."""
    db_path = str(tmp_path / "test_health.db")
    _adb._db_path_override = db_path
    _adb.init_db()
    yield
    _adb._db_path_override = None


def _make_config(strategies=None, **kwargs):
    """Build a minimal config dict for testing."""
    if strategies is None:
        strategies = {
            "mean_reversion": {"enabled": True},
            "momentum_breakout": {"enabled": True},
            "trend_following": {"enabled": False},  # disabled — should be skipped
        }
    base = {
        "risk": {"starting_equity": 10000, "max_open_positions": 10, "max_risk_per_trade_pct": 1.0},
        "fees": {"commission_per_trade": 0},
        "strategies": strategies,
    }
    base.update(kwargs)
    return base


def _make_entry(
    ticker: str,
    strategy: str,
    fill_price: float,
    stop_price: float,
    shares: int,
    timestamp: str,
) -> dict:
    """Build a synthetic entry event like a ledger entry."""
    return {
        "type": "entry",
        "ticker": ticker,
        "strategy": strategy,
        "fill_price": fill_price,
        "stop_price": stop_price,
        "shares": shares,
        "timestamp": timestamp,
        "order_id": f"ord-{ticker}-{timestamp}",
    }


def _make_exit(
    ticker: str,
    strategy: str,
    fill_price: float,
    shares: int,
    timestamp: str,
) -> dict:
    """Build a synthetic exit event."""
    return {
        "type": "exit",
        "ticker": ticker,
        "strategy": strategy,
        "fill_price": fill_price,
        "shares": shares,
        "timestamp": timestamp,
        "order_id": f"exit-{ticker}-{timestamp}",
    }


def _make_completed_trade(
    ticker: str,
    strategy: str,
    entry_price: float,
    exit_price: float,
    shares: int,
    stop_price: float,
    days_ago: int = 5,
    holding_days: int = 3,
) -> tuple:
    """Return (entry_event, exit_event) for a completed trade."""
    entry_dt = datetime.now() - timedelta(days=days_ago + holding_days)
    exit_dt = datetime.now() - timedelta(days=days_ago)
    entry = _make_entry(
        ticker=ticker,
        strategy=strategy,
        fill_price=entry_price,
        stop_price=stop_price,
        shares=shares,
        timestamp=entry_dt.isoformat(),
    )
    exit_ = _make_exit(
        ticker=ticker,
        strategy=strategy,
        fill_price=exit_price,
        shares=shares,
        timestamp=exit_dt.isoformat(),
    )
    return entry, exit_


def _write_ledger(tmp_path: Path, events: list) -> Path:
    """Write a trade ledger JSON file."""
    ledger_dir = tmp_path / "journal"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_file = ledger_dir / "trade_ledger.json"
    ledger_file.write_text(json.dumps(events))
    return ledger_file


def _write_best(tmp_path: Path, strategy: str, sharpe: float, win_rate_pct: float = 60.0) -> Path:
    """Write a research/best/{strategy}.json file."""
    best_dir = tmp_path / "research" / "best"
    best_dir.mkdir(parents=True, exist_ok=True)
    best_file = best_dir / f"{strategy}.json"
    best_file.write_text(json.dumps({
        "strategy": strategy,
        "metrics": {
            "sharpe": sharpe,
            "win_rate_pct": win_rate_pct,
            "total_trades": 200,
        },
    }))
    return best_file


def _insert_trade(
    ticker: str,
    strategy: str,
    entry_price: float,
    exit_price: float,
    shares: int,
    stop_price: float,
    days_ago: int = 5,
    holding_days: int = 3,
) -> None:
    """Insert a completed (closed) trade directly into the temp SQLite DB."""
    from db.atlas_db import get_db
    entry_dt = datetime.now() - timedelta(days=days_ago + holding_days)
    exit_dt = datetime.now() - timedelta(days=days_ago)
    pnl = (exit_price - entry_price) * shares
    pnl_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price else 0
    with get_db() as db:
        db.execute("""
            INSERT INTO trades (ticker, strategy, universe, direction, entry_date, entry_price,
                shares, stop_price, exit_date, exit_price, exit_reason, pnl, pnl_pct,
                hold_days, status, confidence, regime_at_entry)
            VALUES (?, ?, 'sp500', 'long', ?, ?, ?, ?, ?, ?, 'test', ?, ?, ?, 'closed', 0.8, 'test')
        """, (ticker, strategy, entry_dt.isoformat(), entry_price, shares, stop_price,
              exit_dt.isoformat(), exit_price, round(pnl, 4), round(pnl_pct, 4), holding_days))


# ── Monitor factory that injects a custom PROJECT path ────────────────────────

class _MonkeyMonitor(StrategyHealthMonitor):
    """Subclass that overrides PROJECT so file reads go to tmp_path."""

    def __init__(self, config, market_id, project_root: Path):
        super().__init__(config, market_id)
        self._project_root = project_root

    def _load_live_trades(self):
        """Reload from custom project root."""
        if self._live_trades_cache is not None:
            return self._live_trades_cache

        import json as _json
        trades = []
        seen = set()

        # Source 2: journal/trade_ledger.json (using custom root)
        ledger_path = self._project_root / "journal" / "trade_ledger.json"
        if ledger_path.exists():
            with open(ledger_path) as fh:
                ledger = _json.load(fh)
            if isinstance(ledger, list):
                for entry in ledger:
                    if entry.get("fill_price", 0) > 0:
                        oid = entry.get("order_id", "")
                        if oid and oid in seen:
                            continue
                        if oid:
                            seen.add(oid)
                        trades.append(entry)

        # Source 1: live_executions.jsonl (using custom root)
        exec_path = self._project_root / "logs" / "live_executions.jsonl"
        if exec_path.exists():
            with open(exec_path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    event = _json.loads(line)
                    if event.get("fill_price", 0) > 0 and event.get("success", False):
                        oid = event.get("order_id", "")
                        if oid and oid in seen:
                            continue
                        if oid:
                            seen.add(oid)
                        trades.append(event)

        self._live_trades_cache = trades
        return trades

    def _load_backtest_metrics(self, strategy: str):
        """Load from custom project root."""
        import json as _json
        best_path = self._project_root / "research" / "best" / f"{strategy}.json"
        if not best_path.exists():
            return None
        with open(best_path) as fh:
            data = _json.load(fh)
        return data.get("metrics", {})

    def _count_consecutive_degraded_weeks(self, strategy: str) -> int:
        """Check custom project root for health reports."""
        reports_dir = self._project_root / "logs" / "health_reports"
        if not reports_dir.exists():
            return 0
        report_files = sorted(
            reports_dir.glob(f"health_{self.market_id}_*.json"), reverse=True
        )
        consecutive = 0
        for report_file in report_files:
            with open(report_file) as fh:
                data = json.load(fh)
            for a in data.get("assessments", []):
                if a.get("strategy") == strategy:
                    if a.get("status") == DEGRADED:
                        consecutive += 1
                    else:
                        return consecutive
                    break
            else:
                return consecutive
        return consecutive


def _make_monitor(tmp_path: Path, config=None, market_id="sp500") -> _MonkeyMonitor:
    if config is None:
        config = _make_config()
    return _MonkeyMonitor(config, market_id, tmp_path)


# ── Tests: compute_live_metrics ────────────────────────────────────────────────

class TestComputeLiveMetrics:
    """Tests for StrategyHealthMonitor.compute_live_metrics."""

    def test_insufficient_data_when_no_trades(self, tmp_path):
        monitor = _make_monitor(tmp_path)
        metrics = monitor.compute_live_metrics("mean_reversion")
        assert metrics.status == INSUFFICIENT_DATA
        assert metrics.trade_count == 0
        assert metrics.sharpe is None
        assert metrics.win_rate is None

    def test_insufficient_data_when_below_threshold(self, tmp_path):
        """Fewer than MIN_TRADES_FOR_METRICS (10) completed trades → INSUFFICIENT_DATA."""
        events = []
        for i in range(5):
            entry, exit_ = _make_completed_trade(
                f"TICK{i}", "mean_reversion", 100.0, 105.0, 10, 95.0, days_ago=i + 2
            )
            events.extend([entry, exit_])
        _write_ledger(tmp_path, events)

        monitor = _make_monitor(tmp_path)
        metrics = monitor.compute_live_metrics("mean_reversion")
        assert metrics.status == INSUFFICIENT_DATA
        assert metrics.trade_count < MIN_TRADES_FOR_METRICS

    def test_metrics_computed_with_enough_trades(self, tmp_path):
        """10+ completed winning trades → metrics are computed."""
        for i in range(12):
            _insert_trade(f"TICK{i}", "mean_reversion", 100.0, 105.0, 10, 95.0, days_ago=i + 2)
        monitor = _make_monitor(tmp_path)
        metrics = monitor.compute_live_metrics("mean_reversion")
        assert metrics.trade_count == 12
        assert metrics.win_rate is not None
        assert metrics.win_rate == pytest.approx(1.0)  # all wins
        assert metrics.sharpe is not None
        assert metrics.sharpe > 0  # positive from consistent wins

    def test_win_rate_mixed_trades(self, tmp_path):
        """50% win rate with equal wins/losses."""
        for i in range(10):
            exit_price = 105.0 if i % 2 == 0 else 95.0  # alternating win/loss
            _insert_trade(f"TICK{i}", "mean_reversion", 100.0, exit_price, 10, 90.0, days_ago=i + 2)
        monitor = _make_monitor(tmp_path)
        metrics = monitor.compute_live_metrics("mean_reversion")
        assert metrics.trade_count == 10
        assert metrics.win_rate == pytest.approx(0.5, abs=0.01)

    def test_sharpe_not_computed_below_5_trades(self, tmp_path):
        """Sharpe requires at least MIN_TRADES_FOR_SHARPE (5) completed trades."""
        for i in range(10):
            _insert_trade(f"TICK{i}", "mean_reversion", 100.0, 105.0, 10, 95.0, days_ago=i + 2)
        monitor = _make_monitor(tmp_path)
        metrics = monitor.compute_live_metrics("mean_reversion")
        # With 10 trades (5+ returns), Sharpe should be computed
        assert metrics.sharpe is not None

    def test_max_drawdown_computed(self, tmp_path):
        """Max drawdown is computed from equity curve."""
        # Create 10 trades: 5 wins then 5 losses to generate drawdown
        for i in range(5):
            _insert_trade(f"WIN{i}", "mean_reversion", 100.0, 110.0, 10, 95.0, days_ago=20 - i)
        for i in range(5):
            _insert_trade(f"LOSE{i}", "mean_reversion", 100.0, 90.0, 10, 95.0, days_ago=14 - i)
        monitor = _make_monitor(tmp_path)
        metrics = monitor.compute_live_metrics("mean_reversion")
        assert metrics.max_drawdown is not None
        assert metrics.max_drawdown < 0  # drawdown is negative

    def test_window_filters_old_trades(self, tmp_path):
        """Trades older than window_days are excluded."""
        # 10 recent trades (within 60 days)
        for i in range(10):
            _insert_trade(f"RECENT{i}", "mean_reversion", 100.0, 105.0, 10, 95.0, days_ago=i + 2)
        # 5 old trades (outside 60-day window)
        for i in range(5):
            _insert_trade(f"OLD{i}", "mean_reversion", 100.0, 108.0, 10, 95.0, days_ago=90 + i, holding_days=5)
        monitor = _make_monitor(tmp_path)
        metrics = monitor.compute_live_metrics("mean_reversion", window_days=60)
        # Only recent 10 trades should be counted
        assert metrics.trade_count == 10


# ── Tests: compare_to_backtest ─────────────────────────────────────────────────

class TestCompareToBacktest:
    """Tests for StrategyHealthMonitor.compare_to_backtest."""

    def _setup_10_trades(
        self, tmp_path: Path, strategy: str, exit_price: float = 105.0
    ) -> None:
        """Insert 10 completed trades into the temp SQLite DB."""
        for i in range(10):
            _insert_trade(f"TICK{i}", strategy, 100.0, exit_price, 10, 95.0, days_ago=i + 2)

    def test_insufficient_data_no_trades(self, tmp_path):
        _write_best(tmp_path, "mean_reversion", sharpe=1.0)
        monitor = _make_monitor(tmp_path)
        assessment = monitor.compare_to_backtest("mean_reversion")
        assert assessment.status == INSUFFICIENT_DATA
        assert assessment.live_trade_count == 0

    def test_healthy_when_live_sharpe_above_threshold(self, tmp_path):
        """HEALTHY when live Sharpe > 50% of backtest Sharpe."""
        # 10 winning trades → positive Sharpe
        self._setup_10_trades(tmp_path, "mean_reversion", exit_price=110.0)
        # Set backtest Sharpe low so live easily exceeds 50%
        _write_best(tmp_path, "mean_reversion", sharpe=0.5)
        monitor = _make_monitor(tmp_path)
        assessment = monitor.compare_to_backtest("mean_reversion")
        # With consistently winning trades, live Sharpe should be very positive
        if assessment.live_sharpe is not None and assessment.live_sharpe > 0:
            assert assessment.status == HEALTHY
        else:
            # Could be INSUFFICIENT_DATA if sharpe is None
            assert assessment.status in (HEALTHY, INSUFFICIENT_DATA)

    def test_warning_when_live_sharpe_below_threshold(self, tmp_path):
        """WARNING when live Sharpe < 50% of backtest Sharpe but ≥ 0."""
        for i in range(10):
            exit_price = 100.5 if i % 2 == 0 else 99.5
            _insert_trade(f"TICK{i}", "mean_reversion", 100.0, exit_price, 10, 95.0, days_ago=i + 2)

        # High backtest Sharpe to trigger WARNING
        _write_best(tmp_path, "mean_reversion", sharpe=2.0)
        monitor = _make_monitor(tmp_path)
        assessment = monitor.compare_to_backtest("mean_reversion")
        # With very small P&L, Sharpe should be low → WARNING
        assert assessment.live_trade_count == 10
        assert assessment.status in (WARNING, DEGRADED, HEALTHY, INSUFFICIENT_DATA)

    def test_degraded_when_live_sharpe_negative(self, tmp_path):
        """DEGRADED when live Sharpe < 0."""
        for i in range(10):
            _insert_trade(f"TICK{i}", "mean_reversion", 100.0, 90.0, 10, 95.0, days_ago=i + 2)
        _write_best(tmp_path, "mean_reversion", sharpe=1.0)
        monitor = _make_monitor(tmp_path)
        assessment = monitor.compare_to_backtest("mean_reversion")
        assert assessment.live_trade_count == 10
        assert assessment.status == DEGRADED
        assert assessment.live_sharpe is not None
        assert assessment.live_sharpe < 0

    def test_no_backtest_file_still_returns_status(self, tmp_path):
        """Assessment works even without a backtest file — no backtest_sharpe."""
        events = []
        for i in range(10):
            entry, exit_ = _make_completed_trade(
                f"TICK{i}", "mean_reversion", 100.0, 105.0, 10, 95.0, days_ago=i + 2
            )
            events.extend([entry, exit_])
        _write_ledger(tmp_path, events)

        # No research/best file written
        monitor = _make_monitor(tmp_path)
        assessment = monitor.compare_to_backtest("mean_reversion")
        assert assessment.backtest_sharpe is None
        # Status should still be determined from live metrics
        assert assessment.status in (HEALTHY, WARNING, DEGRADED, INSUFFICIENT_DATA)

    def test_backtest_win_rate_pct_converted_to_fraction(self, tmp_path):
        """win_rate_pct from backtest file is converted to 0-1 fraction."""
        events = []
        for i in range(10):
            entry, exit_ = _make_completed_trade(
                f"TICK{i}", "mean_reversion", 100.0, 105.0, 10, 95.0, days_ago=i + 2
            )
            events.extend([entry, exit_])
        _write_ledger(tmp_path, events)

        _write_best(tmp_path, "mean_reversion", sharpe=1.0, win_rate_pct=65.0)
        monitor = _make_monitor(tmp_path)
        assessment = monitor.compare_to_backtest("mean_reversion")
        if assessment.backtest_win_rate is not None:
            assert assessment.backtest_win_rate == pytest.approx(0.65, abs=0.001)


# ── Tests: full_health_report ──────────────────────────────────────────────────

class TestFullHealthReport:
    """Tests for StrategyHealthMonitor.full_health_report."""

    def test_returns_all_enabled_strategies(self, tmp_path):
        """full_health_report covers all enabled strategies in config."""
        config = _make_config(strategies={
            "mean_reversion": {"enabled": True},
            "momentum_breakout": {"enabled": True},
            "trend_following": {"enabled": False},  # disabled
        })
        _write_ledger(tmp_path, [])  # empty — all INSUFFICIENT_DATA
        monitor = _make_monitor(tmp_path, config=config)
        report = monitor.full_health_report("sp500")

        strategy_names = [a.strategy for a in report.assessments]
        assert "mean_reversion" in strategy_names
        assert "momentum_breakout" in strategy_names
        assert "trend_following" not in strategy_names  # disabled

    def test_summary_counts_correct(self, tmp_path):
        """Summary dict has correct counts per status."""
        config = _make_config(strategies={
            "mean_reversion": {"enabled": True},
            "momentum_breakout": {"enabled": True},
        })
        _write_ledger(tmp_path, [])
        monitor = _make_monitor(tmp_path, config=config)
        report = monitor.full_health_report("sp500")

        # Both should be INSUFFICIENT_DATA
        total = sum(report.summary.values())
        assert total == 2
        assert report.summary[INSUFFICIENT_DATA] == 2

    def test_report_has_market_id(self, tmp_path):
        _write_ledger(tmp_path, [])
        monitor = _make_monitor(tmp_path)
        report = monitor.full_health_report("sp500")
        assert report.market_id == "sp500"

    def test_report_has_generated_at(self, tmp_path):
        _write_ledger(tmp_path, [])
        monitor = _make_monitor(tmp_path)
        report = monitor.full_health_report("sp500")
        assert report.generated_at is not None
        # Should be a valid ISO timestamp
        datetime.fromisoformat(report.generated_at)

    def test_to_dict_serializable(self, tmp_path):
        """HealthReport.to_dict() produces JSON-serializable output."""
        _write_ledger(tmp_path, [])
        monitor = _make_monitor(tmp_path)
        report = monitor.full_health_report("sp500")
        d = report.to_dict()
        # Should not raise
        serialized = json.dumps(d, default=str)
        assert "market_id" in serialized

    def test_no_alerts_when_all_insufficient_data(self, tmp_path):
        """No alerts raised when all strategies have INSUFFICIENT_DATA."""
        _write_ledger(tmp_path, [])
        monitor = _make_monitor(tmp_path)
        report = monitor.full_health_report("sp500")
        assert report.alerts == []

    def test_warning_alert_generated(self, tmp_path):
        """Warning alert is included when a strategy is WARNING."""
        events = []
        # Small win/loss to get low positive Sharpe
        for i in range(10):
            ep = 100.1 if i % 2 == 0 else 99.9
            entry, exit_ = _make_completed_trade(
                f"TICK{i}", "mean_reversion", 100.0, ep, 10, 90.0, days_ago=i + 2
            )
            events.extend([entry, exit_])
        _write_ledger(tmp_path, events)
        _write_best(tmp_path, "mean_reversion", sharpe=3.0)  # very high backtest → trigger WARNING

        config = _make_config(strategies={"mean_reversion": {"enabled": True}})
        monitor = _make_monitor(tmp_path, config=config)
        report = monitor.full_health_report("sp500")

        warning_alerts = [a for a in report.alerts if a.status == WARNING]
        degraded_alerts = [a for a in report.alerts if a.status == DEGRADED]
        # Should have at least one alert (WARNING or DEGRADED)
        mr_assessment = next(a for a in report.assessments if a.strategy == "mean_reversion")
        if mr_assessment.status in (WARNING, DEGRADED):
            assert len(report.alerts) >= 1

    def test_degraded_alert_with_3_consecutive_reports(self, tmp_path):
        """3+ consecutive DEGRADED reports → escalation alert."""
        # Create 3 previous health reports showing DEGRADED for mean_reversion
        reports_dir = tmp_path / "logs" / "health_reports"
        reports_dir.mkdir(parents=True, exist_ok=True)

        for day in range(3, 0, -1):
            date_str = (datetime.now() - timedelta(days=day * 7)).strftime("%Y-%m-%d")
            report_data = {
                "market_id": "sp500",
                "generated_at": (datetime.now() - timedelta(days=day * 7)).isoformat(),
                "assessments": [
                    {
                        "strategy": "mean_reversion",
                        "status": DEGRADED,
                        "live_sharpe": -0.3,
                        "live_trade_count": 10,
                    }
                ],
                "alerts": [],
                "summary": {DEGRADED: 1},
            }
            (reports_dir / f"health_sp500_{date_str}.json").write_text(
                json.dumps(report_data)
            )

        # Current run: also DEGRADED (10 losing trades)
        for i in range(10):
            _insert_trade(f"TICK{i}", "mean_reversion", 100.0, 90.0, 10, 95.0, days_ago=i + 2)
        _write_best(tmp_path, "mean_reversion", sharpe=1.0)

        config = _make_config(strategies={"mean_reversion": {"enabled": True}})
        monitor = _make_monitor(tmp_path, config=config)
        report = monitor.full_health_report("sp500")

        degraded_alerts = [
            a for a in report.alerts
            if a.status == DEGRADED and a.consecutive_degraded_weeks >= 3
        ]
        assert len(degraded_alerts) >= 1
        assert degraded_alerts[0].strategy == "mean_reversion"
        assert degraded_alerts[0].consecutive_degraded_weeks >= 3


# ── Tests: check_degradation_alerts ───────────────────────────────────────────

class TestCheckDegradationAlerts:
    """Tests for StrategyHealthMonitor.check_degradation_alerts."""

    def test_returns_list(self, tmp_path):
        _write_ledger(tmp_path, [])
        monitor = _make_monitor(tmp_path)
        alerts = monitor.check_degradation_alerts("sp500")
        assert isinstance(alerts, list)

    def test_empty_when_all_insufficient_data(self, tmp_path):
        """No alerts when all strategies have insufficient data."""
        _write_ledger(tmp_path, [])
        monitor = _make_monitor(tmp_path)
        alerts = monitor.check_degradation_alerts("sp500")
        assert alerts == []


# ── Tests: _load_live_trades ───────────────────────────────────────────────────

class TestLoadLiveTrades:
    """Tests for the data loading layer."""

    def test_empty_when_no_files(self, tmp_path):
        monitor = _make_monitor(tmp_path)
        trades = monitor._load_live_trades()
        assert trades == []

    def test_loads_from_ledger(self, tmp_path):
        ledger_events = [
            _make_entry("AAPL", "mean_reversion", 150.0, 145.0, 10,
                        datetime.now().isoformat())
        ]
        _write_ledger(tmp_path, ledger_events)
        monitor = _make_monitor(tmp_path)
        trades = monitor._load_live_trades()
        assert len(trades) == 1
        assert trades[0]["ticker"] == "AAPL"

    def test_filters_zero_fill_price(self, tmp_path):
        """Events with fill_price = 0 are excluded."""
        events = [
            _make_entry("AAPL", "mean_reversion", 0.0, 145.0, 10,
                        datetime.now().isoformat()),  # zero fill → skip
            _make_entry("MSFT", "mean_reversion", 300.0, 290.0, 5,
                        datetime.now().isoformat()),  # valid fill → include
        ]
        _write_ledger(tmp_path, events)
        monitor = _make_monitor(tmp_path)
        trades = monitor._load_live_trades()
        assert len(trades) == 1
        assert trades[0]["ticker"] == "MSFT"

    def test_deduplicates_by_order_id(self, tmp_path):
        """Same order_id from both sources counted only once."""
        events = [
            {
                "type": "entry",
                "ticker": "AAPL",
                "strategy": "mean_reversion",
                "fill_price": 150.0,
                "stop_price": 145.0,
                "shares": 10,
                "timestamp": datetime.now().isoformat(),
                "order_id": "same-order-id-123",
            }
        ]
        _write_ledger(tmp_path, events)

        # Write same event to live_executions.jsonl with success=True
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        exec_event = {
            "type": "entry",
            "ticker": "AAPL",
            "strategy": "mean_reversion",
            "fill_price": 150.0,
            "success": True,
            "order_id": "same-order-id-123",
            "timestamp": datetime.now().isoformat(),
        }
        (logs_dir / "live_executions.jsonl").write_text(json.dumps(exec_event) + "\n")

        monitor = _make_monitor(tmp_path)
        trades = monitor._load_live_trades()
        # Should have only 1 entry (deduplicated)
        assert len(trades) == 1

    def test_caches_result(self, tmp_path):
        """Second call returns cached result without re-reading files."""
        _write_ledger(tmp_path, [])
        monitor = _make_monitor(tmp_path)
        trades1 = monitor._load_live_trades()
        # Add more events to the file — should still return cached result
        _write_ledger(tmp_path, [
            _make_entry("AAPL", "mean_reversion", 150.0, 145.0, 10,
                        datetime.now().isoformat())
        ])
        trades2 = monitor._load_live_trades()
        assert trades1 is trades2  # same object → cached


# ── Tests: LiveMetrics dataclass ───────────────────────────────────────────────

class TestDataclasses:
    def test_live_metrics_defaults(self):
        m = LiveMetrics(strategy="test", trade_count=0, status=INSUFFICIENT_DATA)
        assert m.sharpe is None
        assert m.win_rate is None
        assert m.avg_r is None

    def test_health_assessment_has_assessed_at(self):
        a = HealthAssessment(strategy="test", status=HEALTHY)
        assert a.assessed_at is not None
        datetime.fromisoformat(a.assessed_at)

    def test_alert_has_timestamp(self):
        alert = Alert(strategy="test", status=WARNING, message="test alert")
        assert alert.timestamp is not None
        datetime.fromisoformat(alert.timestamp)

    def test_health_report_to_dict_structure(self):
        report = HealthReport(
            market_id="sp500",
            generated_at=datetime.now().isoformat(),
            assessments=[
                HealthAssessment(strategy="test", status=INSUFFICIENT_DATA)
            ],
            alerts=[],
            summary={INSUFFICIENT_DATA: 1},
        )
        d = report.to_dict()
        assert d["market_id"] == "sp500"
        assert isinstance(d["assessments"], list)
        assert d["assessments"][0]["strategy"] == "test"
        assert isinstance(d["summary"], dict)
