"""Unit tests for scripts/consolidation_close_positions.py.

Covers universe guard, dry-run default, ticker filtering, clock check,
and the happy-path mock broker sequence.

Run:
    cd /root/atlas && python3 -m pytest tests/test_consolidation_close_positions.py -v --timeout=30
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import scripts.consolidation_close_positions as ccp
from brokers.base import OrderResult, OrderSide, OrderStatus, OrderType, PositionInfo


# ── Test helpers ──────────────────────────────────────────────────────────────

def _make_args(
    live: bool = False,
    market: str | None = None,
    tickers: str | None = None,
    skip_clock_check: bool = True,
) -> "argparse.Namespace":
    import argparse
    return argparse.Namespace(
        live=live,
        market=market,
        tickers=tickers,
        skip_clock_check=skip_clock_check,
    )


def _broker_pos(ticker: str, shares: int, price: float) -> PositionInfo:
    p = PositionInfo(ticker=ticker)
    p.shares = shares
    p.current_price = price
    return p


def _mock_broker(
    tickers_held: list[tuple[str, int, float]] | None = None,
    fill_price: float = 442.10,
    is_open: bool = True,
) -> MagicMock:
    broker = MagicMock()
    positions = [_broker_pos(t, s, p) for t, s, p in (tickers_held or [])]
    broker.get_positions.return_value = positions
    broker.get_clock.return_value = SimpleNamespace(is_open=is_open)
    broker.get_open_orders.return_value = []
    broker.cancel_order.return_value = OrderResult(success=True, order_id="stub")
    broker.place_order.return_value = OrderResult(
        success=True, order_id="sell-123", fill_price=fill_price,
        status=OrderStatus.FILLED,
    )
    broker.connect.return_value = True
    broker.disconnect.return_value = None
    return broker


GLD_POS = {
    "ticker": "GLD", "strategy": "momentum_breakout",
    "entry_date": "2026-05-05", "entry_price": 442.8,
    "shares": 2, "stop_price": 403.32,
    "order_id": "", "stop_order_id": "34ae24d0-xxxx", "tp_order_id": "",
}
XLE_POS = {
    "ticker": "XLE", "strategy": "momentum_breakout",
    "entry_date": "2026-05-05", "entry_price": 59.06,
    "shares": 8, "stop_price": 57.56,
    "order_id": "", "stop_order_id": "a52b73c2-xxxx", "tp_order_id": "",
}
XLI_POS = {
    "ticker": "XLI", "strategy": "momentum_breakout",
    "entry_date": "2026-05-05", "entry_price": 173.97,
    "shares": 9, "stop_price": 170.07,
    "order_id": "", "stop_order_id": "5d4cc2c2-xxxx", "tp_order_id": "",
}

_MINIMAL_CONFIG = {
    "market": "commodity_etfs",
    "trading": {"live_enabled": True, "broker": "alpaca"},
    "risk": {"starting_equity": 1000},
    "fees": {"commission_per_trade": 0, "commission_pct": 0},
}


def _sector_config(market: str = "sector_etfs") -> dict:
    return {**_MINIMAL_CONFIG, "market": market}


# ── 1. Universe guard — reject sp500 ─────────────────────────────────────────

class TestUniverseGuardSp500:

    def test_close_market_raises_for_sp500(self):
        """_close_market('sp500', ...) must raise AssertionError before any I/O."""
        with pytest.raises(AssertionError, match="BLOCKED"):
            ccp._close_market("sp500", _make_args(market="sp500"))

    def test_main_raises_for_sp500(self):
        """main(['--market', 'sp500']) raises AssertionError (not argparse)."""
        with pytest.raises(AssertionError, match="BLOCKED"):
            ccp.main(["--market", "sp500"])


# ── 2. Universe guard — reject unknown universe ───────────────────────────────

class TestUniverseGuardUnknown:

    def test_rejects_foobar(self):
        with pytest.raises(AssertionError, match="BLOCKED"):
            ccp._close_market("foobar", _make_args(market="foobar"))

    def test_rejects_asx(self):
        with pytest.raises(AssertionError, match="BLOCKED"):
            ccp._close_market("asx", _make_args(market="asx"))

    def test_allowed_commodity_etfs_passes_guard(self):
        """commodity_etfs in ALLOWED_UNIVERSES — guard should not fire."""
        assert "commodity_etfs" in ccp.ALLOWED_UNIVERSES

    def test_allowed_sector_etfs_passes_guard(self):
        assert "sector_etfs" in ccp.ALLOWED_UNIVERSES


# ── 3. Default dry-run: place_order NOT called ────────────────────────────────

class TestDefaultDryRun:

    def test_no_place_order_in_dry_run(self):
        broker = _mock_broker(tickers_held=[("GLD", 2, 442.10)])
        state = {"positions": [GLD_POS], "closed_trades": [], "equity_history": []}

        with patch("utils.config.get_active_config", return_value=_MINIMAL_CONFIG), \
             patch("brokers.registry.get_live_broker", return_value=broker), \
             patch("scripts.consolidation_close_positions._load_state_file", return_value=state), \
             patch("time.sleep"):
            results = ccp._close_market("commodity_etfs", _make_args(live=False))

        assert len(results) == 1
        assert results[0].status == "dry_run"
        broker.place_order.assert_not_called()

    def test_dry_run_ticker_and_universe(self):
        broker = _mock_broker(tickers_held=[("GLD", 2, 442.10)])
        state = {"positions": [GLD_POS], "closed_trades": [], "equity_history": []}

        with patch("utils.config.get_active_config", return_value=_MINIMAL_CONFIG), \
             patch("brokers.registry.get_live_broker", return_value=broker), \
             patch("scripts.consolidation_close_positions._load_state_file", return_value=state), \
             patch("time.sleep"):
            results = ccp._close_market("commodity_etfs", _make_args(live=False))

        r = results[0]
        assert r.ticker == "GLD"
        assert r.universe == "commodity_etfs"
        assert "DRY RUN" in r.action
        assert r.fill_price == pytest.approx(442.10, abs=0.01)


# ── 4. Dry-run lists expected ticker rows ─────────────────────────────────────

class TestDryRunListsTargets:

    def test_two_positions_both_returned(self):
        broker = _mock_broker(tickers_held=[("XLE", 8, 59.20), ("XLI", 9, 174.05)])
        state = {"positions": [XLE_POS, XLI_POS], "closed_trades": [], "equity_history": []}
        config = _sector_config("sector_etfs")

        with patch("utils.config.get_active_config", return_value=config), \
             patch("brokers.registry.get_live_broker", return_value=broker), \
             patch("scripts.consolidation_close_positions._load_state_file", return_value=state), \
             patch("time.sleep"):
            results = ccp._close_market("sector_etfs", _make_args(live=False))

        tickers = {r.ticker for r in results}
        assert "XLE" in tickers
        assert "XLI" in tickers
        assert all(r.status == "dry_run" for r in results)


# ── 5. --tickers filter ───────────────────────────────────────────────────────

class TestTickersFilter:

    def test_filter_gld_only(self):
        """--tickers GLD: only GLD result returned; XLE/XLI not in output."""
        broker = _mock_broker(tickers_held=[("GLD", 2, 442.10)])
        state = {"positions": [GLD_POS], "closed_trades": [], "equity_history": []}

        with patch("utils.config.get_active_config", return_value=_MINIMAL_CONFIG), \
             patch("brokers.registry.get_live_broker", return_value=broker), \
             patch("scripts.consolidation_close_positions._load_state_file", return_value=state), \
             patch("time.sleep"):
            results = ccp._close_market(
                "commodity_etfs",
                _make_args(live=False, tickers="GLD"),
            )

        assert len(results) == 1
        assert results[0].ticker == "GLD"

    def test_filter_xle_excludes_xli(self):
        """--tickers XLE in sector_etfs: XLI must not appear in results."""
        broker = _mock_broker(tickers_held=[("XLE", 8, 59.20), ("XLI", 9, 174.05)])
        state = {"positions": [XLE_POS, XLI_POS], "closed_trades": [], "equity_history": []}
        config = _sector_config("sector_etfs")

        with patch("utils.config.get_active_config", return_value=config), \
             patch("brokers.registry.get_live_broker", return_value=broker), \
             patch("scripts.consolidation_close_positions._load_state_file", return_value=state), \
             patch("time.sleep"):
            results = ccp._close_market(
                "sector_etfs",
                _make_args(live=False, tickers="XLE"),
            )

        assert len(results) == 1
        assert results[0].ticker == "XLE"


# ── 6. Market closed — skip ───────────────────────────────────────────────────

class TestMarketClosedSkips:

    def test_closed_market_status_skipped(self):
        """is_open=False with --live: status=skipped, place_order not called."""
        broker = _mock_broker(tickers_held=[("GLD", 2, 442.10)], is_open=False)
        state = {"positions": [GLD_POS], "closed_trades": [], "equity_history": []}

        with patch("utils.config.get_active_config", return_value=_MINIMAL_CONFIG), \
             patch("brokers.registry.get_live_broker", return_value=broker), \
             patch("scripts.consolidation_close_positions._load_state_file", return_value=state), \
             patch("time.sleep"):
            results = ccp._close_market(
                "commodity_etfs",
                _make_args(live=True, skip_clock_check=False),  # consult clock
            )

        assert len(results) == 1
        r = results[0]
        assert r.status == "skipped"
        assert "closed" in r.action.lower()
        broker.place_order.assert_not_called()


# ── 7. Happy-path mock broker ─────────────────────────────────────────────────

class TestHappyPathMockBroker:
    """Full live sequence with all downstream mocks — verifies call signatures."""

    @pytest.fixture()
    def gld_live_result(self):
        """Run _close_ticker for GLD in live mode; collect all mock handles."""
        broker = _mock_broker(tickers_held=[("GLD", 2, 442.10)], fill_price=442.10)
        config = {**_MINIMAL_CONFIG}

        with patch("db.atlas_db.record_trade_exit") as mock_exit_db, \
             patch("brokers.live_portfolio.LivePortfolio.execute_exit",
                   return_value={
                       "ticker": "GLD", "pnl": -1.40, "pnl_pct": -0.15,
                       "exit_type": "manual_consolidation_close",
                   }) as mock_lp_exit, \
             patch("scripts.consolidation_close_positions._update_protective_orders_db") as mock_ppo, \
             patch("scripts.consolidation_close_positions._cancel_protective_orders",
                   return_value=1) as mock_cancel, \
             patch("time.sleep"):
            result = ccp._close_ticker(
                ticker="GLD",
                state_pos_dict=GLD_POS,
                broker=broker,
                all_state_positions=[GLD_POS],
                config=config,
                market="commodity_etfs",
                live=True,
                skip_clock_check=True,
            )

        return {
            "result": result,
            "broker": broker,
            "mock_cancel": mock_cancel,
            "mock_exit_db": mock_exit_db,
            "mock_lp_exit": mock_lp_exit,
            "mock_ppo": mock_ppo,
        }

    def test_cancel_called_once_for_ticker(self, gld_live_result):
        """_cancel_protective_orders must be called exactly once for GLD."""
        m = gld_live_result["mock_cancel"]
        m.assert_called_once()
        _, called_ticker = m.call_args.args
        assert called_ticker == "GLD"

    def test_place_order_market_sell(self, gld_live_result):
        """broker.place_order called once: SELL MARKET 2 shares of GLD."""
        broker = gld_live_result["broker"]
        broker.place_order.assert_called_once()
        kw = broker.place_order.call_args.kwargs
        assert kw["ticker"] == "GLD"
        assert kw["side"] == OrderSide.SELL
        assert kw["qty"] == 2
        assert kw["order_type"] == OrderType.MARKET

    def test_execute_exit_called_with_fill_price(self, gld_live_result):
        """LivePortfolio.execute_exit called with fill_price=442.10 and correct exit_type."""
        m = gld_live_result["mock_lp_exit"]
        m.assert_called_once()
        kw = m.call_args.kwargs
        assert kw["ticker"] == "GLD"
        assert kw["exit_price"] == pytest.approx(442.10, abs=0.01)
        assert kw["exit_type"] == "manual_consolidation_close"

    def test_trades_db_updated_with_exit(self, gld_live_result):
        """atlas_db.record_trade_exit called once with correct ticker and exit_reason."""
        m = gld_live_result["mock_exit_db"]
        m.assert_called_once()
        kw = m.call_args.kwargs
        assert kw["ticker"] == "GLD"
        assert kw["exit_reason"] == "manual_consolidation_close"

    def test_ppo_updated_to_cancelled(self, gld_live_result):
        """_update_protective_orders_db called with market_id and ticker."""
        m = gld_live_result["mock_ppo"]
        m.assert_called_once_with("commodity_etfs", "GLD")

    def test_result_status_closed(self, gld_live_result):
        r = gld_live_result["result"]
        assert r.status == "closed"

    def test_result_fill_price(self, gld_live_result):
        r = gld_live_result["result"]
        assert r.fill_price == pytest.approx(442.10, abs=0.01)

    def test_result_pnl_from_execute_exit(self, gld_live_result):
        """PnL is taken from LivePortfolio.execute_exit return value."""
        r = gld_live_result["result"]
        assert r.pnl == pytest.approx(-1.40, abs=0.01)
