"""Tests for scripts/reconcile.py — broker/local state reconciliation."""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path so `scripts.reconcile` can be imported.
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from scripts.reconcile import (
    Discrepancy,
    ReconciliationReport,
    StateReconciler,
)


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

MINIMAL_CONFIG: dict = {
    "market": "sp500",
    "trading": {"mode": "live", "broker": "alpaca"},
    "alpaca": {"base_url": "https://paper-api.alpaca.markets"},
}


def _make_report(discrepancies=None) -> ReconciliationReport:
    r = ReconciliationReport(timestamp="2026-03-15T10:00:00", market_id="sp500")
    if discrepancies:
        r.discrepancies = discrepancies
    return r


# ─────────────────────────────────────────────
# Discrepancy dataclass
# ─────────────────────────────────────────────

class TestDiscrepancy:
    def test_required_fields(self):
        d = Discrepancy(
            category="missing_local",
            ticker="AAPL",
            description="test desc",
            severity="high",
        )
        assert d.category == "missing_local"
        assert d.ticker == "AAPL"
        assert d.description == "test desc"
        assert d.severity == "high"

    def test_defaults(self):
        d = Discrepancy(
            category="missing_broker",
            ticker="MSFT",
            description="x",
            severity="low",
        )
        assert d.auto_fixable is False
        assert d.fix_action == ""
        assert d.fixed is False

    def test_auto_fixable_fields(self):
        d = Discrepancy(
            category="sl_filled",
            ticker="TSLA",
            description="SL hit",
            severity="high",
            auto_fixable=True,
            fix_action="Mark closed",
        )
        assert d.auto_fixable is True
        assert d.fix_action == "Mark closed"
        assert d.fixed is False


# ─────────────────────────────────────────────
# ReconciliationReport
# ─────────────────────────────────────────────

class TestReconciliationReport:
    def test_clean_when_no_discrepancies(self):
        r = _make_report()
        assert r.clean is True

    def test_not_clean_when_discrepancies_present(self):
        d = Discrepancy(category="missing_local", ticker="AAPL", description="x", severity="high")
        r = _make_report([d])
        assert r.clean is False

    def test_clean_false_after_appending(self):
        r = _make_report()
        assert r.clean is True
        r.discrepancies.append(
            Discrepancy(category="missing_broker", ticker="GOOG", description="x", severity="low")
        )
        assert r.clean is False

    def test_to_dict_contains_expected_keys(self):
        r = _make_report()
        d = r.to_dict()
        assert "timestamp" in d
        assert "market_id" in d
        assert "broker_positions" in d
        assert "local_positions" in d
        assert "discrepancies" in d
        assert "fixes_applied" in d
        assert "broker_equity" in d
        assert "clean" in d  # derived property serialised into dict

    def test_to_dict_clean_true(self):
        r = _make_report()
        assert r.to_dict()["clean"] is True

    def test_to_dict_clean_false(self):
        d = Discrepancy(category="sl_filled", ticker="X", description="x", severity="medium")
        r = _make_report([d])
        result = r.to_dict()
        assert result["clean"] is False
        assert len(result["discrepancies"]) == 1
        assert result["discrepancies"][0]["ticker"] == "X"

    def test_to_dict_discrepancy_serialisation(self):
        d = Discrepancy(
            category="missing_local",
            ticker="NVDA",
            description="test",
            severity="high",
            auto_fixable=True,
            fix_action="Add tracking",
        )
        r = _make_report([d])
        result = r.to_dict()
        disc_dict = result["discrepancies"][0]
        assert disc_dict["category"] == "missing_local"
        assert disc_dict["auto_fixable"] is True
        assert disc_dict["fixed"] is False

    def test_default_numeric_fields(self):
        r = ReconciliationReport(timestamp="t", market_id="asx")
        assert r.broker_positions == 0
        assert r.local_positions == 0
        assert r.broker_equity == 0.0


# ─────────────────────────────────────────────
# StateReconciler — _get_local_positions
# ─────────────────────────────────────────────

class TestGetLocalPositions:
    def test_empty_when_no_ledger(self, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.reconcile.PROJECT", tmp_path)
        reconciler = StateReconciler(MINIMAL_CONFIG, "sp500")
        positions = reconciler._get_local_positions()
        assert positions == {}
        assert reconciler.report.local_positions == 0

    def test_reads_open_positions_from_ledger(self, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.reconcile.PROJECT", tmp_path)
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        # Flat list of entry/exit events (current ledger format)
        ledger = [
            {
                "type": "entry",
                "ticker": "AAPL",
                "strategy": "momentum_breakout",
                "timestamp": "2026-03-01T10:00:00",
                "fill_price": 175.0,
                "shares": 10,
                "direction": "long",
            },
            {
                "type": "entry",
                "ticker": "MSFT",
                "strategy": "trend_following",
                "timestamp": "2026-03-02T10:00:00",
                "fill_price": 400.0,
                "shares": 5,
                "direction": "long",
            },
        ]
        (journal_dir / "trade_ledger.json").write_text(json.dumps(ledger))
        reconciler = StateReconciler(MINIMAL_CONFIG, "sp500")
        positions = reconciler._get_local_positions()
        assert "AAPL" in positions
        assert "MSFT" in positions
        assert positions["AAPL"]["entry_price"] == 175.0
        assert reconciler.report.local_positions == 2

    def test_skips_closed_positions(self, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.reconcile.PROJECT", tmp_path)
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        # GOOG has entry + exit = net 0 (closed), AAPL has entry only (open)
        ledger = [
            {"type": "entry", "ticker": "AAPL", "strategy": "x", "fill_price": 100, "shares": 1},
            {"type": "entry", "ticker": "GOOG", "strategy": "x", "fill_price": 200, "shares": 2},
            {"type": "exit", "ticker": "GOOG", "strategy": "x", "fill_price": 210, "shares": 2},
        ]
        (journal_dir / "trade_ledger.json").write_text(json.dumps(ledger))
        reconciler = StateReconciler(MINIMAL_CONFIG, "sp500")
        positions = reconciler._get_local_positions()
        assert "AAPL" in positions
        assert "GOOG" not in positions

    def test_handles_partial_exits(self, tmp_path, monkeypatch):
        """Position with partial exit shows remaining shares as open."""
        monkeypatch.setattr("scripts.reconcile.PROJECT", tmp_path)
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        ledger = [
            {"type": "entry", "ticker": "TSLA", "strategy": "x", "fill_price": 250, "shares": 10},
            {"type": "exit", "ticker": "TSLA", "strategy": "x", "fill_price": 260, "shares": 7},
        ]
        (journal_dir / "trade_ledger.json").write_text(json.dumps(ledger))
        reconciler = StateReconciler(MINIMAL_CONFIG, "sp500")
        positions = reconciler._get_local_positions()
        assert "TSLA" in positions
        assert positions["TSLA"]["shares"] == 3

    def test_graceful_on_corrupt_ledger(self, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.reconcile.PROJECT", tmp_path)
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        (journal_dir / "trade_ledger.json").write_text("NOT VALID JSON {{{{")
        reconciler = StateReconciler(MINIMAL_CONFIG, "sp500")
        positions = reconciler._get_local_positions()
        assert positions == {}


# ─────────────────────────────────────────────
# StateReconciler.reconcile() — core logic
# ─────────────────────────────────────────────

class TestReconcileLogic:
    """Test reconcile() with mocked broker + local state."""

    def _make_reconciler(self, tmp_path, monkeypatch, local_tickers=None, broker_tickers=None, recent_fills=None):
        """Build a StateReconciler with mocked _get_* methods."""
        monkeypatch.setattr("scripts.reconcile.PROJECT", tmp_path)
        reconciler = StateReconciler(MINIMAL_CONFIG, "sp500")

        # Build local ledger if requested (flat list of entry/exit events)
        if local_tickers:
            journal_dir = tmp_path / "journal"
            journal_dir.mkdir(exist_ok=True)
            ledger = [
                {
                    "type": "entry",
                    "ticker": t,
                    "strategy": "mock_strat",
                    "fill_price": 100.0,
                    "shares": 10,
                    "timestamp": "2026-03-01T10:00:00",
                }
                for t in local_tickers
            ]
            (journal_dir / "trade_ledger.json").write_text(json.dumps(ledger))

        # Mock broker calls (side_effect so report.broker_positions is set correctly)
        broker_pos = {
            t: {"qty": 10, "market_value": 1000, "avg_entry": 100, "unrealized_pl": 0, "side": "long"}
            for t in (broker_tickers or [])
        }

        def _mock_broker_positions():
            reconciler.report.broker_positions = len(broker_pos)
            return broker_pos

        reconciler._get_broker_positions = _mock_broker_positions
        reconciler._get_recent_fills = MagicMock(return_value=recent_fills or [])
        # Disable stale plan check for clean tests
        reconciler._check_stale_plans = MagicMock()
        return reconciler

    def test_clean_when_both_sides_match(self, tmp_path, monkeypatch):
        r = self._make_reconciler(
            tmp_path, monkeypatch,
            local_tickers=["AAPL", "MSFT"],
            broker_tickers=["AAPL", "MSFT"],
        )
        report = r.reconcile()
        assert report.clean is True
        assert len(report.discrepancies) == 0

    def test_clean_when_both_sides_empty(self, tmp_path, monkeypatch):
        r = self._make_reconciler(tmp_path, monkeypatch)
        report = r.reconcile()
        assert report.clean is True

    def test_missing_local_discrepancy(self, tmp_path, monkeypatch):
        """Broker has NVDA, local does not → missing_local."""
        r = self._make_reconciler(
            tmp_path, monkeypatch,
            local_tickers=[],
            broker_tickers=["NVDA"],
        )
        report = r.reconcile()
        assert not report.clean
        assert len(report.discrepancies) == 1
        d = report.discrepancies[0]
        assert d.category == "missing_local"
        assert d.ticker == "NVDA"
        assert d.severity == "high"
        assert d.auto_fixable is False  # No atlas_fill in recent_fills

    def test_missing_broker_discrepancy(self, tmp_path, monkeypatch):
        """Local has AAPL, broker doesn't, no recent fill → missing_broker."""
        r = self._make_reconciler(
            tmp_path, monkeypatch,
            local_tickers=["AAPL"],
            broker_tickers=[],
            recent_fills=[],
        )
        report = r.reconcile()
        assert not report.clean
        assert len(report.discrepancies) == 1
        d = report.discrepancies[0]
        assert d.category == "missing_broker"
        assert d.ticker == "AAPL"
        assert d.auto_fixable is False

    def test_sl_filled_discrepancy(self, tmp_path, monkeypatch):
        """Local has TSLA, broker doesn't, but a sell fill exists → sl_filled."""
        recent_fills = [
            {"ticker": "TSLA", "side": "sell", "qty": 5, "fill_price": 200.0, "filled_at": "", "order_type": "stop"}
        ]
        r = self._make_reconciler(
            tmp_path, monkeypatch,
            local_tickers=["TSLA"],
            broker_tickers=[],
            recent_fills=recent_fills,
        )
        report = r.reconcile()
        assert not report.clean
        assert len(report.discrepancies) == 1
        d = report.discrepancies[0]
        assert d.category == "sl_filled"
        assert d.ticker == "TSLA"
        assert d.auto_fixable is True

    def test_multiple_discrepancies(self, tmp_path, monkeypatch):
        """Multiple mismatches are all reported."""
        r = self._make_reconciler(
            tmp_path, monkeypatch,
            local_tickers=["AAPL", "GOOG"],
            broker_tickers=["AAPL", "MSFT"],  # GOOG missing on broker, MSFT missing locally
        )
        report = r.reconcile()
        assert not report.clean
        categories = {d.category for d in report.discrepancies}
        assert "missing_local" in categories   # MSFT
        assert "missing_broker" in categories  # GOOG

    def test_sets_broker_and_local_position_counts(self, tmp_path, monkeypatch):
        r = self._make_reconciler(
            tmp_path, monkeypatch,
            local_tickers=["AAPL"],
            broker_tickers=["AAPL", "NVDA"],
        )
        report = r.reconcile()
        assert report.local_positions == 1
        assert report.broker_positions == 2


# ─────────────────────────────────────────────
# StateReconciler.auto_fix()
# ─────────────────────────────────────────────

class TestAutoFix:
    def test_auto_fix_sl_filled(self, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.reconcile.PROJECT", tmp_path)
        # Create ledger with open TSLA position
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        ledger = [
            {
                "type": "entry",
                "ticker": "TSLA",
                "strategy": "momentum_breakout",
                "fill_price": 250.0,
                "shares": 10,
                "timestamp": "2026-03-01T10:00:00",
            }
        ]
        (journal_dir / "trade_ledger.json").write_text(json.dumps(ledger))
        
        reconciler = StateReconciler(MINIMAL_CONFIG, "sp500")
        # Mock recent fills with a sell order for TSLA
        reconciler._get_recent_fills = MagicMock(return_value=[
            {
                "ticker": "TSLA",
                "side": "sell",
                "qty": 10,
                "fill_price": 240.0,
                "filled_at": "2026-03-05T14:00:00",
                "order_type": "stop",
                "order_id": "test_order_123",
            }
        ])
        
        disc = Discrepancy(
            category="sl_filled",
            ticker="TSLA",
            description="SL filled",
            severity="high",
            auto_fixable=True,
            fix_action="Mark closed",
        )
        reconciler.report.discrepancies = [disc]
        fixes = reconciler.auto_fix()
        assert len(fixes) == 1
        assert "TSLA" in fixes[0]
        assert disc.fixed is True
        assert reconciler.report.fixes_applied == fixes

    def test_auto_fix_missing_local(self, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.reconcile.PROJECT", tmp_path)
        # Create empty ledger
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        (journal_dir / "trade_ledger.json").write_text("[]")
        
        reconciler = StateReconciler(MINIMAL_CONFIG, "sp500")
        # Mock broker position for NVDA
        reconciler._get_broker_positions = MagicMock(return_value={
            "NVDA": {
                "qty": 5,
                "market_value": 2500.0,
                "avg_entry": 500.0,
                "unrealized_pl": 0,
                "side": "long",
            }
        })
        # Mock recent fills with an Atlas buy order for NVDA
        reconciler._get_recent_fills = MagicMock(return_value=[
            {
                "ticker": "NVDA",
                "side": "buy",
                "qty": 5,
                "fill_price": 500.0,
                "filled_at": "2026-03-02T10:00:00",
                "order_type": "limit",
                "order_id": "test_order_nvda_123",
                "client_order_id": "atlas_sp500_momentum_breakout_NVDA_123",
                "is_atlas": True,
            }
        ])
        
        disc = Discrepancy(
            category="missing_local",
            ticker="NVDA",
            description="Missing locally",
            severity="high",
            auto_fixable=True,
            fix_action="Add tracking",
        )
        reconciler.report.discrepancies = [disc]
        fixes = reconciler.auto_fix()
        assert len(fixes) == 1
        assert "NVDA" in fixes[0]
        assert disc.fixed is True

    def test_auto_fix_skips_non_fixable(self, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.reconcile.PROJECT", tmp_path)
        reconciler = StateReconciler(MINIMAL_CONFIG, "sp500")
        disc = Discrepancy(
            category="missing_broker",
            ticker="GOOG",
            description="Manual needed",
            severity="high",
            auto_fixable=False,
        )
        reconciler.report.discrepancies = [disc]
        fixes = reconciler.auto_fix()
        assert fixes == []
        assert disc.fixed is False

    def test_auto_fix_idempotent(self, tmp_path, monkeypatch):
        """Calling auto_fix twice doesn't double-apply."""
        monkeypatch.setattr("scripts.reconcile.PROJECT", tmp_path)
        # Create ledger with open AAPL position
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        ledger = [
            {
                "type": "entry",
                "ticker": "AAPL",
                "strategy": "trend_following",
                "fill_price": 175.0,
                "shares": 8,
                "timestamp": "2026-03-01T10:00:00",
            }
        ]
        (journal_dir / "trade_ledger.json").write_text(json.dumps(ledger))
        
        reconciler = StateReconciler(MINIMAL_CONFIG, "sp500")
        # Mock recent fills with a sell order for AAPL
        reconciler._get_recent_fills = MagicMock(return_value=[
            {
                "ticker": "AAPL",
                "side": "sell",
                "qty": 8,
                "fill_price": 180.0,
                "filled_at": "2026-03-05T14:00:00",
                "order_type": "stop",
                "order_id": "test_order_456",
            }
        ])
        
        disc = Discrepancy(
            category="sl_filled",
            ticker="AAPL",
            description="x",
            severity="high",
            auto_fixable=True,
        )
        reconciler.report.discrepancies = [disc]
        fixes1 = reconciler.auto_fix()
        fixes2 = reconciler.auto_fix()
        assert len(fixes1) == 1
        assert len(fixes2) == 0  # Already fixed, not re-applied

    def test_auto_fix_mixed_fixable(self, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.reconcile.PROJECT", tmp_path)
        # Create ledger with AAPL and GOOG entries
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        ledger = [
            {
                "type": "entry",
                "ticker": "AAPL",
                "strategy": "momentum_breakout",
                "fill_price": 175.0,
                "shares": 10,
                "timestamp": "2026-03-01T10:00:00",
            },
            {
                "type": "entry",
                "ticker": "GOOG",
                "strategy": "trend_following",
                "fill_price": 140.0,
                "shares": 5,
                "timestamp": "2026-03-01T11:00:00",
            },
        ]
        (journal_dir / "trade_ledger.json").write_text(json.dumps(ledger))
        
        reconciler = StateReconciler(MINIMAL_CONFIG, "sp500")
        # Mock recent fills with a sell order for AAPL and a buy order for NVDA
        reconciler._get_recent_fills = MagicMock(return_value=[
            {
                "ticker": "AAPL",
                "side": "sell",
                "qty": 10,
                "fill_price": 180.0,
                "filled_at": "2026-03-05T14:00:00",
                "order_type": "stop",
                "order_id": "test_order_789",
            },
            {
                "ticker": "NVDA",
                "side": "buy",
                "qty": 7,
                "fill_price": 500.0,
                "filled_at": "2026-03-02T10:00:00",
                "order_type": "limit",
                "order_id": "test_order_nvda_789",
                "client_order_id": "atlas_sp500_trend_following_NVDA_789",
                "is_atlas": True,
            }
        ])
        # Mock broker position for NVDA (missing_local case)
        reconciler._get_broker_positions = MagicMock(return_value={
            "NVDA": {
                "qty": 7,
                "market_value": 3500.0,
                "avg_entry": 500.0,
                "unrealized_pl": 0,
                "side": "long",
            }
        })
        
        reconciler.report.discrepancies = [
            Discrepancy(category="sl_filled", ticker="AAPL", description="x", severity="high", auto_fixable=True),
            Discrepancy(category="missing_broker", ticker="GOOG", description="y", severity="high", auto_fixable=False),
            Discrepancy(category="missing_local", ticker="NVDA", description="z", severity="high", auto_fixable=True),
        ]
        fixes = reconciler.auto_fix()
        assert len(fixes) == 2
        fixed_tickers = {d.ticker for d in reconciler.report.discrepancies if d.fixed}
        assert "AAPL" in fixed_tickers
        assert "NVDA" in fixed_tickers
        assert "GOOG" not in fixed_tickers


# ─────────────────────────────────────────────
# format_telegram_message()
# ─────────────────────────────────────────────

class TestFormatTelegramMessage:
    def test_clean_report_message(self):
        r = _make_report()
        r.broker_positions = 3
        r.local_positions = 3
        r.broker_equity = 12345.67
        reconciler = StateReconciler(MINIMAL_CONFIG, "sp500")
        reconciler.report = r
        msg = reconciler.format_telegram_message()
        assert "✅" in msg
        assert "Reconciliation Clean" in msg
        assert "SP500" in msg
        assert "12,345.67" in msg
        assert "No discrepancies" in msg

    def test_dirty_report_message(self):
        d = Discrepancy(
            category="missing_local",
            ticker="AAPL",
            description="not tracked locally",
            severity="high",
        )
        r = _make_report([d])
        r.broker_positions = 1
        r.local_positions = 0
        reconciler = StateReconciler(MINIMAL_CONFIG, "sp500")
        reconciler.report = r
        msg = reconciler.format_telegram_message()
        assert "⚠️" in msg
        assert "Reconciliation Report" in msg
        assert "AAPL" in msg
        assert "Discrepancies: 1" in msg
        assert "🔴" in msg  # high severity icon

    def test_medium_severity_icon(self):
        d = Discrepancy(
            category="stale_plan",
            ticker="(plan)",
            description="old plan",
            severity="medium",
        )
        r = _make_report([d])
        reconciler = StateReconciler(MINIMAL_CONFIG, "sp500")
        reconciler.report = r
        msg = reconciler.format_telegram_message()
        assert "🟡" in msg

    def test_fixed_discrepancy_shows_checkmark(self):
        d = Discrepancy(
            category="sl_filled",
            ticker="TSLA",
            description="SL filled",
            severity="high",
            auto_fixable=True,
            fixed=True,
        )
        r = _make_report([d])
        reconciler = StateReconciler(MINIMAL_CONFIG, "sp500")
        reconciler.report = r
        msg = reconciler.format_telegram_message()
        assert "✅" in msg  # fixed indicator on the discrepancy line

    def test_fixes_applied_listed(self):
        r = _make_report()
        r.discrepancies = [
            Discrepancy(category="sl_filled", ticker="AAPL", description="x", severity="high")
        ]
        r.fixes_applied = ["Marked AAPL closed (SL filled during outage)"]
        reconciler = StateReconciler(MINIMAL_CONFIG, "sp500")
        reconciler.report = r
        msg = reconciler.format_telegram_message()
        assert "Fixes Applied" in msg
        assert "AAPL" in msg

    def test_market_id_uppercased(self):
        r = ReconciliationReport(timestamp="2026-01-01T00:00:00", market_id="asx")
        reconciler = StateReconciler(MINIMAL_CONFIG, "asx")
        reconciler.report = r
        msg = reconciler.format_telegram_message()
        assert "ASX" in msg

    def test_html_formatting_present(self):
        r = _make_report()
        reconciler = StateReconciler(MINIMAL_CONFIG, "sp500")
        reconciler.report = r
        msg = reconciler.format_telegram_message()
        assert "<b>" in msg
        assert "<i>" in msg
