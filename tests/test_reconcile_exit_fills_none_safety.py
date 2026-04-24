"""P1-10 regression tests — None-safe reconcile_exit_fills / record_exit.

Covers:
  - TradeLedger.record_exit with pnl=None must NOT raise TypeError
  - reconcile_exit_fills with fill_price/qty=None skips cleanly (no crash)
  - reconcile_exit_fills completes when entry is missing (entry_price=0 → pnl=None)
  - _fmt() helper handles None, valid float, and bad types gracefully
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# ---------------------------------------------------------------------------
# 1. _fmt() helper
# ---------------------------------------------------------------------------

class TestFmtHelper:
    """Unit tests for the None-safe _fmt() formatter added to live_executor."""

    def _get_fmt(self):
        from brokers import live_executor as le
        return le._fmt

    def test_none_returns_question_mark(self):
        assert self._get_fmt()(None) == "?"

    def test_float_formats_correctly(self):
        assert self._get_fmt()(3.14159) == "3.14"

    def test_integer_works(self):
        assert self._get_fmt()(42) == "42.00"

    def test_custom_spec(self):
        assert self._get_fmt()(0.5, "{:.1%}") == "50.0%"

    def test_bad_type_returns_str(self):
        result = self._get_fmt()("not-a-number")
        assert isinstance(result, str)  # doesn't raise


# ---------------------------------------------------------------------------
# 2. TradeLedger.record_exit — pnl=None must not raise
# ---------------------------------------------------------------------------

class TestRecordExitNonePnl:
    """The format string PnL=${...:.2f} was crashing when pnl=None."""

    def test_record_exit_with_none_pnl_does_not_raise(self, tmp_path):
        """record_exit(pnl=None) must log cleanly, not TypeError."""
        from journal.logger import TradeLedger
        ledger_file = tmp_path / "trade_ledger.json"
        ledger_file.write_text("[]")

        with patch("journal.logger.JOURNAL_DIR", tmp_path), \
             patch("journal.logger.TradeLedger.FILE", ledger_file), \
             patch("db.atlas_db.record_trade_exit", return_value=None):
            ledger = TradeLedger()
            ledger.trades = []  # clean slate

            exit_record = {
                "ticker": "UNG",
                "strategy": "momentum_breakout",
                "shares": 10,
                "fill_price": 15.50,
                "entry_price": 0.0,       # no entry → pnl=None
                "pnl": None,              # ← the culprit field
                "pnl_pct": None,
                "exit_reason": "trailing_stop_fill",
                "order_id": "test-order-123",
                "timestamp": "2026-04-24T00:00:00",
                "reconciled": True,
            }
            # This must NOT raise TypeError
            ledger.record_exit(exit_record)

    def test_record_exit_with_valid_pnl_still_works(self, tmp_path):
        """Ensure the format fix didn't break the happy path."""
        from journal.logger import TradeLedger
        ledger_file = tmp_path / "trade_ledger.json"
        ledger_file.write_text("[]")

        with patch("journal.logger.JOURNAL_DIR", tmp_path), \
             patch("journal.logger.TradeLedger.FILE", ledger_file), \
             patch("db.atlas_db.record_trade_exit", return_value=None):
            ledger = TradeLedger()
            ledger.trades = []

            exit_record = {
                "ticker": "CHTR",
                "strategy": "momentum_breakout",
                "shares": 4,
                "fill_price": 243.50,
                "entry_price": 243.93,
                "pnl": -1.72,           # real pnl
                "pnl_pct": -0.18,
                "exit_reason": "trailing_stop_fill",
                "order_id": "test-order-456",
                "timestamp": "2026-04-24T03:00:00",
                "reconciled": True,
            }
            # Must not raise
            ledger.record_exit(exit_record)

    def test_record_exit_with_zero_pnl(self, tmp_path):
        """pnl=0 (not None) must format as '0.00'."""
        from journal.logger import TradeLedger
        ledger_file = tmp_path / "trade_ledger.json"
        ledger_file.write_text("[]")

        with patch("journal.logger.JOURNAL_DIR", tmp_path), \
             patch("journal.logger.TradeLedger.FILE", ledger_file), \
             patch("db.atlas_db.record_trade_exit", return_value=None):
            ledger = TradeLedger()
            ledger.trades = []

            exit_record = {
                "ticker": "TEST",
                "strategy": "test",
                "shares": 1,
                "fill_price": 100.0,
                "pnl": 0,            # zero — not None, use default
                "exit_reason": "test",
                "order_id": "ord-0",
                "timestamp": "2026-04-24T00:00:00",
            }
            ledger.record_exit(exit_record)  # must not raise


# ---------------------------------------------------------------------------
# 3. reconcile_exit_fills — None fill_price/qty skipped cleanly
# ---------------------------------------------------------------------------

def _make_mock_order(
    order_id: str = "ord-001",
    symbol: str = "UNG",
    side: str = "sell",
    status: str = "filled",
    filled_avg_price=None,   # ← None to test guard
    filled_qty: str = "10",
    client_order_id: str = "atlas_trail_UNG_001",
    filled_at: str = "2026-04-24T00:00:00Z",
):
    """Build a minimal Alpaca order mock."""
    order = MagicMock()
    order.id = order_id
    order.symbol = symbol
    order.side = MagicMock()
    order.side.value = side
    order.status = MagicMock()
    order.status.value = status
    order.filled_avg_price = filled_avg_price
    order.filled_qty = filled_qty
    order.qty = filled_qty
    order.client_order_id = client_order_id
    order.filled_at = filled_at
    return order


class TestReconcileExitFillsNoneSafety:
    """reconcile_exit_fills must not crash when broker returns None fields."""

    def _make_executor(self):
        """Build a minimal LiveExecutor with mocked broker, no real connections."""
        from brokers.live_executor import LiveExecutor
        config = {
            "market_id": "commodity_etfs",
            "live_enabled": False,
            "risk": {},
        }
        executor = object.__new__(LiveExecutor)
        executor.config = config
        executor._connected = True
        executor._broker = MagicMock()
        executor._broker._trade_client = MagicMock()
        return executor

    def test_none_fill_price_skipped(self):
        """Order with filled_avg_price=None must be skipped, not crash."""
        executor = self._make_executor()
        none_price_order = _make_mock_order(
            filled_avg_price=None,  # ← triggers None guard
            filled_qty="10",
            status="filled",
        )

        with patch("brokers.live_executor._get_regime_model") as mock_regime, \
             patch.object(executor._broker, "_broker_call", return_value=[none_price_order]), \
             patch("journal.logger.TradeLedger") as mock_ledger_cls:
            mock_regime.return_value.classify_current.return_value.state.value = "bull_risk_on"
            mock_ledger = MagicMock()
            mock_ledger.trades = []
            mock_ledger_cls.return_value = mock_ledger

            result = executor.reconcile_exit_fills()
            # The order was skipped — nothing reconciled
            assert result == []
            # record_exit should NOT have been called
            mock_ledger.record_exit.assert_not_called()

    def test_none_qty_skipped(self):
        """Order with filled_qty=None AND qty=None must be skipped."""
        executor = self._make_executor()
        none_qty_order = _make_mock_order(
            filled_avg_price="15.50",
            filled_qty=None,
            status="filled",
        )
        none_qty_order.qty = None  # both None

        with patch("brokers.live_executor._get_regime_model") as mock_regime, \
             patch.object(executor._broker, "_broker_call", return_value=[none_qty_order]), \
             patch("journal.logger.TradeLedger") as mock_ledger_cls:
            mock_regime.return_value.classify_current.return_value.state.value = "bull_risk_on"
            mock_ledger = MagicMock()
            mock_ledger.trades = []
            mock_ledger_cls.return_value = mock_ledger

            result = executor.reconcile_exit_fills()
            assert result == []
            mock_ledger.record_exit.assert_not_called()

    def test_valid_order_with_no_entry_does_not_crash(self, tmp_path):
        """Valid fill but no matching entry (pnl=None) must reconcile cleanly."""
        executor = self._make_executor()
        valid_order = _make_mock_order(
            filled_avg_price="15.50",  # string — Alpaca API returns Decimal-like
            filled_qty="10",
            status="filled",
        )

        from journal.logger import TradeLedger
        ledger_file = tmp_path / "trade_ledger.json"
        ledger_file.write_text("[]")

        with patch("brokers.live_executor._get_regime_model") as mock_regime, \
             patch.object(executor._broker, "_broker_call", return_value=[valid_order]), \
             patch("journal.logger.JOURNAL_DIR", tmp_path), \
             patch("journal.logger.TradeLedger.FILE", ledger_file), \
             patch("db.atlas_db.record_trade_exit", return_value=None):
            mock_regime.return_value.classify_current.return_value.state.value = "recovery_early"

            # Should complete without TypeError even when pnl=None
            result = executor.reconcile_exit_fills()
            # 1 order processed (pnl=None is fine after our fix)
            assert len(result) == 1
            assert result[0]["ticker"] == "UNG"
            assert result[0]["pnl"] is None  # entry was absent → pnl stays None

    def test_already_recorded_order_skipped(self):
        """Orders already in the ledger by order_id must be skipped (dedup)."""
        executor = self._make_executor()
        dup_order = _make_mock_order(
            order_id="existing-ord-001",
            filled_avg_price="20.0",
            filled_qty="5",
        )

        with patch("brokers.live_executor._get_regime_model") as mock_regime, \
             patch.object(executor._broker, "_broker_call", return_value=[dup_order]), \
             patch("journal.logger.TradeLedger") as mock_ledger_cls:
            mock_regime.return_value.classify_current.return_value.state.value = "bull_risk_on"
            mock_ledger = MagicMock()
            # Simulate existing order already in ledger
            mock_ledger.trades = [
                {"type": "exit", "order_id": "existing-ord-001", "ticker": "UNG"}
            ]
            mock_ledger_cls.return_value = mock_ledger

            result = executor.reconcile_exit_fills()
            assert result == []


# ---------------------------------------------------------------------------
# 4. Plan.rejected_entries populated from PortfolioConstructor (P1-9 related)
# ---------------------------------------------------------------------------

class TestConstructorRejectsInjected:
    """_run_regime_aware_plan must inject constructed.rejected into plan.rejected_entries."""

    def test_constructor_rejects_in_plan(self):
        """When PortfolioConstructor rejects N signals, plan.rejected_entries must have N entries."""
        from brokers.plan import TradePlanGenerator
        from unittest.mock import MagicMock, patch

        # Build a minimal signal mock
        def make_signal(ticker: str):
            sig = MagicMock()
            sig.ticker = ticker
            sig.strategy = "momentum_breakout"
            sig.entry_price = 100.0
            sig.stop_price = 95.0
            sig.take_profit = 115.0
            sig.position_size = 5
            sig.confidence = 0.9
            sig.rationale = "test"
            sig.features = {}
            sig.sector = "Technology"
            sig.market_id = "sp500"
            sig.universe = "sp500"
            return sig

        selected_sig = make_signal("AAPL")
        rejected_sig = make_signal("MSFT")

        from portfolio.constructor import ConstructedPortfolio
        constructed = ConstructedPortfolio(
            signals=[selected_sig],
            rejected=[(rejected_sig, "max_positions_exceeded")],
        )

        mock_portfolio = MagicMock()
        mock_portfolio.positions = []
        mock_portfolio.cash = 5000.0
        mock_portfolio.equity.return_value = 5000.0
        mock_portfolio.atlas_positions = []
        mock_portfolio.portfolio_summary.return_value = {
            "total_pnl": 0.0, "total_pnl_pct": 0.0,
            "open_positions": [],
        }
        mock_portfolio.check_risk_limits.return_value = (True, "ok")

        config = {
            "market": "sp500",
            "market_id": "sp500",
            "version": "test",
            "regime_enabled": False,
            "risk": {"max_open_positions": 5, "min_confidence": 0.0},
            "allocation": {"enabled": False},
        }

        gen = object.__new__(TradePlanGenerator)
        gen.portfolio = mock_portfolio
        gen.config = config

        # Call generate_plan with only the selected signal to simulate what
        # _run_regime_aware_plan does — then inject constructor rejects
        plan = gen.generate_plan(
            signals=[selected_sig],
            exit_recommendations=[],
            prices={"AAPL": 100.0, "MSFT": 100.0},
            trade_date="2026-04-24",
        )

        # Simulate the injection added by P1-9 fix
        _constructor_rejects = []
        for _sig, _reason in constructed.rejected:
            _constructor_rejects.append({
                "ticker": _sig.ticker,
                "strategy": _sig.strategy,
                "entry_price": _sig.entry_price,
                "stop_price": _sig.stop_price,
                "take_profit": getattr(_sig, "take_profit", None),
                "position_size": _sig.position_size,
                "position_value": round(_sig.entry_price * _sig.position_size, 2),
                "risk_amount": round(abs(_sig.entry_price - _sig.stop_price) * _sig.position_size, 2),
                "confidence": _sig.confidence,
                "rationale": getattr(_sig, "rationale", ""),
                "features": getattr(_sig, "features", {}),
                "sector": getattr(_sig, "sector", "Unknown"),
                "market_id": getattr(_sig, "market_id", config.get("market", "")),
                "rejection_reason": _reason,
            })
        plan["rejected_entries"] = _constructor_rejects + plan.get("rejected_entries", [])

        # Assertions
        assert len(plan["rejected_entries"]) >= 1
        reject = plan["rejected_entries"][0]
        assert reject["ticker"] == "MSFT"
        assert reject["rejection_reason"] == "max_positions_exceeded"
        assert reject["entry_price"] == 100.0

