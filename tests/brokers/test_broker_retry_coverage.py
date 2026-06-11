"""Regression tests for C1, C2, C5 — broker-call retry coverage.

C1: sync_all_protective_orders wraps get_orders in _broker_call (retries on 429)
C2: _broker_call itself retries correctly on transient errors (OCO submit path)
C5: reconcile_entry_fills and reconcile_exit_fills use _broker_call for get_orders

Run:
    cd /root/atlas && python3 -m pytest tests/test_broker_retry_coverage.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))


class _HTTPLikeError(Exception):
    """Simulates alpaca-py HTTP errors with a status_code attribute."""

    def __init__(self, status_code: int, msg: str = "") -> None:
        super().__init__(msg or f"HTTP {status_code}")
        self.status_code = status_code


# ─── Helpers ─────────────────────────────────────────────────

def _make_broker():
    """Construct an AlpacaBroker without connecting (skips __init__ live checks)."""
    from atlas.brokers.alpaca.broker import AlpacaBroker

    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._connected = True
    broker._paper = True
    broker._feed = "iex"
    broker._tif = "day"
    broker._order_map = {}
    broker._market_data = None
    return broker


def _fake_position(ticker: str = "AAPL") -> SimpleNamespace:
    """Minimal position-like object for sync_all_protective_orders."""
    return SimpleNamespace(
        ticker=ticker,
        shares=10,
        entry_price=150.0,
        current_price=155.0,
        stop_price=140.0,
        take_profit=None,
        entry_date="2026-01-01",
        market_value=1550.0,
        unrealized_pnl=50.0,
        unrealized_pnl_pct=0.03,
        side="long",
        strategy="mtf_momentum",
        sector="Technology",
    )


# ═══════════════════════════════════════════════════════════════
# C1 — sync_all_protective_orders retries get_orders
# ═══════════════════════════════════════════════════════════════

class TestSyncProtectiveRetry:
    """C1 regression: get_orders inside sync_all_protective_orders is wrapped in _broker_call."""

    def test_get_orders_retried_on_429(self):
        """sync_all_protective_orders retries get_orders up to 3 times on HTTP 429."""
        broker = _make_broker()

        call_count = 0

        def flaky_get_orders(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _HTTPLikeError(429, "Too Many Requests")
            return []  # success on 3rd attempt

        broker._trade_client = MagicMock()
        broker._trade_client.get_orders = flaky_get_orders

        with patch("atlas.brokers.retry.time.sleep"):
            result = broker.sync_all_protective_orders(
                positions=[_fake_position()], plan={}, dry_run=True
            )

        # 3 calls total: 2 failures + 1 success
        assert call_count == 3, (
            f"Expected 3 get_orders calls (2 retries + 1 success), got {call_count}. "
            "C1: get_orders must be wrapped in _broker_call."
        )
        # Method must return a result dict, not raise
        assert isinstance(result, dict)
        assert "sl_placed" in result or "errors" in result

    def test_get_orders_non_retryable_400_fails_immediately(self):
        """Control: HTTP 400 is non-retryable — only 1 call, error returned in summary."""
        broker = _make_broker()

        call_count = 0

        def bad_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise _HTTPLikeError(400, "Bad Request")

        broker._trade_client = MagicMock()
        broker._trade_client.get_orders = bad_request

        with patch("atlas.brokers.retry.time.sleep"):
            result = broker.sync_all_protective_orders(
                positions=[_fake_position()], plan={}, dry_run=True
            )

        # 400 must not be retried
        assert call_count == 1, (
            f"Non-retryable 400 must not retry; expected 1 call, got {call_count}"
        )
        # sync_all_protective_orders catches the exception and returns an error summary
        assert isinstance(result, dict)

    def test_c1_source_uses_broker_call(self):
        """Shape check: sync_all_protective_orders calls _broker_call for get_orders (C1)."""
        src = (PROJECT / "atlas" / "brokers" / "alpaca" / "broker.py").read_text(encoding="utf-8")
        idx = src.index("def sync_all_protective_orders")
        # Use 4000-char window: docstring is ~1300 chars; _broker_call is ~2650 chars in
        block = src[idx: idx + 4000]
        assert "_broker_call" in block, (
            "C1: sync_all_protective_orders must wrap get_orders in _broker_call"
        )
        assert "get_orders" in block


# ═══════════════════════════════════════════════════════════════
# C2 — _broker_call retry logic (OCO submit path)
# ═══════════════════════════════════════════════════════════════

class TestOCOSubmitRetry:
    """C2 regression: _broker_call retries transient errors; OCO submit is wrapped."""

    def test_broker_call_retries_on_503(self):
        """_broker_call retries up to 3 times on HTTP 503 (Service Unavailable)."""
        broker = _make_broker()

        attempts: list[int] = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise _HTTPLikeError(503, "Service Unavailable")
            return "ok"

        with patch("atlas.brokers.retry.time.sleep"):
            result = broker._broker_call(flaky)

        assert result == "ok"
        assert len(attempts) == 3, (
            f"Expected 3 attempts (2 failures + 1 success), got {len(attempts)}"
        )

    def test_broker_call_retries_on_429(self):
        """_broker_call retries on HTTP 429 (rate limit)."""
        broker = _make_broker()

        attempts: list[int] = []

        def rate_limited():
            attempts.append(1)
            if len(attempts) < 2:
                raise _HTTPLikeError(429, "Too Many Requests")
            return "success"

        with patch("atlas.brokers.retry.time.sleep"):
            result = broker._broker_call(rate_limited)

        assert result == "success"
        assert len(attempts) == 2

    def test_broker_call_does_not_retry_on_400(self):
        """_broker_call must NOT retry on HTTP 400 (client error — not transient)."""
        broker = _make_broker()

        attempts: list[int] = []

        def bad_request():
            attempts.append(1)
            raise _HTTPLikeError(400, "Bad Request")

        with patch("atlas.brokers.retry.time.sleep"):
            with pytest.raises(_HTTPLikeError):
                broker._broker_call(bad_request)

        assert len(attempts) == 1, (
            f"Must not retry on non-retryable 400; expected 1 call, got {len(attempts)}"
        )

    def test_oco_submit_sites_use_broker_call(self):
        """Shape check: both OCO submit_order sites in broker.py use _broker_call (C2)."""
        src = (PROJECT / "atlas" / "brokers" / "alpaca" / "broker.py").read_text(encoding="utf-8")
        count = src.count("self._broker_call(self._trade_client.submit_order")
        assert count >= 2, (
            f"Expected >=2 submit_order _broker_call wraps (initial OCO + tighten OCO), "
            f"found {count}. C2: both OCO submit sites must be wrapped."
        )

    def test_broker_call_returns_result_on_first_success(self):
        """_broker_call returns the function result immediately on first-attempt success."""
        broker = _make_broker()

        def always_ok():
            return {"status": "filled"}

        result = broker._broker_call(always_ok)
        assert result == {"status": "filled"}

    def test_broker_call_reraises_after_max_retries(self):
        """_broker_call re-raises after all retries exhausted."""
        broker = _make_broker()

        attempts: list[int] = []

        def always_fails():
            attempts.append(1)
            raise _HTTPLikeError(503, "Always unavailable")

        with patch("atlas.brokers.retry.time.sleep"):
            with pytest.raises(_HTTPLikeError):
                broker._broker_call(always_fails)

        # 3 total attempts (DEFAULT_MAX_RETRIES)
        assert len(attempts) == 3, (
            f"Expected 3 total attempts before giving up, got {len(attempts)}"
        )


# ═══════════════════════════════════════════════════════════════
