"""Regression tests for Task #327: persist Alpaca TP/SL order UUID to
live_{market}.json state after successful retro-placement.

Tests verify that ``_apply_state_file_consistency`` (and its caller
``_apply_db_consistency``) write ``stop_order_id`` / ``tp_order_id`` to
the JSON position state file so subsequent sync cycles can detect the
existing protective order without re-querying the broker.

Key assertions:
- TP UUID written to live_sp500.json after successful TP placement
- SL UUID written to live_sp500.json after successful SL placement
- Existing non-empty IDs are NOT overwritten with empty strings
- Failure path (broker error / placement failure) does NOT persist a UUID
- Paper pass does NOT update live_*.json (only paper_*.json)
- Missing state file / corrupted JSON is non-fatal (no exception raised)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from brokers.base import OrderResult, OrderSide, OrderStatus  # noqa: E402
from scripts.sync_protective_orders import (  # noqa: E402
    _apply_db_consistency,
    _apply_state_file_consistency,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_state_file(state_dir: Path, market_id: str, positions: list[dict]) -> Path:
    """Write a minimal live_{market_id}.json to *state_dir*."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"live_{market_id}.json"
    state = {
        "market_id": market_id,
        "mode": "live",
        "positions": positions,
        "closed_trades": [],
        "equity_history": [],
    }
    path.write_text(json.dumps(state, indent=2))
    return path


def _load_state(state_dir: Path, market_id: str) -> dict:
    path = state_dir / f"live_{market_id}.json"
    return json.loads(path.read_text())


def _make_open_order(
    order_id: str,
    ticker: str,
    order_type: str,  # "limit" | "stop" | "trailing_stop"
    side: str = "sell",
) -> OrderResult:
    """Minimal OrderResult as returned by broker.get_open_orders()."""
    return OrderResult(
        success=True,
        order_id=order_id,
        ticker=ticker,
        side=OrderSide.SELL if side == "sell" else OrderSide.BUY,
        status=OrderStatus.PENDING,
        raw={"order_type": order_type},
    )


def _make_broker(open_orders: list[OrderResult]) -> MagicMock:
    """Mock broker that returns *open_orders* from get_open_orders()."""
    broker = MagicMock()
    broker.get_open_orders.return_value = open_orders
    return broker


def _oco_sync_result(ticker: str, qty: int = 1, stop: float = 90.0, tp: float = 120.0) -> dict:
    """Minimal sync_result as returned by broker.sync_all_protective_orders()
    for a successful OCO placement."""
    return {
        "sl_placed": 1,
        "tp_placed": 1,
        "sl_already_exists": 0,
        "tp_already_exists": 0,
        "errors": 0,
        "per_ticker": {
            ticker: {
                "sl_action": "oco_placed",
                "tp_action": "oco_placed",
                "stop_price": stop,
                "take_profit": tp,
                "qty": qty,
            }
        },
    }


def _sl_only_sync_result(ticker: str, qty: int = 1, stop: float = 90.0) -> dict:
    """sync_result for SL-only (PDT fallback / trailing placed)."""
    return {
        "sl_placed": 1,
        "tp_placed": 0,
        "sl_already_exists": 0,
        "tp_already_exists": 0,
        "errors": 0,
        "per_ticker": {
            ticker: {
                "sl_action": "placed_pdt_fallback",
                "tp_action": "pdt_deferred",
                "stop_price": stop,
                "qty": qty,
            }
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# Tests for _apply_state_file_consistency directly
# ═══════════════════════════════════════════════════════════════════════════

class TestApplyStateFileConsistency:
    """Unit tests for the _apply_state_file_consistency helper."""

    def test_tp_order_id_written_to_state_file(self, tmp_path):
        """TP UUID is persisted to live_sp500.json when ticker_ops has a tp ID."""
        state_dir = tmp_path / "state"
        _make_state_file(state_dir, "sp500", [
            {"ticker": "AAPL", "stop_order_id": "", "tp_order_id": ""},
        ])

        ticker_ops = {"AAPL": {"stop": "", "tp": "test-tp-uuid-12345"}}
        _apply_state_file_consistency("sp500", ticker_ops, state_dir=state_dir)

        state = _load_state(state_dir, "sp500")
        aapl = next(p for p in state["positions"] if p["ticker"] == "AAPL")
        assert aapl["tp_order_id"] == "test-tp-uuid-12345", (
            f"Expected tp_order_id='test-tp-uuid-12345', got {aapl['tp_order_id']!r}"
        )

    def test_stop_order_id_written_to_state_file(self, tmp_path):
        """SL UUID is persisted to live_sp500.json when ticker_ops has a stop ID."""
        state_dir = tmp_path / "state"
        _make_state_file(state_dir, "sp500", [
            {"ticker": "MSFT", "stop_order_id": "", "tp_order_id": ""},
        ])

        ticker_ops = {"MSFT": {"stop": "test-sl-uuid-99999", "tp": ""}}
        _apply_state_file_consistency("sp500", ticker_ops, state_dir=state_dir)

        state = _load_state(state_dir, "sp500")
        msft = next(p for p in state["positions"] if p["ticker"] == "MSFT")
        assert msft["stop_order_id"] == "test-sl-uuid-99999", (
            f"Expected stop_order_id='test-sl-uuid-99999', got {msft['stop_order_id']!r}"
        )

    def test_both_order_ids_written_to_state_file(self, tmp_path):
        """Both stop_order_id and tp_order_id are persisted when both present."""
        state_dir = tmp_path / "state"
        _make_state_file(state_dir, "sp500", [
            {"ticker": "TSLA", "stop_order_id": "", "tp_order_id": ""},
        ])

        ticker_ops = {"TSLA": {"stop": "stop-uuid-abc", "tp": "tp-uuid-xyz"}}
        _apply_state_file_consistency("sp500", ticker_ops, state_dir=state_dir)

        state = _load_state(state_dir, "sp500")
        tsla = next(p for p in state["positions"] if p["ticker"] == "TSLA")
        assert tsla["stop_order_id"] == "stop-uuid-abc"
        assert tsla["tp_order_id"] == "tp-uuid-xyz"

    def test_existing_id_not_overwritten_with_empty(self, tmp_path):
        """Existing non-empty IDs must NOT be clobbered by empty strings in ticker_ops."""
        state_dir = tmp_path / "state"
        _make_state_file(state_dir, "sp500", [
            {
                "ticker": "NVDA",
                "stop_order_id": "prior-stop-id",
                "tp_order_id": "prior-tp-id",
            },
        ])

        # Ops has empty values (order not resolved this cycle)
        ticker_ops = {"NVDA": {"stop": "", "tp": ""}}
        _apply_state_file_consistency("sp500", ticker_ops, state_dir=state_dir)

        state = _load_state(state_dir, "sp500")
        nvda = next(p for p in state["positions"] if p["ticker"] == "NVDA")
        assert nvda["stop_order_id"] == "prior-stop-id", "Existing stop_order_id was clobbered"
        assert nvda["tp_order_id"] == "prior-tp-id", "Existing tp_order_id was clobbered"

    def test_no_file_rewrite_when_ids_already_match(self, tmp_path):
        """If the IDs already match, the file should NOT be rewritten (mtime unchanged)."""
        state_dir = tmp_path / "state"
        path = _make_state_file(state_dir, "sp500", [
            {
                "ticker": "AMZN",
                "stop_order_id": "existing-stop-uuid",
                "tp_order_id": "existing-tp-uuid",
            },
        ])
        import os
        mtime_before = os.path.getmtime(str(path))

        # Same IDs — nothing should change
        ticker_ops = {"AMZN": {"stop": "existing-stop-uuid", "tp": "existing-tp-uuid"}}
        _apply_state_file_consistency("sp500", ticker_ops, state_dir=state_dir)

        mtime_after = os.path.getmtime(str(path))
        assert mtime_after == mtime_before, (
            "State file was rewritten even though IDs were already current"
        )

    def test_missing_state_file_is_nonfatal(self, tmp_path):
        """If live_sp500.json does not exist, function returns silently (no exception)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        # No state file created — should not raise
        ticker_ops = {"AAPL": {"stop": "some-uuid", "tp": ""}}
        _apply_state_file_consistency("sp500", ticker_ops, state_dir=state_dir)  # should not raise

    def test_corrupted_json_is_nonfatal(self, tmp_path, caplog):
        """Corrupted state JSON is logged as WARNING but does NOT raise an exception."""
        import logging
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        corrupt_path = state_dir / "live_sp500.json"
        corrupt_path.write_text("{{{{NOT JSON")

        ticker_ops = {"AAPL": {"stop": "uuid-abc", "tp": ""}}
        with caplog.at_level(logging.WARNING, logger="atlas.sync_protective_orders"):
            _apply_state_file_consistency("sp500", ticker_ops, state_dir=state_dir)

        warning_msgs = [r.message for r in caplog.records if "failed to update" in r.message]
        assert warning_msgs, "Expected a WARNING log for corrupted JSON"

    def test_ticker_not_in_state_file_is_skipped(self, tmp_path):
        """If ticker_ops has a ticker not in state file positions, it is silently skipped."""
        state_dir = tmp_path / "state"
        _make_state_file(state_dir, "sp500", [
            {"ticker": "CAT", "stop_order_id": "", "tp_order_id": ""},
        ])

        # AAPL is in ticker_ops but NOT in the state file
        ticker_ops = {"AAPL": {"stop": "some-stop-uuid", "tp": "some-tp-uuid"}}
        _apply_state_file_consistency("sp500", ticker_ops, state_dir=state_dir)

        # CAT should be unchanged
        state = _load_state(state_dir, "sp500")
        cat = next(p for p in state["positions"] if p["ticker"] == "CAT")
        assert cat["stop_order_id"] == ""
        assert cat["tp_order_id"] == ""


# ═══════════════════════════════════════════════════════════════════════════
# Tests via _apply_db_consistency (end-to-end through the main helper)
# ═══════════════════════════════════════════════════════════════════════════

class TestApplyDbConsistencyStateFilePersist:
    """Integration-style tests: call _apply_db_consistency with mocked broker
    and verify live_{market}.json is updated with returned order IDs."""

    def _run(
        self,
        tmp_path: Path,
        ticker: str,
        open_orders: list[OrderResult],
        sync_result: dict,
        market_id: str = "sp500",
        pass_label: str = "live",
    ) -> dict:
        """Helper: run _apply_db_consistency with mocked deps, return state."""
        state_dir = tmp_path / "state"
        _make_state_file(state_dir, market_id, [
            {"ticker": ticker, "stop_order_id": "", "tp_order_id": ""},
        ])
        broker = _make_broker(open_orders)

        with (
            patch("scripts.sync_protective_orders.update_trade_protective_orders",
                  return_value=0, create=True),
            patch("scripts.sync_protective_orders._protective_ledger_enabled",
                  return_value=False),
        ):
            # Import the function from the module to pick up the patch correctly
            from scripts.sync_protective_orders import _apply_db_consistency as _fn
            _fn(broker, market_id, sync_result, pass_label, state_dir=state_dir)

        return _load_state(state_dir, market_id)

    def test_tp_uuid_persisted_via_apply_db_consistency(self, tmp_path):
        """TP UUID from broker open orders is written to live_sp500.json."""
        tp_order_id = "test-tp-uuid-12345"
        orders = [
            _make_open_order(tp_order_id, "AAPL", "limit"),   # TP leg
        ]
        sync_result = _oco_sync_result("AAPL")

        state = self._run(tmp_path, "AAPL", orders, sync_result)
        aapl = next(p for p in state["positions"] if p["ticker"] == "AAPL")

        assert aapl["tp_order_id"] == tp_order_id, (
            f"Expected tp_order_id={tp_order_id!r}, got {aapl['tp_order_id']!r}"
        )

    def test_sl_uuid_persisted_via_apply_db_consistency(self, tmp_path):
        """SL UUID from broker open orders is written to live_sp500.json."""
        sl_order_id = "test-sl-uuid-67890"
        orders = [
            _make_open_order(sl_order_id, "MSFT", "stop"),   # SL leg
        ]
        sync_result = _sl_only_sync_result("MSFT")

        state = self._run(tmp_path, "MSFT", orders, sync_result)
        msft = next(p for p in state["positions"] if p["ticker"] == "MSFT")

        assert msft["stop_order_id"] == sl_order_id, (
            f"Expected stop_order_id={sl_order_id!r}, got {msft['stop_order_id']!r}"
        )

    def test_failed_submit_does_not_persist_uuid(self, tmp_path):
        """If the submit fails (action=error), no UUID should be written."""
        # No open orders at broker (placement failed)
        orders = []
        # sync_result with error action
        sync_result = {
            "sl_placed": 0,
            "tp_placed": 0,
            "sl_already_exists": 0,
            "tp_already_exists": 0,
            "errors": 1,
            "per_ticker": {
                "AMZN": {
                    "sl_action": "error",
                    "tp_action": "error",
                    "sl_message": "Broker rejected order",
                    "stop_price": 90.0,
                    "qty": 1,
                }
            },
        }
        state = self._run(tmp_path, "AMZN", orders, sync_result)
        amzn = next(p for p in state["positions"] if p["ticker"] == "AMZN")

        assert amzn["stop_order_id"] == "", (
            f"stop_order_id should stay empty on error, got {amzn['stop_order_id']!r}"
        )
        assert amzn["tp_order_id"] == "", (
            f"tp_order_id should stay empty on error, got {amzn['tp_order_id']!r}"
        )

    def test_pdt_deferred_does_not_persist_uuid(self, tmp_path):
        """PDT-deferred action should NOT write any UUID to state file."""
        orders = []  # no orders placed
        sync_result = {
            "sl_placed": 0,
            "tp_placed": 0,
            "sl_already_exists": 0,
            "tp_already_exists": 0,
            "errors": 0,
            "pdt_deferred": 1,
            "per_ticker": {
                "TSLA": {
                    "sl_action": "pdt_deferred",
                    "tp_action": "pdt_deferred",
                    "stop_price": 190.0,
                    "qty": 2,
                }
            },
        }
        state = self._run(tmp_path, "TSLA", orders, sync_result)
        tsla = next(p for p in state["positions"] if p["ticker"] == "TSLA")

        assert tsla["stop_order_id"] == "", (
            f"stop_order_id should stay empty for pdt_deferred, got {tsla['stop_order_id']!r}"
        )
        assert tsla["tp_order_id"] == "", (
            f"tp_order_id should stay empty for pdt_deferred, got {tsla['tp_order_id']!r}"
        )

    def test_paper_pass_does_not_update_live_state_file(self, tmp_path):
        """pass_label='paper' must NOT write to live_sp500.json."""
        tp_order_id = "paper-tp-uuid-99999"
        orders = [
            _make_open_order(tp_order_id, "NVDA", "limit"),
        ]
        sync_result = _oco_sync_result("NVDA")

        # pass_label="paper" — should skip state file update
        state_dir = tmp_path / "state"
        _make_state_file(state_dir, "sp500", [
            {"ticker": "NVDA", "stop_order_id": "", "tp_order_id": ""},
        ])
        broker = _make_broker(orders)

        with (
            patch("scripts.sync_protective_orders.update_trade_protective_orders",
                  return_value=0, create=True),
            patch("scripts.sync_protective_orders._protective_ledger_enabled",
                  return_value=False),
        ):
            from scripts.sync_protective_orders import _apply_db_consistency as _fn
            _fn(broker, "sp500", sync_result, "paper", state_dir=state_dir)

        state = _load_state(state_dir, "sp500")
        nvda = next(p for p in state["positions"] if p["ticker"] == "NVDA")
        # Paper pass should NOT have updated the live state file
        assert nvda["tp_order_id"] == "", (
            f"Paper pass must not write tp_order_id to live state file, "
            f"got {nvda['tp_order_id']!r}"
        )

    def test_trailing_stop_uuid_persisted(self, tmp_path):
        """Trailing stop (Path B) UUID is written to stop_order_id in state."""
        sl_order_id = "trailing-sl-uuid-abc"
        orders = [
            _make_open_order(sl_order_id, "CAT", "trailing_stop"),
        ]
        # trailing_placed action
        sync_result = {
            "sl_placed": 1,
            "tp_placed": 0,
            "sl_already_exists": 0,
            "tp_already_exists": 0,
            "errors": 0,
            "per_ticker": {
                "CAT": {
                    "sl_action": "trailing_placed",
                    "tp_action": "trailing",
                    "stop_price": 180.0,
                    "qty": 1,
                }
            },
        }

        # Note: "trailing_placed" is NOT in _DB_UPDATE_ACTIONS — so DB update
        # is skipped.  But state file update uses ticker_ops (all resolved IDs),
        # so trailing_stop orders are captured there.
        state = self._run(tmp_path, "CAT", orders, sync_result)
        cat = next(p for p in state["positions"] if p["ticker"] == "CAT")

        assert cat["stop_order_id"] == sl_order_id, (
            f"Expected trailing stop_order_id={sl_order_id!r}, got {cat['stop_order_id']!r}"
        )
