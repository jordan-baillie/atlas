"""Tests for sync_protective_orders._apply_db_consistency (#274).

Verifies that the DB-consistency helper persists new operative stop/tp order
IDs to trades.stop_order_id / trades.tp_order_id after sync_all_protective_orders
places or replaces a protective order.

Tests use the autouse _isolate_prod_db fixture (conftest.py) so every test
runs against a fresh throw-away SQLite DB.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Project root on path
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from brokers.base import OrderResult, OrderSide, OrderStatus  # noqa: E402
from db import atlas_db as _adb  # noqa: E402
from db.atlas_db import get_db, init_db, record_trade_entry  # noqa: E402
from scripts.sync_protective_orders import (  # noqa: E402
    _DB_UPDATE_ACTIONS,
    _apply_db_consistency,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_order_result(
    ticker: str,
    order_type: str,
    order_id: str,
    side: OrderSide = OrderSide.SELL,
) -> OrderResult:
    """Minimal OrderResult as returned by broker.get_open_orders()."""
    return OrderResult(
        success=True,
        order_id=order_id,
        ticker=ticker,
        side=side,
        status=OrderStatus.PENDING,
        raw={"order_type": order_type},
    )


def _insert_open_trade(ticker: str, universe: str = "sp500") -> None:
    """Insert a minimal open trade row into the isolated test DB."""
    record_trade_entry(
        ticker=ticker,
        strategy="momentum_breakout",
        universe=universe,
        entry_price=100.0,
        shares=10,
        stop_price=90.0,
        take_profit=120.0,
        confidence=0.7,
        regime_state="bull_risk_on",
    )


def _query_trade(ticker: str, universe: str = "sp500") -> dict:
    """Return stop_order_id and tp_order_id for the open trade."""
    with get_db() as db:
        row = db.execute(
            "SELECT stop_order_id, tp_order_id FROM trades "
            "WHERE ticker=? AND universe=? AND status='open'",
            (ticker, universe),
        ).fetchone()
    if row is None:
        return {}
    return {"stop_order_id": row["stop_order_id"] or "", "tp_order_id": row["tp_order_id"] or ""}


def _make_broker(open_orders: list[OrderResult]) -> MagicMock:
    b = MagicMock()
    b.get_open_orders.return_value = open_orders
    return b


# ═══════════════════════════════════════════════════════════════
# 1. OCO placed — stop + tp IDs resolved from broker orders
# ═══════════════════════════════════════════════════════════════

class TestOcoPlaced:

    def test_updates_stop_and_tp_on_oco_placed(self) -> None:
        """oco_placed action → both stop_order_id and tp_order_id written."""
        _insert_open_trade("MU")

        orders = [
            _make_order_result("MU", "stop", "STOP-1"),
            _make_order_result("MU", "limit", "TP-1"),
        ]
        broker = _make_broker(orders)
        sync_result = {
            "per_ticker": {
                "MU": {"sl_action": "oco_placed", "tp_action": "oco_placed",
                       "oco_order_id": "PARENT-X"},
            }
        }

        _apply_db_consistency(broker, "sp500", sync_result)

        row = _query_trade("MU")
        assert row["stop_order_id"] == "STOP-1", f"stop_order_id wrong: {row}"
        assert row["tp_order_id"] == "TP-1", f"tp_order_id wrong: {row}"

    def test_updates_on_tightened_action(self) -> None:
        """tightened (cancel-and-replace) also persists new IDs."""
        _insert_open_trade("MU")

        orders = [
            _make_order_result("MU", "trailing_stop", "NEW-STOP"),
            _make_order_result("MU", "limit", "NEW-TP"),
        ]
        broker = _make_broker(orders)
        sync_result = {
            "per_ticker": {
                "MU": {"sl_action": "tightened", "tp_action": "tightened",
                       "oco_order_id": "PARENT-Y"},
            }
        }

        _apply_db_consistency(broker, "sp500", sync_result)

        row = _query_trade("MU")
        assert row["stop_order_id"] == "NEW-STOP"
        assert row["tp_order_id"] == "NEW-TP"


# ═══════════════════════════════════════════════════════════════
# 2. PDT fallback — SL-only (no TP leg)
# ═══════════════════════════════════════════════════════════════

class TestPdtFallback:

    def test_updates_only_stop_on_pdt_fallback(self) -> None:
        """placed_pdt_fallback: standalone STOP placed, no TP → stop_order_id only."""
        _insert_open_trade("AMD")

        # Only a standalone STOP order — no LIMIT leg
        orders = [
            _make_order_result("AMD", "stop", "STOP-FB"),
        ]
        broker = _make_broker(orders)
        sync_result = {
            "per_ticker": {
                "AMD": {"sl_action": "placed_pdt_fallback", "sl_order_id": "STOP-FB"},
            }
        }

        _apply_db_consistency(broker, "sp500", sync_result)

        row = _query_trade("AMD")
        assert row["stop_order_id"] == "STOP-FB"
        # tp_order_id was NULL → stays NULL (no LIMIT order in open orders)
        assert row["tp_order_id"] == ""


# ═══════════════════════════════════════════════════════════════
# 3. No-op actions — skipped / pdt_deferred
# ═══════════════════════════════════════════════════════════════

class TestNoopActions:

    def test_skipped_action_no_db_update(self) -> None:
        """sl_action='skipped' → idempotent, no DB write."""
        _insert_open_trade("AAPL")

        # Pre-condition: stop_order_id is empty
        pre = _query_trade("AAPL")
        assert pre["stop_order_id"] == ""

        broker = _make_broker([
            _make_order_result("AAPL", "stop", "OLD-STOP"),
        ])
        sync_result = {
            "per_ticker": {
                "AAPL": {"sl_action": "skipped"},
            }
        }

        _apply_db_consistency(broker, "sp500", sync_result)

        # Still empty — skipped means no new order was placed
        post = _query_trade("AAPL")
        assert post["stop_order_id"] == "", (
            "skipped action must NOT update DB (order ID unchanged)"
        )

    def test_pdt_deferred_no_db_update(self) -> None:
        """sl_action='pdt_deferred' → no broker order placed → no DB write."""
        _insert_open_trade("TSLA")

        broker = _make_broker([])  # No orders on broker
        sync_result = {
            "per_ticker": {
                "TSLA": {"sl_action": "pdt_deferred"},
            }
        }

        _apply_db_consistency(broker, "sp500", sync_result)

        post = _query_trade("TSLA")
        assert post["stop_order_id"] == ""
        assert post["tp_order_id"] == ""


# ═══════════════════════════════════════════════════════════════
# 4. No open trade in DB — logs warning, no exception
# ═══════════════════════════════════════════════════════════════

class TestNoOpenTrade:

    def test_no_open_trade_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """oco_placed but no open trade in DB → update_trade_protective_orders
        logs a WARNING about no matching row. No exception propagates."""
        # Do NOT insert any trade for NVDA

        orders = [
            _make_order_result("NVDA", "stop", "STOP-NVD"),
            _make_order_result("NVDA", "limit", "TP-NVD"),
        ]
        broker = _make_broker(orders)
        sync_result = {
            "per_ticker": {
                "NVDA": {"sl_action": "oco_placed"},
            }
        }

        import logging
        with caplog.at_level(logging.WARNING):
            _apply_db_consistency(broker, "sp500", sync_result)

        # Should warn about no matching trade row (from update_trade_protective_orders)
        assert any("no open trade" in r.message.lower() for r in caplog.records), (
            f"Expected a 'no open trade' warning; got: {[r.message for r in caplog.records]}"
        )


# ═══════════════════════════════════════════════════════════════
# 5. DB failure is non-fatal
# ═══════════════════════════════════════════════════════════════

class TestDbFailureNonFatal:

    def test_db_failure_does_not_propagate(self) -> None:
        """If update_trade_protective_orders raises, _apply_db_consistency
        logs a warning and returns normally (no exception to caller)."""
        _insert_open_trade("GS")

        orders = [
            _make_order_result("GS", "stop", "STOP-GS"),
        ]
        broker = _make_broker(orders)
        sync_result = {
            "per_ticker": {
                "GS": {"sl_action": "oco_placed"},
            }
        }

        with patch(
            "db.atlas_db.update_trade_protective_orders",
            side_effect=RuntimeError("DB exploded"),
        ):
            # Must not raise — the helper swallows DB errors
            result = _apply_db_consistency(broker, "sp500", sync_result)

        # Returns None (implicitly) — the caller's result dict is unaffected
        assert result is None

    def test_broker_get_open_orders_failure_is_non_fatal(self) -> None:
        """If broker.get_open_orders raises, _apply_db_consistency swallows it."""
        broker = MagicMock()
        broker.get_open_orders.side_effect = ConnectionError("broker down")

        sync_result = {
            "per_ticker": {
                "MSFT": {"sl_action": "oco_placed"},
            }
        }

        # Must not raise
        result = _apply_db_consistency(broker, "sp500", sync_result)
        assert result is None


# ═══════════════════════════════════════════════════════════════
# 6. _DB_UPDATE_ACTIONS constant correctness
# ═══════════════════════════════════════════════════════════════

class TestActionSet:

    def test_action_set_contains_expected_actions(self) -> None:
        """_DB_UPDATE_ACTIONS must contain the canonical new-order actions."""
        expected = {"oco_placed", "tightened", "placed_pdt_fallback",
                    "placed_fallback", "trailing_upgraded"}
        assert expected <= _DB_UPDATE_ACTIONS, (
            f"Missing actions: {expected - _DB_UPDATE_ACTIONS}"
        )

    def test_skipped_and_pdt_deferred_not_in_action_set(self) -> None:
        """Actions that don't place new orders must NOT be in _DB_UPDATE_ACTIONS."""
        for action in ("skipped", "pdt_deferred", "dry_run_placed", "error", ""):
            assert action not in _DB_UPDATE_ACTIONS, (
                f"Action '{action}' should NOT trigger a DB update"
            )
