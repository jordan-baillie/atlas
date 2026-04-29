"""Tests for scripts/migrate_to_oco.py

All broker interaction is mocked — zero live API calls.
"""
from __future__ import annotations

import sys
import types
import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ── Ensure project root is on path ─────────────────────────────────────────
PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))


# ── Load the module under test ─────────────────────────────────────────────
import importlib.util

def _load_migrate():
    spec = importlib.util.spec_from_file_location(
        "migrate_to_oco",
        PROJECT / "scripts" / "migrate_to_oco.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


migrate_mod = _load_migrate()


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_order(
    order_type: str,
    side: str,
    qty: int,
    stop_price: str = "",
    limit_price: str = "",
    order_class: str = "",
    order_id: str = "abc12345-0000-0000-0000-000000000000",
    symbol: str = "GLD",
) -> MagicMock:
    """Build a mock OrderResult with a realistic .raw dict."""
    o = MagicMock()
    o.ticker = symbol
    o.raw = {
        "id": order_id,
        "symbol": symbol,
        "order_type": order_type,
        "order_class": order_class,
        "side": side,
        "qty": str(qty),
        "stop_price": stop_price,
        "limit_price": limit_price,
        "status": "new",
    }
    return o


def _make_position(ticker: str, qty: int, entry: float = 400.0, mv: float = 420.0) -> MagicMock:
    pos = MagicMock()
    pos.ticker = ticker
    pos.shares = qty
    pos.entry_price = entry
    pos.market_value = mv
    return pos


def _make_broker(
    ticker: str = "GLD",
    pos_qty: int = 2,
    stop_qty: int = 2,
    tp_qty: int = 2,
    stop_price: str = "420.66",
    tp_price: str = "450.00",
    trailing: bool = False,
    already_bracket: bool = False,
    no_orders: bool = False,
) -> MagicMock:
    """Build a mock broker with configurable state."""
    broker = MagicMock()
    broker.connect.return_value = True

    pos = _make_position(ticker, pos_qty)
    broker.get_positions.return_value = [pos]

    if no_orders:
        broker.get_open_orders.return_value = []
    elif already_bracket:
        oco = _make_order("limit", "sell", pos_qty, limit_price=tp_price,
                          order_class="oco", symbol=ticker)
        stop_leg = _make_order("stop", "sell", pos_qty, stop_price=stop_price,
                               order_class="oco",
                               order_id="stop5678-0000-0000-0000-000000000000",
                               symbol=ticker)
        broker.get_open_orders.return_value = [oco, stop_leg]
    elif trailing:
        trail = _make_order("trailing_stop", "sell", pos_qty,
                            order_id="trail000-0000-0000-0000-000000000000",
                            symbol=ticker)
        broker.get_open_orders.return_value = [trail]
    else:
        stop_o = _make_order(
            "stop", "sell", stop_qty,
            stop_price=stop_price,
            order_id="stopord1-0000-0000-0000-000000000001",
            symbol=ticker,
        )
        tp_o = _make_order(
            "limit", "sell", tp_qty,
            limit_price=tp_price,
            order_id="tporder1-0000-0000-0000-000000000002",
            symbol=ticker,
        )
        broker.get_open_orders.return_value = [stop_o, tp_o]

    # cancel_order: success by default
    cancel_result = MagicMock()
    cancel_result.success = True
    cancel_result.message = "Cancelled"
    broker.cancel_order.return_value = cancel_result

    # _wait_for_cancel_confirmed: True by default
    broker._wait_for_cancel_confirmed.return_value = True

    # submit_order response via _broker_call
    oco_order = MagicMock()
    oco_order.id = "newoco00-0000-0000-0000-999999999999"
    broker._broker_call.return_value = oco_order

    # post-state get_open_orders: include oco-class stop + limit legs
    post_stop = _make_order("stop", "sell", pos_qty, stop_price=stop_price,
                            order_class="oco", symbol=ticker,
                            order_id="postst00-0000-0000-0000-000000000001")
    post_tp = _make_order("limit", "sell", pos_qty, limit_price=tp_price,
                          order_class="oco", symbol=ticker,
                          order_id="posttp00-0000-0000-0000-000000000002")
    # First call returns pre-state; second call (after sleep) returns post-state
    broker.get_open_orders.side_effect = [
        [
            _make_order("stop", "sell", stop_qty, stop_price=stop_price,
                        order_id="stopord1-0000-0000-0000-000000000001", symbol=ticker),
            _make_order("limit", "sell", tp_qty, limit_price=tp_price,
                        order_id="tporder1-0000-0000-0000-000000000002", symbol=ticker),
        ],
        # Post-place call (after sleep 2s)
        [post_stop, post_tp],
    ]

    return broker


# ══════════════════════════════════════════════════════════════════════════════
# Test: Reconnaissance mode
# ══════════════════════════════════════════════════════════════════════════════

class TestReconnaissance:
    """Test --reconnaissance mode (read-only, no mutation)."""

    def test_reconnaisance_prints_expected_state(self, capsys):
        """Recon should print ticker, qty, entry, mv, and order types."""
        broker = MagicMock()
        pos = _make_position("GLD", 2, entry=400.0, mv=420.0)
        broker.get_positions.return_value = [pos, _make_position("CAT", 1, entry=800.0)]
        stop_o = _make_order("stop", "sell", 2, stop_price="420.66", symbol="GLD",
                             order_id="stopord1-0000-0000-0000-000000000001")
        tp_o = _make_order("limit", "sell", 2, limit_price="450.00", symbol="GLD",
                           order_id="tporder1-0000-0000-0000-000000000002")
        cat_stop = _make_order("stop", "sell", 1, stop_price="799.47", symbol="CAT",
                               order_id="catstop1-0000-0000-0000-000000000003")
        cat_tp = _make_order("limit", "sell", 1, limit_price="850.00", symbol="CAT",
                             order_id="cattp001-0000-0000-0000-000000000004")
        broker.get_open_orders.return_value = [stop_o, tp_o, cat_stop, cat_tp]
        with patch.object(migrate_mod, "connect_broker", return_value=broker):
            results = migrate_mod.run_reconnaissance()

        out = capsys.readouterr().out
        assert "GLD" in out
        assert "qty=2" in out
        assert "entry=$400.00" in out
        assert "[stop]" in out
        assert "[limit]" in out
        assert "NEEDS MIGRATION" in out

    def test_reconnaisance_skips_already_bracket(self, capsys):
        """If order_class=oco present, warn SKIP (already migrated)."""
        broker = MagicMock()
        pos = _make_position("GLD", 2)
        broker.get_positions.return_value = [pos]
        oco_o = _make_order("limit", "sell", 2, limit_price="450.00",
                             order_class="oco", symbol="GLD")
        stop_leg = _make_order("stop", "sell", 2, stop_price="420.66",
                               order_class="oco", symbol="GLD",
                               order_id="stopleg1-0000-0000-0000-000000000001")
        broker.get_open_orders.return_value = [oco_o, stop_leg]
        with patch.object(migrate_mod, "connect_broker", return_value=broker):
            results = migrate_mod.run_reconnaissance()
        out = capsys.readouterr().out
        assert "ALREADY HAS bracket/oco" in out
        gld_info = results.get("GLD", {})
        assert not gld_info.get("needs_migration"), "Already-bracket should not need migration"

    def test_reconnaisance_warns_trailing_stop(self, capsys):
        """Trailing stop positions should warn SKIP."""
        broker = MagicMock()
        pos = _make_position("GLD", 2)
        broker.get_positions.return_value = [pos]
        trail = _make_order("trailing_stop", "sell", 2, symbol="GLD")
        broker.get_open_orders.return_value = [trail]
        with patch.object(migrate_mod, "connect_broker", return_value=broker):
            results = migrate_mod.run_reconnaissance()
        out = capsys.readouterr().out
        assert "HAS TRAILING STOP" in out or "trailing_stop" in out.lower()
        gld_info = results.get("GLD", {})
        assert not gld_info.get("needs_migration"), "Trailing stop should not need_migration"

    def test_reconnaisance_warns_qty_mismatch(self, capsys):
        """Qty mismatch between position and orders should warn."""
        broker = MagicMock()
        pos = _make_position("GLD", 3)  # position has 3
        broker.get_positions.return_value = [pos]
        stop_o = _make_order("stop", "sell", 2, stop_price="420.66", symbol="GLD")  # stop has 2
        tp_o = _make_order("limit", "sell", 2, limit_price="450.00", symbol="GLD",
                           order_id="tporder1-0000-0000-0000-000000000002")
        broker.get_open_orders.return_value = [stop_o, tp_o]
        with patch.object(migrate_mod, "connect_broker", return_value=broker):
            results = migrate_mod.run_reconnaissance()
        out = capsys.readouterr().out
        assert "QTY MISMATCH" in out or "MISMATCH" in out
        gld_info = results.get("GLD", {})
        assert not gld_info.get("needs_migration"), "Qty mismatch should not allow migration"


# ══════════════════════════════════════════════════════════════════════════════
# Test: migrate_ticker happy path
# ══════════════════════════════════════════════════════════════════════════════

class TestMigrateTickerHappyPath:
    """Test the full happy-path migration flow."""

    def test_happy_path_calls_cancel_and_place(self, capsys):
        """Happy path: cancel stop → cancel TP → place OCO with correct prices."""
        import time as time_mod

        broker = _make_broker(ticker="GLD", pos_qty=2, stop_price="420.66", tp_price="450.00")

        with patch.object(time_mod, "sleep"):  # skip the 2s sleep
            migrate_mod.migrate_ticker(broker, "GLD")

        out = capsys.readouterr().out

        # Both cancel calls made
        assert broker.cancel_order.call_count == 2
        # Both _wait_for_cancel_confirmed calls made
        assert broker._wait_for_cancel_confirmed.call_count == 2
        # OCO placement called via _broker_call
        assert broker._broker_call.call_count == 1

        # Check the LimitOrderRequest was built with correct prices
        call_args = broker._broker_call.call_args
        request = call_args[0][1]  # second positional arg to _broker_call is the request
        assert round(request.limit_price, 2) == 450.00
        assert round(request.stop_loss.stop_price, 2) == 420.66
        assert request.qty == 2

        assert "MIGRATED SUCCESSFULLY" in out

    def test_happy_path_verifies_prices_before_cancel(self, capsys):
        """Prices must be snapshotted BEFORE cancel is called."""
        import time as time_mod

        broker = _make_broker(ticker="GLD", pos_qty=1, stop_qty=1, tp_qty=1, stop_price="421.00", tp_price="460.00")
        call_order = []

        original_cancel = broker.cancel_order.side_effect
        def tracking_cancel(order_id):
            call_order.append(("cancel", order_id))
            result = MagicMock()
            result.success = True
            return result
        broker.cancel_order.side_effect = tracking_cancel

        with patch.object(time_mod, "sleep"):
            migrate_mod.migrate_ticker(broker, "GLD")

        # Cancels happened; OCO prices used correct values
        assert broker._broker_call.call_count == 1
        req = broker._broker_call.call_args[0][1]
        assert abs(req.limit_price - 460.00) < 0.01
        assert abs(req.stop_loss.stop_price - 421.00) < 0.01


# ══════════════════════════════════════════════════════════════════════════════
# Test: Abort on cancel timeout
# ══════════════════════════════════════════════════════════════════════════════

class TestCancelTimeout:
    """Abort if cancel is not confirmed — must NOT place OCO."""

    def test_stop_cancel_timeout_aborts_no_place(self, capsys):
        """If stop cancel times out, abort immediately — do NOT cancel TP or place OCO."""
        import time as time_mod

        broker = _make_broker(ticker="GLD")
        # First _wait_for_cancel_confirmed (for stop) → timeout (False)
        broker._wait_for_cancel_confirmed.side_effect = [False]

        with patch.object(time_mod, "sleep"):
            with pytest.raises(SystemExit) as exc_info:
                migrate_mod.migrate_ticker(broker, "GLD")

        msg = str(exc_info.value)
        assert "ABORT" in msg
        assert "cancel NOT confirmed" in msg or "stop cancel NOT confirmed" in msg

        # OCO must NOT have been placed
        assert broker._broker_call.call_count == 0
        # Only ONE cancel_order call (for stop) — TP should not have been touched
        assert broker.cancel_order.call_count == 1

    def test_tp_cancel_timeout_aborts_no_place(self, capsys):
        """If TP cancel times out after stop is already cancelled — abort, report unprotected."""
        import time as time_mod

        broker = _make_broker(ticker="GLD")
        # First _wait_for_cancel_confirmed (stop) → True; second (TP) → timeout (False)
        broker._wait_for_cancel_confirmed.side_effect = [True, False]

        with patch.object(time_mod, "sleep"):
            with pytest.raises(SystemExit) as exc_info:
                migrate_mod.migrate_ticker(broker, "GLD")

        msg = str(exc_info.value)
        assert "ABORT" in msg
        # Should warn about unprotected state
        assert "UNPROTECTED" in msg or "CRITICAL" in msg or "stop" in msg.lower()

        # OCO must NOT have been placed
        assert broker._broker_call.call_count == 0


# ══════════════════════════════════════════════════════════════════════════════
# Test: Skip trailing_stop ticker
# ══════════════════════════════════════════════════════════════════════════════

class TestTrailingStopSkip:
    """Trailing stop tickers must be skipped — no cancel, no place."""

    def test_trailing_stop_no_cancel_called(self, capsys):
        """Ticker with trailing_stop → SKIP without calling cancel_order."""
        import time as time_mod

        broker = _make_broker(ticker="GLD", trailing=True)
        # Adjust side_effect: only one get_open_orders call since we skip early
        trail = _make_order("trailing_stop", "sell", 2, symbol="GLD")
        broker.get_open_orders.side_effect = None
        broker.get_open_orders.return_value = [trail]

        with patch.object(time_mod, "sleep"):
            migrate_mod.migrate_ticker(broker, "GLD")  # should return normally (skip)

        out = capsys.readouterr().out
        assert "SKIP" in out
        assert "trailing_stop" in out.lower() or "trailing" in out.lower()

        # No cancellation
        assert broker.cancel_order.call_count == 0
        # No OCO placement
        assert broker._broker_call.call_count == 0


# ══════════════════════════════════════════════════════════════════════════════
# Test: Skip already-bracket ticker
# ══════════════════════════════════════════════════════════════════════════════

class TestAlreadyBracketSkip:
    """Tickers with order_class=bracket/oco must be skipped."""

    def test_already_oco_no_cancel_called(self, capsys):
        """order_class=oco on existing orders → SKIP without calling cancel."""
        import time as time_mod

        broker = _make_broker(ticker="GLD", already_bracket=True)
        broker.get_open_orders.side_effect = None
        oco_o = _make_order("limit", "sell", 2, limit_price="450.00",
                             order_class="oco", symbol="GLD")
        stop_leg = _make_order("stop", "sell", 2, stop_price="420.66",
                               order_class="oco", symbol="GLD",
                               order_id="stopleg1-0000-0000-0000-000000000001")
        broker.get_open_orders.return_value = [oco_o, stop_leg]

        with patch.object(time_mod, "sleep"):
            migrate_mod.migrate_ticker(broker, "GLD")  # should return (skip)

        out = capsys.readouterr().out
        assert "SKIP" in out
        assert "bracket" in out.lower() or "oco" in out.lower()

        assert broker.cancel_order.call_count == 0
        assert broker._broker_call.call_count == 0


# ══════════════════════════════════════════════════════════════════════════════
# Test: Abort on partial coverage (qty mismatch)
# ══════════════════════════════════════════════════════════════════════════════

class TestQtyMismatchAbort:
    """Qty mismatch between position and protective orders → abort before cancel."""

    def test_stop_qty_mismatch_aborts_no_cancel(self, capsys):
        """Stop order qty != position qty → abort, do NOT call cancel_order."""
        import time as time_mod

        broker = _make_broker(ticker="GLD", pos_qty=3, stop_qty=2, tp_qty=3)
        broker.get_open_orders.side_effect = None
        stop_o = _make_order("stop", "sell", 2, stop_price="420.66", symbol="GLD",
                             order_id="stopord1-0000-0000-0000-000000000001")
        tp_o = _make_order("limit", "sell", 3, limit_price="450.00", symbol="GLD",
                           order_id="tporder1-0000-0000-0000-000000000002")
        broker.get_open_orders.return_value = [stop_o, tp_o]

        with patch.object(time_mod, "sleep"):
            with pytest.raises(SystemExit) as exc_info:
                migrate_mod.migrate_ticker(broker, "GLD")

        msg = str(exc_info.value)
        assert "ABORT" in msg
        assert "partial" in msg.lower() or "qty" in msg.lower() or "mismatch" in msg.lower()

        # No cancellation
        assert broker.cancel_order.call_count == 0
        # No OCO
        assert broker._broker_call.call_count == 0

    def test_tp_qty_mismatch_aborts_no_cancel(self, capsys):
        """TP order qty != position qty → abort before any cancel."""
        import time as time_mod

        broker = _make_broker(ticker="GLD", pos_qty=3, stop_qty=3, tp_qty=2)
        broker.get_open_orders.side_effect = None
        stop_o = _make_order("stop", "sell", 3, stop_price="420.66", symbol="GLD",
                             order_id="stopord1-0000-0000-0000-000000000001")
        tp_o = _make_order("limit", "sell", 2, limit_price="450.00", symbol="GLD",
                           order_id="tporder1-0000-0000-0000-000000000002")
        broker.get_open_orders.return_value = [stop_o, tp_o]

        with patch.object(time_mod, "sleep"):
            with pytest.raises(SystemExit) as exc_info:
                migrate_mod.migrate_ticker(broker, "GLD")

        msg = str(exc_info.value)
        assert "ABORT" in msg
        assert broker.cancel_order.call_count == 0
        assert broker._broker_call.call_count == 0


# ══════════════════════════════════════════════════════════════════════════════
# Test: CLI argparse
# ══════════════════════════════════════════════════════════════════════════════

class TestCLI:
    """Test CLI argument handling."""

    def test_ticker_without_execute_is_dry_run(self, capsys):
        """--ticker without --execute should print DRY-RUN and exit 0."""
        rc = migrate_mod.main(["--ticker", "GLD"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "DRY-RUN" in out or "dry" in out.lower()

    def test_invalid_ticker_rejected(self):
        """Invalid ticker should be rejected by argparse."""
        with pytest.raises(SystemExit) as exc_info:
            migrate_mod.main(["--ticker", "AAPL", "--execute"])
        # argparse exits with code 2 for invalid choice
        assert exc_info.value.code != 0

    def test_reconnaisance_and_ticker_mutually_exclusive(self, capsys):
        """--reconnaissance and --ticker together should print error and exit."""
        rc = migrate_mod.main(["--reconnaissance", "--ticker", "GLD"])
        assert rc != 0
        out = capsys.readouterr().out
        assert "mutually exclusive" in out or "ERROR" in out

    def test_no_args_prints_help(self, capsys):
        """No arguments should print help and exit non-zero."""
        # main([]) calls parser.print_help() and returns 1
        rc = migrate_mod.main([])
        out = capsys.readouterr().out
        assert rc != 0


