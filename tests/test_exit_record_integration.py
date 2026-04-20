"""Integration regression: mid-exit failure does not abort subsequent exits (C3 + C4).

Verifies that:
  - C3: LivePortfolio.record_closed_trade() is callable without AttributeError
  - C4: Exit fill-poll exception (get_order_status raises) does NOT abort remaining exits
        in the same execute_plan() call

Run:
    cd /root/atlas && python3 -m pytest tests/test_exit_record_integration.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from brokers.live_executor import LiveExecutor
from brokers.live_portfolio import LivePortfolio
from brokers.base import (
    AccountInfo, OrderResult, OrderSide, OrderStatus, PositionInfo,
)


# ─── Config / executor helpers ───────────────────────────────

def _live_cfg() -> dict:
    return {
        "version": "test-c3c4-integration-v1.0",
        "market": "sp500",
        "market_id": "sp500",
        "trading": {
            "mode": "live",
            "broker": "alpaca",
            "live_enabled": True,
            "live_safety": {
                "max_order_value": 50_000,
                "max_daily_orders": 50,
                "dry_run_first": False,
                "max_daily_loss_pct": 0.20,
            },
        },
        "risk": {
            "starting_equity": 10_000.0,
            "max_risk_per_trade_pct": 0.01,
            "max_open_positions": 10,
        },
        "data": {"source": "alpaca", "history_years": 1},
    }


def _make_executor() -> LiveExecutor:
    ex = LiveExecutor(_live_cfg())
    ex._connected = True
    ex._halted = False
    ex._daily_date = "2026-04-20"
    ex._daily_order_count = 0
    ex._circuit_breaker_tripped = False
    ex._daily_start_equity = 10_000.0
    return ex


def _pending_order_result(ticker: str, qty: int = 10) -> OrderResult:
    """Order with fill_price=0 — triggers the C4 poll loop."""
    return OrderResult(
        success=True,
        order_id=f"ORD-{ticker}-POLL",
        ticker=ticker,
        side=OrderSide.SELL,
        status=OrderStatus.SUBMITTED,
        requested_qty=qty,
        filled_qty=0,
        fill_price=0.0,  # fill_price == 0 triggers the poll loop
        raw={},
    )


def _filled_order_result(ticker: str, qty: int = 10, price: float = 105.0) -> OrderResult:
    """Immediately-filled order — no poll needed."""
    return OrderResult(
        success=True,
        order_id=f"ORD-{ticker}-FILL",
        ticker=ticker,
        side=OrderSide.SELL,
        status=OrderStatus.FILLED,
        requested_qty=qty,
        filled_qty=qty,
        fill_price=price,
        raw={},
    )


def _position(ticker: str, shares: int = 10, price: float = 105.0) -> PositionInfo:
    return PositionInfo(
        ticker=ticker,
        shares=shares,
        entry_price=100.0,
        current_price=price,
        market_value=shares * price,
        unrealized_pnl=shares * (price - 100.0),
        unrealized_pnl_pct=(price - 100.0) / 100.0,
        cost_basis=shares * 100.0,
        strategy="mtf_momentum",
        entry_date="2026-04-10",
    )


# ═══════════════════════════════════════════════════════════════
# C3 + C4 integration
# ═══════════════════════════════════════════════════════════════

class TestC3C4Integration:
    """Integration: mid-exit poll exception (C4) does not abort second exit; C3 path accessible."""

    def test_poll_exception_does_not_abort_second_exit(self):
        """C4+integration: first exit's get_order_status raises; second exit still completes."""
        ex = _make_executor()

        ticker1 = "AAPL"
        ticker2 = "MSFT"

        mock_broker = MagicMock()
        mock_broker.get_account_info.return_value = AccountInfo(
            equity=10_000.0, cash=5_000.0
        )

        # Both positions exist at the broker
        mock_broker.get_positions.return_value = [
            _position(ticker1),
            _position(ticker2),
        ]
        mock_broker.get_open_orders.return_value = []

        # First exit returns fill_price=0 (triggers poll), second returns immediate fill
        place_order_calls: list[str] = []

        def place_order_side_effect(**kwargs):
            t = kwargs.get("ticker", "?")
            place_order_calls.append(str(t))
            if str(t) == ticker1:
                return _pending_order_result(ticker1)   # poll needed
            return _filled_order_result(ticker2)        # immediate fill

        mock_broker.place_order.side_effect = place_order_side_effect

        # get_order_status raises → triggers the C4 guard (try/except/break)
        mock_broker.get_order_status.side_effect = ConnectionError(
            "Simulated network failure (C4 guard test)"
        )

        ex._broker = mock_broker

        plan = {
            "status": "APPROVED",
            "trade_date": "2026-04-20",
            "proposed_entries": [],
            "proposed_exits": [
                {"ticker": ticker1, "reason": "take_profit"},
                {"ticker": ticker2, "reason": "stop_loss"},
            ],
        }

        with (
            patch("brokers.live_executor._journal_entry"),
            patch("brokers.live_executor.LiveExecutor._cancel_open_orders_for_ticker",
                  return_value=0),
            patch("brokers.live_executor.LiveExecutor._run_volatility_gate",
                  return_value={"action": "allow", "reason": "test"}),
            patch("brokers.live_executor.LiveExecutor.check_market_state",
                  return_value={"is_tradeable": True, "message": ""}),
            patch("brokers.live_executor.LiveExecutor._capture_start_equity"),
            patch("brokers.kill_switch.is_halted", return_value=False),
            patch("brokers.alpaca.tradable_assets.filter_tradable",
                  return_value=([], [])),
            # Suppress live dependencies that are not under test
            patch("regime.model.RegimeModel.classify_current",
                  side_effect=RuntimeError("no model in test")),
            patch("journal.logger.TradeLedger"),
            patch("brokers.live_portfolio.LivePortfolio.record_closed_trade"),
            patch("brokers.live_portfolio.LivePortfolio.save_state"),
            patch("time.sleep"),
        ):
            result = ex.execute_plan(plan, "2026-04-20")

        # Both exits must appear in the report
        assert len(result["exits"]) == 2, (
            f"C4: expected 2 exits in report, got {len(result['exits'])}. "
            "Poll exception for first exit must NOT abort processing of second exit."
        )

        # Second exit must succeed (it had an immediate fill)
        exit2 = next(
            (e for e in result["exits"] if e.get("ticker") == ticker2), None
        )
        assert exit2 is not None, f"Exit for {ticker2} must appear in report"
        assert exit2.get("success") is True, (
            f"C4+C3: second exit ({ticker2}) must succeed. Got: {exit2}"
        )

    def test_record_closed_trade_no_attribute_error(self, tmp_path, monkeypatch):
        """C3 integration: LivePortfolio.record_closed_trade() does not raise AttributeError.

        Before C3: _execute_exit called _portfolio.load_state() which doesn't exist,
        raising AttributeError inside the try/except block, preventing the
        record_closed_trade() call from ever executing.
        """
        monkeypatch.setattr("brokers.live_portfolio.PROJECT_ROOT", tmp_path)
        (tmp_path / "brokers" / "state").mkdir(parents=True)

        config = {
            "market_id": "sp500",
            "risk": {
                "starting_equity": 5000,
                "max_risk_per_trade_pct": 0.01,
                "max_open_positions": 10,
                "max_sector_concentration": 2,
                "max_daily_drawdown_pct": 0.02,
                "leverage": 1.0,
            },
            "fees": {},
        }

        portfolio = LivePortfolio(config, market_id="sp500")
        portfolio.broker_data_valid = True

        with patch.object(portfolio, "_trigger_dashboard_refresh"):
            try:
                portfolio.record_closed_trade(
                    {
                        "ticker": "AAPL",
                        "strategy": "mtf_momentum",
                        "entry_price": 150.0,
                        "exit_price": 160.0,
                        "shares": 5,
                        "pnl": 50.0,
                        "pnl_pct": 6.67,
                        "holding_days": 5,
                        "exit_reason": "take_profit",
                        "exit_date": "2026-04-20",
                        "entry_date": "2026-04-15",
                        "order_id": "ORD-C3-001",
                    }
                )
            except AttributeError as err:
                pytest.fail(
                    f"C3: AttributeError raised — phantom load_state() may have returned: {err}"
                )

        assert len(portfolio.closed_trades) == 1, (
            "C3: record_closed_trade must append to closed_trades list"
        )

    def test_execute_plan_reports_dict_even_with_poll_failure(self):
        """C4: execute_plan always returns a report dict, never raises from poll exception."""
        ex = _make_executor()
        ticker = "TSLA"

        mock_broker = MagicMock()
        mock_broker.get_account_info.return_value = AccountInfo(equity=10_000.0, cash=5_000.0)
        mock_broker.get_positions.return_value = [_position(ticker)]
        mock_broker.get_open_orders.return_value = []
        mock_broker.place_order.return_value = _pending_order_result(ticker)
        mock_broker.get_order_status.side_effect = TimeoutError("Broker timeout")
        ex._broker = mock_broker

        plan = {
            "status": "APPROVED",
            "trade_date": "2026-04-20",
            "proposed_entries": [],
            "proposed_exits": [{"ticker": ticker, "reason": "stop_loss"}],
        }

        with (
            patch("brokers.live_executor._journal_entry"),
            patch("brokers.live_executor.LiveExecutor._cancel_open_orders_for_ticker",
                  return_value=0),
            patch("brokers.live_executor.LiveExecutor._run_volatility_gate",
                  return_value={"action": "allow", "reason": "test"}),
            patch("brokers.live_executor.LiveExecutor.check_market_state",
                  return_value={"is_tradeable": True, "message": ""}),
            patch("brokers.live_executor.LiveExecutor._capture_start_equity"),
            patch("brokers.kill_switch.is_halted", return_value=False),
            patch("brokers.alpaca.tradable_assets.filter_tradable",
                  return_value=([], [])),
            patch("regime.model.RegimeModel.classify_current",
                  side_effect=RuntimeError("no model")),
            patch("journal.logger.TradeLedger"),
            patch("brokers.live_portfolio.LivePortfolio.record_closed_trade"),
            patch("brokers.live_portfolio.LivePortfolio.save_state"),
            patch("time.sleep"),
        ):
            # Must not raise — C4 guard must catch the TimeoutError inside the poll
            try:
                result = ex.execute_plan(plan, "2026-04-20")
            except TimeoutError:
                pytest.fail(
                    "C4: TimeoutError from get_order_status must not propagate out of execute_plan"
                )

        assert isinstance(result, dict), "execute_plan must always return a dict"
        assert "exits" in result
        assert len(result["exits"]) == 1
