"""Tests for scripts/healthcheck_tp_coverage.py.

All broker interactions are mocked via monkeypatching.
No real Alpaca calls are made.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ── path bootstrap ─────────────────────────────────────────────────────────────
_ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(_ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATLAS_ROOT))

from scripts.healthcheck_tp_coverage import (
    MARKETS,
    MISSING_THRESHOLD_MINUTES,
    _load_state,
    _save_state,
    classify_orders,
    run_check,
)
from brokers.base import OrderResult, OrderSide, OrderStatus


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_order(
    ticker: str,
    side: str = "SELL",
    order_type: str = "limit",
    order_class: str = "",
    raw_status: str = "accepted",
) -> OrderResult:
    """Build a minimal OrderResult for testing."""
    atlas_side = OrderSide.SELL if side == "SELL" else OrderSide.BUY
    atlas_status = (
        OrderStatus.CANCELLED
        if raw_status in ("canceled", "expired", "rejected")
        else OrderStatus.PENDING
        if raw_status == "held"
        else OrderStatus.SUBMITTED
    )
    return OrderResult(
        success=True,
        order_id=f"ord-{ticker}-{order_type}",
        ticker=ticker,
        side=atlas_side,
        status=atlas_status,
        raw={
            "order_type": order_type,
            "order_class": order_class,
            "status": raw_status,
            "side": side.lower(),
        },
    )


def _make_position(ticker: str) -> SimpleNamespace:
    """Minimal PositionInfo-like object."""
    return SimpleNamespace(ticker=ticker, shares=1, current_price=100.0)


def _make_broker(
    positions: list[Any],
    orders: list[OrderResult],
    connect_ok: bool = True,
) -> MagicMock:
    """Build a mock broker that returns given positions and orders."""
    broker = MagicMock()
    broker.connect.return_value = connect_ok
    broker.get_positions.return_value = positions
    broker.get_open_orders.return_value = orders
    return broker


def _patch_market(
    monkeypatch: pytest.MonkeyPatch,
    market_positions: dict[str, list[Any]],  # market_id → positions
    market_orders: dict[str, list[OrderResult]],  # market_id → orders
    connect_ok: bool = True,
) -> None:
    """Patch get_active_config + get_live_broker for each market."""
    brokers: dict[str, MagicMock] = {}
    for mkt in MARKETS:
        pos = market_positions.get(mkt, [])
        ords = market_orders.get(mkt, [])
        brokers[mkt] = _make_broker(pos, ords, connect_ok=connect_ok)

    def _fake_config(market_id: str) -> dict:
        return {"market": market_id, "trading": {"live_enabled": True}}

    def _fake_broker(cfg: dict) -> MagicMock:
        return brokers[cfg["market"]]

    monkeypatch.setattr(
        "scripts.healthcheck_tp_coverage.check_market",
        lambda market_id: _check_market_with_mocks(
            market_id, market_positions, market_orders, connect_ok
        ),
    )


def _check_market_with_mocks(
    market_id: str,
    market_positions: dict[str, list[Any]],
    market_orders: dict[str, list[OrderResult]],
    connect_ok: bool = True,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Direct implementation using mock data, bypassing broker calls."""
    if not connect_ok:
        return None, f"Broker connect() returned False for {market_id}"

    positions = market_positions.get(market_id, [])
    orders = market_orders.get(market_id, [])

    results = []
    for pos in positions:
        has_stop, has_tp = classify_orders(orders, pos.ticker)
        results.append({
            "ticker": pos.ticker,
            "market": market_id,
            "has_stop": has_stop,
            "has_tp": has_tp,
        })
    return results, None


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestClassifyOrders:
    """Unit tests for the order classification logic."""

    def test_position_with_stop_and_tp_passes(self):
        """Happy path: stop + limit orders → both covered."""
        orders = [
            _make_order("CAT", order_type="stop"),
            _make_order("CAT", order_type="limit"),
        ]
        has_stop, has_tp = classify_orders(orders, "CAT")
        assert has_stop is True
        assert has_tp is True

    def test_held_status_counts_as_coverage(self):
        """Broker 'held' status (pre-market) MUST count as active coverage."""
        orders = [
            _make_order("GLD", order_type="stop", raw_status="held"),
            _make_order("GLD", order_type="limit", raw_status="accepted"),
        ]
        has_stop, has_tp = classify_orders(orders, "GLD")
        assert has_stop is True, "held stop should count as coverage"
        assert has_tp is True

    def test_canceled_status_does_not_count(self):
        """Canceled orders must NOT count as coverage."""
        orders = [
            _make_order("XLF", order_type="stop", raw_status="canceled"),
            _make_order("XLF", order_type="limit", raw_status="expired"),
        ]
        has_stop, has_tp = classify_orders(orders, "XLF")
        assert has_stop is False, "canceled stop should not count"
        assert has_tp is False, "expired limit should not count"

    def test_oco_bracket_counts_as_both(self):
        """Single OCO/bracket order satisfies BOTH stop and TP coverage."""
        orders = [
            _make_order("XLI", order_type="limit", order_class="oco"),
        ]
        has_stop, has_tp = classify_orders(orders, "XLI")
        assert has_stop is True, "oco order should count as stop coverage"
        assert has_tp is True, "oco order should count as TP coverage"

    def test_bracket_order_class_counts_as_both(self):
        """Bracket order class also satisfies both stop and TP."""
        orders = [
            _make_order("MSFT", order_type="limit", order_class="bracket"),
        ]
        has_stop, has_tp = classify_orders(orders, "MSFT")
        assert has_stop is True
        assert has_tp is True

    def test_buy_orders_ignored(self):
        """BUY orders do not count as protective coverage."""
        orders = [
            _make_order("SPY", side="BUY", order_type="limit"),
            _make_order("SPY", side="BUY", order_type="stop"),
        ]
        has_stop, has_tp = classify_orders(orders, "SPY")
        assert has_stop is False
        assert has_tp is False

    def test_trailing_stop_counts_as_stop(self):
        """Trailing stop order type counts as stop coverage."""
        orders = [
            _make_order("NVDA", order_type="trailing_stop"),
            _make_order("NVDA", order_type="limit"),
        ]
        has_stop, has_tp = classify_orders(orders, "NVDA")
        assert has_stop is True
        assert has_tp is True

    def test_wrong_ticker_ignored(self):
        """Orders for other tickers don't count for the queried ticker."""
        orders = [
            _make_order("AAPL", order_type="stop"),
            _make_order("AAPL", order_type="limit"),
        ]
        has_stop, has_tp = classify_orders(orders, "CAT")
        assert has_stop is False
        assert has_tp is False


class TestRunCheck:
    """Integration tests for the full run_check() flow."""

    def test_position_with_stop_and_tp_passes(self, tmp_path, monkeypatch):
        """All positions covered → exit 0, no alert fired."""
        alert_calls = []
        monkeypatch.setattr("utils.telegram.send_message", lambda *a, **kw: alert_calls.append((a, kw)))

        positions = {"sp500": [_make_position("CAT")]}
        orders = {
            "sp500": [
                _make_order("CAT", order_type="stop"),
                _make_order("CAT", order_type="limit"),
            ]
        }
        monkeypatch.setattr(
            "scripts.healthcheck_tp_coverage.check_market",
            lambda mkt: _check_market_with_mocks(mkt, positions, orders),
        )

        state_file = tmp_path / "state.json"
        exit_code = run_check(no_alert=True, state_path=state_file)

        assert exit_code == 0
        assert alert_calls == [], "No alert should be sent when all covered"

    def test_position_missing_tp_records_first_missing_no_alert_yet(self, tmp_path, monkeypatch):
        """First time a position is missing TP → recorded in state, no alert yet."""
        alert_calls = []
        monkeypatch.setattr("utils.telegram.send_message", lambda *a, **kw: alert_calls.append(a))

        positions = {"sp500": [_make_position("CAT")]}
        orders = {
            "sp500": [
                _make_order("CAT", order_type="stop"),
                # No limit order → missing TP
            ]
        }
        monkeypatch.setattr(
            "scripts.healthcheck_tp_coverage.check_market",
            lambda mkt: _check_market_with_mocks(mkt, positions, orders),
        )

        state_file = tmp_path / "state.json"
        exit_code = run_check(no_alert=True, state_path=state_file)

        # Exit 0 — below threshold (first observation)
        assert exit_code == 0, "Should not alert on first observation (under threshold)"

        # State should be written
        state = _load_state(state_file)
        assert "sp500:CAT" in state["first_missing_at"]

    def test_position_missing_tp_for_6_minutes_alerts(self, tmp_path, monkeypatch):
        """Position missing TP for >5 min → alert fires, exit 1."""
        alert_calls = []
        monkeypatch.setattr("utils.telegram.send_message", lambda *a, **kw: alert_calls.append(a))

        six_min_ago = (datetime.now(tz=timezone.utc) - timedelta(minutes=6)).isoformat()
        state_file = tmp_path / "state.json"
        _save_state(
            {"first_missing_at": {"sp500:CAT": six_min_ago}, "last_run_at": None},
            state_file,
        )

        positions = {"sp500": [_make_position("CAT")]}
        orders = {
            "sp500": [
                _make_order("CAT", order_type="stop"),
                # Still no TP
            ]
        }
        monkeypatch.setattr(
            "scripts.healthcheck_tp_coverage.check_market",
            lambda mkt: _check_market_with_mocks(mkt, positions, orders),
        )

        exit_code = run_check(no_alert=False, state_path=state_file)

        assert exit_code == 1, "Should exit 1 when alert fires"
        assert len(alert_calls) == 1, "Exactly one Telegram alert should be sent"
        alert_text = alert_calls[0][0]
        assert "CAT" in alert_text
        assert "sp500" in alert_text

    def test_position_recovers_state_cleared(self, tmp_path, monkeypatch):
        """Coverage restored → key removed from state file."""
        monkeypatch.setattr("utils.telegram.send_message", lambda *a, **kw: None)

        four_min_ago = (datetime.now(tz=timezone.utc) - timedelta(minutes=4)).isoformat()
        state_file = tmp_path / "state.json"
        _save_state(
            {"first_missing_at": {"sp500:CAT": four_min_ago}, "last_run_at": None},
            state_file,
        )

        positions = {"sp500": [_make_position("CAT")]}
        orders = {
            "sp500": [
                _make_order("CAT", order_type="stop"),
                _make_order("CAT", order_type="limit"),  # TP now present
            ]
        }
        monkeypatch.setattr(
            "scripts.healthcheck_tp_coverage.check_market",
            lambda mkt: _check_market_with_mocks(mkt, positions, orders),
        )

        exit_code = run_check(no_alert=True, state_path=state_file)

        assert exit_code == 0
        state = _load_state(state_file)
        assert "sp500:CAT" not in state["first_missing_at"], "Recovered ticker should be cleared from state"

    def test_broker_connect_failure_alerts_and_exits_2(self, tmp_path, monkeypatch):
        """Broker connection failure → Telegram alert, exit 2."""
        alert_calls = []
        monkeypatch.setattr("utils.telegram.send_message", lambda *a, **kw: alert_calls.append(a))

        def _fail_market(market_id: str):
            return None, f"Broker connect() returned False for {market_id}"

        monkeypatch.setattr("scripts.healthcheck_tp_coverage.check_market", _fail_market)

        state_file = tmp_path / "state.json"
        exit_code = run_check(no_alert=False, state_path=state_file)

        assert exit_code == 2
        assert len(alert_calls) == 1, "Should send one alert on broker failure"

    def test_no_alert_flag_skips_telegram(self, tmp_path, monkeypatch):
        """--no-alert flag prevents Telegram from being called."""
        real_calls = []
        monkeypatch.setattr(
            "utils.telegram.send_message",
            lambda *a, **kw: real_calls.append(a),
        )

        six_min_ago = (datetime.now(tz=timezone.utc) - timedelta(minutes=6)).isoformat()
        state_file = tmp_path / "state.json"
        _save_state(
            {"first_missing_at": {"sp500:CAT": six_min_ago}, "last_run_at": None},
            state_file,
        )

        positions = {"sp500": [_make_position("CAT")]}
        orders = {"sp500": [_make_order("CAT", order_type="stop")]}  # missing TP
        monkeypatch.setattr(
            "scripts.healthcheck_tp_coverage.check_market",
            lambda mkt: _check_market_with_mocks(mkt, positions, orders),
        )

        exit_code = run_check(no_alert=True, state_path=state_file)

        assert exit_code == 1, "Should still exit 1 (alert would have fired)"
        assert real_calls == [], "send_message must NOT be called with --no-alert"

    def test_state_file_corrupted_resets_cleanly(self, tmp_path):
        """Corrupted state file → empty state, no crash."""
        state_file = tmp_path / "state.json"
        state_file.write_text("{invalid json ~~}")

        state = _load_state(state_file)

        assert isinstance(state, dict)
        assert state["first_missing_at"] == {}

    def test_held_status_counts_as_coverage_integration(self, tmp_path, monkeypatch):
        """Held orders (pre-market) must count as active — no false positive."""
        monkeypatch.setattr("utils.telegram.send_message", lambda *a, **kw: None)

        positions = {"sp500": [_make_position("CAT")]}
        orders = {
            "sp500": [
                _make_order("CAT", order_type="stop", raw_status="held"),  # held stop
                _make_order("CAT", order_type="limit", raw_status="accepted"),
            ]
        }
        monkeypatch.setattr(
            "scripts.healthcheck_tp_coverage.check_market",
            lambda mkt: _check_market_with_mocks(mkt, positions, orders),
        )

        state_file = tmp_path / "state.json"
        exit_code = run_check(no_alert=True, state_path=state_file)

        assert exit_code == 0, "Held orders must count as coverage (no false positive)"

    def test_oco_bracket_counts_as_both_integration(self, tmp_path, monkeypatch):
        """Single OCO order → both stop and TP covered → exit 0."""
        monkeypatch.setattr("utils.telegram.send_message", lambda *a, **kw: None)

        positions = {"sp500": [_make_position("XLI")]}
        orders = {
            "sp500": [
                _make_order("XLI", order_type="limit", order_class="oco"),  # OCO counts both
            ]
        }
        monkeypatch.setattr(
            "scripts.healthcheck_tp_coverage.check_market",
            lambda mkt: _check_market_with_mocks(mkt, positions, orders),
        )

        state_file = tmp_path / "state.json"
        exit_code = run_check(no_alert=True, state_path=state_file)

        assert exit_code == 0, "OCO order should count as both stop and TP"

    def test_canceled_status_does_not_count_integration(self, tmp_path, monkeypatch):
        """Canceled orders don't count — should record missing, not cover."""
        monkeypatch.setattr("utils.telegram.send_message", lambda *a, **kw: None)

        positions = {"sp500": [_make_position("XLF")]}
        orders = {
            "sp500": [
                _make_order("XLF", order_type="stop", raw_status="canceled"),
                _make_order("XLF", order_type="limit", raw_status="expired"),
            ]
        }
        monkeypatch.setattr(
            "scripts.healthcheck_tp_coverage.check_market",
            lambda mkt: _check_market_with_mocks(mkt, positions, orders),
        )

        state_file = tmp_path / "state.json"
        exit_code = run_check(no_alert=True, state_path=state_file)

        # First observation → state recorded, no alert yet (exit 0)
        assert exit_code == 0, "Under threshold — first observation"
        state = _load_state(state_file)
        assert "sp500:XLF" in state["first_missing_at"], "Should be tracked as missing"


# ── FIX-HC-TPLESS-001 Tests ────────────────────────────────────────────────────

class TestTPLessStrategyExemption:
    """FIX-HC-TPLESS-001: TP-less strategies exempt from TP coverage alerts."""

    def test_connors_rsi2_with_stop_only_passes(self, tmp_path, monkeypatch):
        """connors_rsi2 (TP-less) with stop only → no alert, is_tp_less=True."""
        from scripts import healthcheck_tp_coverage as hc

        # Setup state file with FCX/connors_rsi2 in commodity_etfs
        monkeypatch.setattr(hc, "_ATLAS_ROOT", tmp_path)
        state_dir = tmp_path / "brokers" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "live_commodity_etfs.json").write_text(
            json.dumps({"positions": [{"ticker": "FCX", "strategy": "connors_rsi2"}]})
        )

        # Stub config — uses nested form to exercise that code path
        def _fake_config(market_id):
            return {"strategies": {"strategies": {
                "connors_rsi2": {"profit_target_atr_mult": 0, "atr_stop_mult": 1.0},
                "momentum_breakout": {"profit_target_atr_mult": 6.0},
            }}}

        import utils.config
        monkeypatch.setattr(utils.config, "get_active_config", _fake_config)

        class _Pos:
            def __init__(self, ticker, strategy=""):
                self.ticker = ticker
                self.strategy = strategy

        class _Order:
            def __init__(self, ticker, side, order_type, status="held", order_class=None):
                self.ticker = ticker
                from enum import Enum
                class _S(Enum):
                    SELL = "SELL"
                self.side = _S.SELL if side == "SELL" else None
                self.raw = {
                    "order_type": order_type,
                    "status": status,
                    "order_class": order_class or "",
                }

        class _StubBroker:
            def connect(self):
                return True
            def get_positions(self):
                return [_Pos("FCX", "connors_rsi2")]
            def get_open_orders(self):
                return [_Order("FCX", "SELL", "stop")]

        from brokers import registry
        monkeypatch.setattr(registry, "get_live_broker", lambda cfg: _StubBroker())

        # Clear cache to ensure fresh config read
        hc._TP_LESS_CACHE.clear()

        results, error = hc.check_market("commodity_etfs")

        assert error is None
        assert len(results) == 1
        r = results[0]
        assert r["ticker"] == "FCX"
        assert r["has_stop"] is True
        assert r["has_tp"] is False
        assert r["is_tp_less"] is True, "connors_rsi2 (profit_target_atr_mult=0) should be TP-less"
        assert r["strategy"] == "connors_rsi2"

    def test_momentum_breakout_with_stop_only_still_flagged(self, tmp_path, monkeypatch):
        """momentum_breakout (uses TP) with stop only → is_tp_less=False (still alerts)."""
        from scripts import healthcheck_tp_coverage as hc

        monkeypatch.setattr(hc, "_ATLAS_ROOT", tmp_path)
        state_dir = tmp_path / "brokers" / "state"
        state_dir.mkdir(parents=True)
        # State file: GLD on momentum_breakout (a TP-using strategy)
        (state_dir / "live_commodity_etfs.json").write_text(
            json.dumps({"positions": [{"ticker": "GLD", "strategy": "momentum_breakout"}]})
        )

        def _fake_config(market_id):
            return {"strategies": {"strategies": {
                "connors_rsi2": {"profit_target_atr_mult": 0, "atr_stop_mult": 1.0},
                "momentum_breakout": {"profit_target_atr_mult": 6.0},
            }}}

        import utils.config
        monkeypatch.setattr(utils.config, "get_active_config", _fake_config)

        class _Pos:
            def __init__(self, ticker, strategy=""):
                self.ticker = ticker
                self.strategy = strategy

        class _Order:
            def __init__(self, ticker, side, order_type, status="accepted", order_class=None):
                self.ticker = ticker
                from enum import Enum
                class _S(Enum):
                    SELL = "SELL"
                self.side = _S.SELL if side == "SELL" else None
                self.raw = {
                    "order_type": order_type,
                    "status": status,
                    "order_class": order_class or "",
                }

        class _StubBroker:
            def connect(self):
                return True
            def get_positions(self):
                return [_Pos("GLD", "momentum_breakout")]
            def get_open_orders(self):
                # Only a stop order — no TP
                return [_Order("GLD", "SELL", "stop")]

        from brokers import registry
        monkeypatch.setattr(registry, "get_live_broker", lambda cfg: _StubBroker())

        hc._TP_LESS_CACHE.clear()

        results, error = hc.check_market("commodity_etfs")

        assert error is None
        assert len(results) == 1
        r = results[0]
        assert r["ticker"] == "GLD"
        assert r["has_stop"] is True
        assert r["has_tp"] is False
        assert r["is_tp_less"] is False, "momentum_breakout (profit_target_atr_mult=6.0) uses TP — should NOT be exempt"
        assert r["strategy"] == "momentum_breakout"

    def test_is_strategy_tp_less_helper(self, monkeypatch):
        """Direct test of the _is_strategy_tp_less helper for all relevant cases."""
        from scripts import healthcheck_tp_coverage as hc

        def _fake_config(market_id):
            return {"strategies": {"strategies": {
                "connors_rsi2": {"profit_target_atr_mult": 0, "atr_stop_mult": 1.0},
                "momentum_breakout": {"profit_target_atr_mult": 6.0},
                "trend_following": {"profit_target_atr_mult": 3.5},
                "tp_disabled_explicit": {"uses_tp": False, "profit_target_atr_mult": 5.0},
                "tp_enabled_explicit": {"uses_tp": True, "profit_target_atr_mult": 0.0},
                "tp_pct_strategy": {"tp_pct": 2.5, "profit_target_atr_mult": 0},
                "zero_pct_strategy": {"tp_pct": 0, "profit_target_atr_mult": 0},
            }}}

        import utils.config
        monkeypatch.setattr(utils.config, "get_active_config", _fake_config)
        hc._TP_LESS_CACHE.clear()

        # TP-less: profit_target_atr_mult=0 (absent defaults to 0 too)
        assert hc._is_strategy_tp_less("sp500", "connors_rsi2") is True

        # TP-using: profit_target_atr_mult=6.0
        assert hc._is_strategy_tp_less("sp500", "momentum_breakout") is False

        # TP-using: profit_target_atr_mult=3.5
        assert hc._is_strategy_tp_less("sp500", "trend_following") is False

        # TP-less: uses_tp=False (overrides even non-zero multiplier)
        assert hc._is_strategy_tp_less("sp500", "tp_disabled_explicit") is True

        # TP-using: uses_tp=True forces TP-using even with zero multiplier
        assert hc._is_strategy_tp_less("sp500", "tp_enabled_explicit") is False

        # TP-using: tp_pct=2.5 (non-zero tp_pct counts as TP-using)
        assert hc._is_strategy_tp_less("sp500", "tp_pct_strategy") is False

        # TP-less: both tp_pct=0 and profit_target_atr_mult=0
        assert hc._is_strategy_tp_less("sp500", "zero_pct_strategy") is True

        # Unknown strategy → fail-safe: assume TP-using (will alert if TP missing)
        assert hc._is_strategy_tp_less("sp500", "unknown_strategy") is False

        # Empty strategy name → always False
        assert hc._is_strategy_tp_less("sp500", "") is False

    def test_tpless_position_missing_stop_alerts(self, tmp_path, monkeypatch):
        """Even TP-less strategies must have a stop — missing stop triggers alert."""
        alert_calls = []
        monkeypatch.setattr("utils.telegram.send_message", lambda *a, **kw: alert_calls.append(a))

        # Pre-seed state: FCX/commodity_etfs was first seen missing 6 min ago
        six_min_ago = (datetime.now(tz=timezone.utc) - timedelta(minutes=6)).isoformat()
        state_file = tmp_path / "state.json"
        _save_state(
            {"first_missing_at": {"commodity_etfs:FCX": six_min_ago}, "last_run_at": None},
            state_file,
        )

        # Patch check_market to return FCX with is_tp_less=True but NO stop
        def _mock_check_market(market_id):
            if market_id == "commodity_etfs":
                return [
                    {
                        "ticker": "FCX",
                        "market": "commodity_etfs",
                        "has_stop": False,
                        "has_tp": False,
                        "strategy": "connors_rsi2",
                        "is_tp_less": True,
                    }
                ], None
            return [], None

        monkeypatch.setattr(
            "scripts.healthcheck_tp_coverage.check_market",
            _mock_check_market,
        )

        exit_code = run_check(no_alert=False, state_path=state_file)

        assert exit_code == 1, "Should exit 1 when TP-less position is missing its required stop"
        assert len(alert_calls) == 1, "Exactly one Telegram alert should fire"
        alert_text = alert_calls[0][0]
        assert "FCX" in alert_text
        assert "MISSING STOP" in alert_text
        assert "connors_rsi2" in alert_text, "Alert should identify the TP-less strategy"

    def test_tpless_position_with_stop_clears_state(self, tmp_path, monkeypatch):
        """TP-less position that recovers (gains stop) → cleared from state."""
        monkeypatch.setattr("utils.telegram.send_message", lambda *a, **kw: None)

        # Pre-seed state: FCX was previously flagged
        four_min_ago = (datetime.now(tz=timezone.utc) - timedelta(minutes=4)).isoformat()
        state_file = tmp_path / "state.json"
        _save_state(
            {"first_missing_at": {"commodity_etfs:FCX": four_min_ago}, "last_run_at": None},
            state_file,
        )

        # Now FCX has a stop (recovered)
        def _mock_check_market(market_id):
            if market_id == "commodity_etfs":
                return [
                    {
                        "ticker": "FCX",
                        "market": "commodity_etfs",
                        "has_stop": True,
                        "has_tp": False,
                        "strategy": "connors_rsi2",
                        "is_tp_less": True,
                    }
                ], None
            return [], None

        monkeypatch.setattr(
            "scripts.healthcheck_tp_coverage.check_market",
            _mock_check_market,
        )

        exit_code = run_check(no_alert=True, state_path=state_file)

        assert exit_code == 0, "TP-less position with stop should be fully covered"
        state = _load_state(state_file)
        assert "commodity_etfs:FCX" not in state["first_missing_at"], \
            "Recovered TP-less ticker should be cleared from state"

    def test_is_strategy_tp_less_config_error_returns_false(self, monkeypatch):
        """Config load failure → fail-safe returns False (treat as TP-using)."""
        from scripts import healthcheck_tp_coverage as hc

        import utils.config
        monkeypatch.setattr(
            utils.config,
            "get_active_config",
            lambda market_id: (_ for _ in ()).throw(FileNotFoundError("no config")),
        )
        hc._TP_LESS_CACHE.clear()

        result = hc._is_strategy_tp_less("nonexistent_market", "connors_rsi2")
        assert result is False, "Config load error should fail safe (return False, not True)"

    def test_lookup_strategy_from_state_returns_empty_on_missing_file(self, tmp_path, monkeypatch):
        """_lookup_strategy_from_state returns '' when state file does not exist."""
        from scripts import healthcheck_tp_coverage as hc

        monkeypatch.setattr(hc, "_ATLAS_ROOT", tmp_path)
        # No state file created in tmp_path

        result = hc._lookup_strategy_from_state("sp500", "AAPL")
        assert result == "", "Should return empty string when state file is missing"

    def test_lookup_strategy_from_state_finds_strategy(self, tmp_path, monkeypatch):
        """_lookup_strategy_from_state returns correct strategy for known ticker."""
        from scripts import healthcheck_tp_coverage as hc

        monkeypatch.setattr(hc, "_ATLAS_ROOT", tmp_path)
        state_dir = tmp_path / "brokers" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "live_sp500.json").write_text(json.dumps({
            "positions": [
                {"ticker": "CAT", "strategy": "momentum_breakout"},
                {"ticker": "NVDA", "strategy": "trend_following"},
            ]
        }))

        assert hc._lookup_strategy_from_state("sp500", "CAT") == "momentum_breakout"
        assert hc._lookup_strategy_from_state("sp500", "NVDA") == "trend_following"
        assert hc._lookup_strategy_from_state("sp500", "UNKNOWN") == ""

    def test_summary_log_counts_tp_less_correctly(self, tmp_path, monkeypatch, caplog):
        """Summary log correctly counts TP-less positions."""
        import logging
        monkeypatch.setattr("utils.telegram.send_message", lambda *a, **kw: None)

        def _mock_check_market(market_id):
            if market_id == "sp500":
                return [
                    {
                        "ticker": "CAT",
                        "market": "sp500",
                        "has_stop": True,
                        "has_tp": True,
                        "strategy": "momentum_breakout",
                        "is_tp_less": False,
                    },
                    {
                        "ticker": "FCX",
                        "market": "sp500",
                        "has_stop": True,
                        "has_tp": False,
                        "strategy": "connors_rsi2",
                        "is_tp_less": True,
                    },
                ], None
            return [], None

        monkeypatch.setattr(
            "scripts.healthcheck_tp_coverage.check_market",
            _mock_check_market,
        )

        state_file = tmp_path / "state.json"
        with caplog.at_level(logging.INFO, logger="scripts.healthcheck_tp_coverage"):
            exit_code = run_check(no_alert=True, state_path=state_file)

        assert exit_code == 0, "Both positions are covered (CAT fully, FCX TP-less with stop)"
        # Check summary line mentions 1 TP-less position
        summary_lines = [r.message for r in caplog.records if "Summary:" in r.message]
        assert len(summary_lines) == 1
        assert "1 TP-less" in summary_lines[0], f"Expected '1 TP-less' in summary: {summary_lines[0]}"
        assert "2/2" in summary_lines[0], f"Expected 2/2 covered in summary: {summary_lines[0]}"
