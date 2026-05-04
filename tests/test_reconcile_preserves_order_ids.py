"""Tests that reconcile_positions --fix preserves stop_order_id / tp_order_id.

Fix (2026-05-04, Commit 1): the corrected_positions loop now loads
position_protective_orders (canonical source of truth, synced from broker)
before falling back to internal state, then empty string.  Previously,
both order ids were silently dropped on every --fix run because
get_positions() has no knowledge of order ids.

Two cases covered:
  1. PO table row exists → stop_order_id + tp_order_id come from PO table.
  2. No PO row but internal_pos has stop_order_id → fallback preserved.
"""
from __future__ import annotations

import json
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_broker_position(ticker: str, shares: int = 9, entry_price: float = 174.0):
    """Return a minimal Position-like SimpleNamespace."""
    return types.SimpleNamespace(ticker=ticker, shares=shares, entry_price=entry_price)


def _make_mock_broker(positions: list) -> MagicMock:
    broker = MagicMock()
    broker.connect.return_value = True
    broker.get_positions.return_value = positions
    broker.disconnect.return_value = None
    return broker


def _make_state_file(state_dir: Path, market_id: str, positions: list[dict]) -> None:
    """Write a minimal live_{market}.json state file to state_dir."""
    state_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "market_id": market_id,
        "mode": "live",
        "positions": positions,
        "closed_trades": [],
        "equity_history": [],
        "last_saved": "2026-05-01T00:00:00",
    }
    (state_dir / f"live_{market_id}.json").write_text(json.dumps(state, indent=2))


def _make_config(config_dir: Path, market_id: str) -> None:
    """Write a minimal active config file to config_dir."""
    config_dir.mkdir(parents=True, exist_ok=True)
    cfg = {"trading": {"broker": "alpaca"}}
    (config_dir / f"{market_id}.json").write_text(json.dumps(cfg))


def _insert_po_row(
    market_id: str,
    ticker: str,
    stop_order_id: str,
    stop_price: float,
    tp_order_id: str,
    tp_price: float,
) -> None:
    """Insert a row into position_protective_orders in the test-isolated DB.

    The _isolate_prod_db autouse fixture (conftest.py) ensures all atlas_db
    operations within a test write to a throw-away SQLite, so this is safe.
    """
    from db import atlas_db
    with atlas_db.get_db() as db:
        db.execute(
            """
            INSERT OR REPLACE INTO position_protective_orders
                (market_id, ticker, trade_id, position_qty,
                 stop_order_id, stop_price,
                 tp_order_id,   tp_price,
                 oco_class, last_synced_at, status)
            VALUES (?, ?, NULL, 9, ?, ?, ?, ?, 'oco', '2026-05-01T00:00:00Z', 'active')
            """,
            (market_id, ticker, stop_order_id, stop_price, tp_order_id, tp_price),
        )


# ── Test 1: PO table row exists → ids flow through to JSON ───────────────────

class TestPreservesOrderIdsFromPOTable:
    """position_protective_orders row → stop/tp order ids appear in JSON."""

    def test_stop_and_tp_order_ids_from_po_table(self, tmp_path: Path) -> None:
        """When PO table has an active row for the ticker, stop_order_id and
        tp_order_id are written to live_{market}.json after --fix.

        Scenario: empty internal state (XLI UNTRACKED) → fix triggers.
        PO table has canonical stop + tp ids for XLI.
        After fix, JSON must carry those ids.
        """
        market_id = "sector_etfs"
        state_dir = tmp_path / "brokers" / "state"
        config_dir = tmp_path / "config" / "active"

        # Empty internal state → XLI will be UNTRACKED → triggers fix
        _make_state_file(state_dir, market_id, positions=[])
        _make_config(config_dir, market_id)

        # Seed test DB with canonical protective-order ids (same values as prod XLI)
        _insert_po_row(
            market_id=market_id,
            ticker="XLI",
            stop_order_id="5d4cc2c2-4115-4e1e-b480-c145f6f29e3f",
            stop_price=170.07,
            tp_order_id="f5e9b55c-a9ec-4ccd-862b-bd999eaa9bba",
            tp_price=200.07,
        )

        mock_broker = _make_mock_broker(
            [_make_broker_position("XLI", shares=9, entry_price=174.0)]
        )

        with (
            patch("scripts.reconcile_positions.PROJECT", tmp_path),
            patch("scripts.reconcile_positions._STATE_DIR", state_dir),
            patch("brokers.registry.get_live_broker", return_value=mock_broker),
            patch("universe.builder.get_universe_tickers", return_value=["XLI"]),
            patch("universe.membership.derive_universe", return_value=market_id),
        ):
            import scripts.reconcile_positions as _rp
            result = _rp.reconcile_positions(market_id=market_id, fix=True, dry_run=False)

        assert not result.get("error"), f"Unexpected error: {result.get('error')}"
        assert result.get("fixed"), "result['fixed'] must be True after --fix"

        written = json.loads((state_dir / f"live_{market_id}.json").read_text())
        by_ticker = {p["ticker"]: p for p in written["positions"]}
        assert "XLI" in by_ticker, "XLI must appear in the written state file"

        xli = by_ticker["XLI"]
        assert xli.get("stop_order_id") == "5d4cc2c2-4115-4e1e-b480-c145f6f29e3f", (
            f"stop_order_id must come from PO table; got {xli.get('stop_order_id')!r}"
        )
        assert xli.get("tp_order_id") == "f5e9b55c-a9ec-4ccd-862b-bd999eaa9bba", (
            f"tp_order_id must come from PO table; got {xli.get('tp_order_id')!r}"
        )
        assert abs(float(xli.get("stop_price", 0)) - 170.07) < 0.01, (
            f"stop_price must come from PO table; got {xli.get('stop_price')}"
        )


# ── Test 2: No PO row, internal state has ids → fallback preserves them ───────

class TestPreservesOrderIdsFromInternalStateFallback:
    """No PO row for ticker → fallback to internal_pos stop_order_id / tp_order_id."""

    def test_internal_state_ids_preserved_when_no_po_row(self, tmp_path: Path) -> None:
        """When no PO row exists for the ticker, ids from the existing internal
        state are carried into the corrected JSON (fallback path).

        Scenario: internal state has XLI with 5 shares (MISMATCH vs broker 9).
        Internal state also has stop_order_id and tp_order_id.
        No PO table row exists for XLI.
        After fix, JSON must retain the internal-state ids.
        """
        market_id = "sector_etfs"
        state_dir = tmp_path / "brokers" / "state"
        config_dir = tmp_path / "config" / "active"

        # Internal state: XLI with qty=5 (broker has 9 → MISMATCH triggers fix)
        # and known order ids that should be preserved.
        internal_xli: dict = {
            "ticker": "XLI",
            "strategy": "momentum_breakout",
            "entry_date": "2026-04-24",
            "entry_price": 174.0,
            "shares": 5,           # mismatched — broker has 9
            "stop_price": 170.07,
            "order_id": "",
            "stop_order_id": "fallback-stop-111",
            "tp_order_id": "fallback-tp-222",
        }
        _make_state_file(state_dir, market_id, positions=[internal_xli])
        _make_config(config_dir, market_id)

        # PO table has NO row for XLI — do NOT call _insert_po_row here

        mock_broker = _make_mock_broker(
            [_make_broker_position("XLI", shares=9, entry_price=174.0)]
        )

        with (
            patch("scripts.reconcile_positions.PROJECT", tmp_path),
            patch("scripts.reconcile_positions._STATE_DIR", state_dir),
            patch("brokers.registry.get_live_broker", return_value=mock_broker),
            patch("universe.builder.get_universe_tickers", return_value=["XLI"]),
            patch("universe.membership.derive_universe", return_value=market_id),
        ):
            import scripts.reconcile_positions as _rp
            result = _rp.reconcile_positions(market_id=market_id, fix=True, dry_run=False)

        assert not result.get("error"), f"Unexpected error: {result.get('error')}"
        assert result.get("fixed"), "result['fixed'] must be True after --fix"

        written = json.loads((state_dir / f"live_{market_id}.json").read_text())
        by_ticker = {p["ticker"]: p for p in written["positions"]}
        assert "XLI" in by_ticker, "XLI must appear in the written state file"

        xli = by_ticker["XLI"]
        assert xli.get("stop_order_id") == "fallback-stop-111", (
            "stop_order_id must fall back to internal state value "
            f"when no PO row exists; got {xli.get('stop_order_id')!r}"
        )
        assert xli.get("tp_order_id") == "fallback-tp-222", (
            "tp_order_id must fall back to internal state value "
            f"when no PO row exists; got {xli.get('tp_order_id')!r}"
        )
