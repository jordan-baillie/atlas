"""Regression tests for Telegram noise reduction (P1 fix).

Covers:
  1. OCO/bracket/OTO stop orders must NOT trigger held-stop detection.
  2. Simple (non-OCO) stuck stops still trigger detection as before.
  3. Intraday drawdown ignores pre-attribution-reset equity history.
  4. Intraday drawdown falls back to starting_equity when no post-reset history.
  5. EOD position monitor deduplication — sends at most once per day.

Root cause: commit 35d2286a migrated 4 positions to OCO brackets.
Alpaca OCO stop legs are permanently status=HELD by design (the stop
activates only when the TP limit does not fill).  _handle_held_stops was
written pre-OCO and treated every HELD stop as a stuck-stop candidate,
causing a cancel+resubmit loop every 15-minute cron cycle.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.sync_protective_orders import _handle_held_stops


# ── Helper factories ──────────────────────────────────────────────────────────

def _make_order(
    ticker: str,
    order_id: str,
    *,
    status: str = "held",
    order_type: str = "stop",
    side: str = "sell",
    order_class: str = "simple",
) -> MagicMock:
    """Build a mock OrderResult with configurable order_class."""
    o = MagicMock()
    o.ticker = ticker
    o.order_id = order_id
    o.raw = {
        "status": status,
        "order_type": order_type,
        "side": side,
        "order_class": order_class,
    }
    return o


def _make_broker(orders: list) -> MagicMock:
    """Mock broker that returns *orders* from get_open_orders."""
    from brokers.base import OrderResult, OrderStatus

    b = MagicMock()
    b.get_open_orders.return_value = orders
    cancel_ok = MagicMock()
    cancel_ok.success = True
    cancel_ok.message = "cancelled"
    b.cancel_order.return_value = cancel_ok
    # _wait_for_cancel_confirm polls get_order_status — return CANCELLED immediately
    b.get_order_status.return_value = OrderResult(
        success=True, order_id="", status=OrderStatus.CANCELLED
    )
    return b


# ── Test 1: OCO/bracket orders must be skipped ────────────────────────────────

class TestHeldStopsSkipsOCO:
    """Verifies OCO/bracket/OTO stop legs are not treated as stuck."""

    @pytest.mark.parametrize("order_class", ["oco", "bracket", "oto", "OCO", "Bracket"])
    def test_oco_order_not_in_currently_held(
        self, tmp_path: Path, order_class: str
    ) -> None:
        """OCO/bracket/OTO stop SELL order with status=held MUST NOT trigger detection."""
        state_file = tmp_path / "held.json"
        broker = _make_broker([
            _make_order("MU", "mu-oco-stop", order_class=order_class),
        ])

        result = _handle_held_stops(
            broker, "sp500",
            dry_run=True,
            send_telegram=False,
            state_file=state_file,
            now_iso="2026-04-30T10:00:00",
        )

        assert result["resubmitted"] == [], (
            f"OCO order_class={order_class!r} must NOT trigger resubmit"
        )
        assert result["newly_held"] == [], (
            f"OCO order_class={order_class!r} must NOT be marked newly_held"
        )
        assert result["errors"] == []
        broker.cancel_order.assert_not_called()

    def test_cat_oco_not_flagged(self, tmp_path: Path) -> None:
        """Reproduce the exact CAT/MU false-positive pattern from the OCO loop bug."""
        state_file = tmp_path / "held.json"
        # Simulate BOTH CAT and MU as OCO bracket members
        broker = _make_broker([
            _make_order("CAT", "cat-oco", order_class="bracket"),
            _make_order("MU", "mu-oco", order_class="oco"),
        ])

        result = _handle_held_stops(
            broker, "sp500",
            dry_run=False,
            send_telegram=False,
            state_file=state_file,
        )

        assert result["resubmitted"] == []
        assert result["newly_held"] == []
        # State file must remain empty (no tracking started)
        if state_file.exists():
            assert json.loads(state_file.read_text()) == {}

    def test_no_telegram_for_oco_on_second_cycle(self, tmp_path: Path) -> None:
        """Even if somehow state was pre-populated, OCO leg must not cancel."""
        state_file = tmp_path / "held.json"
        # Pre-populate as if first cycle ran (the old buggy behaviour)
        state_file.write_text(json.dumps({
            "MU::sp500": {"first_seen": "2026-04-30T00:00:00", "order_id": "mu-oco"},
        }))
        broker = _make_broker([
            _make_order("MU", "mu-oco", order_class="oco"),
        ])

        with patch("utils.telegram.send_message") as mock_tg:
            result = _handle_held_stops(
                broker, "sp500",
                dry_run=False,
                send_telegram=True,
                state_file=state_file,
            )

        # MU is an OCO member → not detected → stale state entry is cleaned up
        assert result["resubmitted"] == []
        broker.cancel_order.assert_not_called()
        mock_tg.assert_not_called()


# ── Test 2: Simple (non-OCO) stuck stop still triggers correctly ───────────────

class TestHeldStopsSimpleStopStillWorks:
    """Non-OCO stops must still be caught by _handle_held_stops."""

    def test_simple_stop_newly_held_first_cycle(self, tmp_path: Path) -> None:
        """order_class='simple' held stop → newly_held on first cycle."""
        state_file = tmp_path / "held.json"
        broker = _make_broker([
            _make_order("AMD", "amd-stop-1", order_class="simple"),
        ])

        result = _handle_held_stops(
            broker, "sp500",
            dry_run=False,
            send_telegram=False,
            state_file=state_file,
            now_iso="2026-04-30T10:00:00",
        )

        assert "AMD" in result["newly_held"], "simple stop must be marked newly_held"
        assert result["resubmitted"] == []
        broker.cancel_order.assert_not_called()

    def test_empty_order_class_still_detected(self, tmp_path: Path) -> None:
        """Missing order_class (empty string) must still be processed (defensive default)."""
        state_file = tmp_path / "held.json"
        broker = _make_broker([
            _make_order("NVDA", "nvda-stop", order_class=""),
        ])

        result = _handle_held_stops(
            broker, "sp500",
            dry_run=False,
            send_telegram=False,
            state_file=state_file,
            now_iso="2026-04-30T10:00:00",
        )

        # Should be newly_held on first cycle (no state yet)
        assert "NVDA" in result["newly_held"]

    def test_simple_stop_triggers_cancel_on_second_cycle(self, tmp_path: Path) -> None:
        """Non-OCO stuck stop: cancel_order called on second consecutive cycle."""
        state_file = tmp_path / "held.json"
        state_file.write_text(json.dumps({
            "GLD::sp500": {"first_seen": "2026-04-30T09:45:00", "order_id": "gld-stop"},
        }))
        broker = _make_broker([
            _make_order("GLD", "gld-stop", order_class="simple"),
        ])

        result = _handle_held_stops(
            broker, "sp500",
            dry_run=False,
            send_telegram=False,
            state_file=state_file,
        )

        assert result["resubmitted"] == ["GLD"]
        broker.cancel_order.assert_called_once_with("gld-stop")


# ── Test 3: Drawdown filters pre-attribution history ──────────────────────────

class TestIntraydayDrawdownAttributionFilter:
    """check_portfolio_drawdown must ignore equity history from before 2026-04-29."""

    def _make_portfolio(
        self,
        equity_history: list[dict],
        starting_equity: float,
        cash: float,
        positions: list | None = None,
    ) -> MagicMock:
        p = MagicMock()
        p.equity_history = equity_history
        p.starting_equity = starting_equity
        p.cash = cash
        p.positions = positions or []
        # equity() just returns cash (no positions in these tests)
        p.equity.return_value = cash
        return p

    def test_pre_reset_history_ignored_no_false_positive(self) -> None:
        """Pre-cutoff equity=5189 must not create a false 78% drawdown alert."""
        from scripts.intraday_monitor import check_portfolio_drawdown

        portfolio = self._make_portfolio(
            equity_history=[
                {"date": "2026-04-15", "equity": 5189.0},
                {"date": "2026-04-30", "equity": 1233.0},
            ],
            starting_equity=971.0,
            cash=1233.0,
        )
        # Prices doesn't matter since portfolio.equity() is mocked
        prices = {}
        fired: dict = {}

        alerts = check_portfolio_drawdown(portfolio, prices, fired)

        # Current equity (1233) > starting_equity (971) → no drawdown
        assert alerts == [], (
            "Pre-attribution history must not produce false-positive drawdown alert"
        )

    def test_pre_reset_history_only_no_false_positive_with_slight_loss(self) -> None:
        """Even with a slight loss, old history must not dominate peak calculation."""
        from scripts.intraday_monitor import check_portfolio_drawdown

        # Pre-reset old peak: 5000; post-reset current: 1200 (24% loss from 5000 → WRONG)
        # But starting_equity=1250 and current=1200 → only 4% loss → should NOT alert (< 3%? actually 4% > 3%)
        # Let's use current=1215 with starting=1250 → (1250-1215)/1250=2.8% < 3% → no alert
        portfolio = self._make_portfolio(
            equity_history=[{"date": "2026-04-01", "equity": 5000.0}],  # pre-reset only
            starting_equity=1250.0,
            cash=1215.0,  # slight dip from starting, but < 3% threshold
        )
        portfolio.equity.return_value = 1215.0
        prices = {}
        fired: dict = {}

        alerts = check_portfolio_drawdown(portfolio, prices, fired)

        # peak = starting_equity=1250, dd=(1250-1215)/1250=2.8% < PORTFOLIO_DD_PCT=3%
        assert alerts == [], (
            "Pre-reset history must be ignored; peak=starting_equity=1250"
        )

    def test_post_reset_history_used_correctly_triggers_alert(self) -> None:
        """Post-reset history IS used and can trigger real alerts."""
        from scripts.intraday_monitor import check_portfolio_drawdown

        portfolio = self._make_portfolio(
            equity_history=[
                {"date": "2026-04-15", "equity": 5000.0},   # pre-reset, ignored
                {"date": "2026-04-29", "equity": 1400.0},   # post-reset cutoff boundary
                {"date": "2026-04-30", "equity": 1300.0},   # post-reset (second day)
            ],
            starting_equity=1000.0,
            cash=1300.0,
        )
        # Current equity = 1200 (drop from post-reset peak of 1400)
        portfolio.equity.return_value = 1200.0
        prices = {}
        fired: dict = {}

        alerts = check_portfolio_drawdown(portfolio, prices, fired)

        # post-reset peak=1400, current=1200 → dd = (1400-1200)/1400 = 14.3% >> 3%
        assert len(alerts) == 1
        assert alerts[0]["type"] == "portfolio_dd"
        assert "14" in alerts[0]["message"]  # 14.3% drawdown


# ── Test 4: Fallback to starting_equity when no post-cutoff history ────────────

class TestDrawdownFallsBackToStartingEquity:
    """If all history is pre-reset, peak = starting_equity."""

    def test_all_pre_reset_uses_starting_equity(self) -> None:
        """All equity_history before 2026-04-29 → peak = starting_equity."""
        from scripts.intraday_monitor import check_portfolio_drawdown

        p = MagicMock()
        p.equity_history = [
            {"date": "2026-04-01", "equity": 5000.0},
            {"date": "2026-04-15", "equity": 4800.0},
        ]
        p.starting_equity = 1000.0
        p.cash = 800.0
        p.positions = []
        p.equity.return_value = 800.0  # current equity: loss from starting

        prices = {}
        fired: dict = {}

        from scripts.intraday_monitor import check_portfolio_drawdown
        alerts = check_portfolio_drawdown(p, prices, fired)

        # peak = starting_equity = 1000; current = 800; dd = 20% >> 3% → alert
        assert len(alerts) == 1
        assert alerts[0]["type"] == "portfolio_dd"
        # The alert should show peak ~= 1000 (starting_equity), NOT 5000
        msg = alerts[0]["message"]
        assert "1,000" in msg or "1000" in msg, (
            f"Peak in alert should be ~starting_equity (1000), not 5000. Got: {msg}"
        )
        assert "5,000" not in msg and "5000" not in msg, (
            f"Pre-reset peak (5000) must NOT appear in alert. Got: {msg}"
        )

    def test_empty_history_uses_starting_equity(self) -> None:
        """equity_history=[] → peak = starting_equity."""
        from scripts.intraday_monitor import check_portfolio_drawdown

        p = MagicMock()
        p.equity_history = []
        p.starting_equity = 1000.0
        p.cash = 950.0
        p.positions = []
        p.equity.return_value = 950.0

        prices = {}
        fired: dict = {}
        alerts = check_portfolio_drawdown(p, prices, fired)

        # dd = (1000-950)/1000 = 5% > 3% threshold → alert
        assert len(alerts) == 1
        assert alerts[0]["type"] == "portfolio_dd"


# ── Test 5: EOD position monitor deduplication ────────────────────────────────

class TestEODPositionMonitorDedup:
    """run_position_monitor must send Telegram at most once per day.

    Design: daily cooldown state file at data/eod_position_monitor_state.json.
    First EOD call sends Telegram (if alerts). Subsequent calls that day
    suppress Telegram regardless of which market triggered EOD.
    Target: max 1 position-monitor Telegram per day (down from 3 with 3 markets).
    """

    def test_first_run_sends_telegram_when_alerts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First call per day with alerts → Telegram sent, state written."""
        import scripts.eod_settlement as eod

        state_file = tmp_path / "eod_monitor.json"
        monkeypatch.setattr(eod, "_EOD_MONITOR_STATE_FILE", state_file)

        sent_calls: list[bool] = []

        def mock_evaluate_all(send_telegram: bool) -> dict:
            sent_calls.append(send_telegram)
            return {"evaluated": 2, "alerts": 1}

        with patch("monitor.evaluator.evaluate_all", mock_evaluate_all):
            eod.run_position_monitor()

        # First call: send_telegram=True
        assert sent_calls == [True]
        # State file written
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["last_sent_date"] == str(date.today())

    def test_second_run_same_day_suppresses_telegram(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Second call same day → Telegram suppressed (send_telegram=False)."""
        import scripts.eod_settlement as eod

        state_file = tmp_path / "eod_monitor.json"
        state_file.write_text(json.dumps({"last_sent_date": str(date.today())}))
        monkeypatch.setattr(eod, "_EOD_MONITOR_STATE_FILE", state_file)

        sent_calls: list[bool] = []

        def mock_evaluate_all(send_telegram: bool) -> dict:
            sent_calls.append(send_telegram)
            return {"evaluated": 2, "alerts": 1}

        with patch("monitor.evaluator.evaluate_all", mock_evaluate_all):
            eod.run_position_monitor()

        # Second call: send_telegram=False (suppressed)
        assert sent_calls == [False]

    def test_three_markets_send_at_most_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulating 3 concurrent EOD market runs → only 1 Telegram send total.

        Design: daily cooldown file. First market to run sends, subsequent
        markets are suppressed. Max sends=1 instead of 3.
        """
        import scripts.eod_settlement as eod

        state_file = tmp_path / "eod_monitor.json"
        monkeypatch.setattr(eod, "_EOD_MONITOR_STATE_FILE", state_file)

        telegram_sends: list[bool] = []

        def mock_evaluate_all(send_telegram: bool) -> dict:
            telegram_sends.append(send_telegram)
            return {"evaluated": 3, "alerts": 2}

        # Simulate 3 consecutive market EOD runs (sp500, commodity_etfs, sector_etfs)
        # Run 1: sp500 — should send
        with patch("monitor.evaluator.evaluate_all", mock_evaluate_all):
            eod.run_position_monitor()

        # Run 2: commodity_etfs — should NOT send (cooldown active)
        with patch("monitor.evaluator.evaluate_all", mock_evaluate_all):
            eod.run_position_monitor()

        # Run 3: sector_etfs — should NOT send
        with patch("monitor.evaluator.evaluate_all", mock_evaluate_all):
            eod.run_position_monitor()

        # Total sends: evaluate_all called 3 times, but send_telegram=True only once
        assert len(telegram_sends) == 3
        true_sends = sum(1 for s in telegram_sends if s)
        assert true_sends <= 1, (
            f"Expected at most 1 Telegram send across 3 market runs, got {true_sends}"
        )

    def test_no_state_write_when_no_alerts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If no alerts fired, do not write the cooldown state (allow next run to try)."""
        import scripts.eod_settlement as eod

        state_file = tmp_path / "eod_monitor.json"
        monkeypatch.setattr(eod, "_EOD_MONITOR_STATE_FILE", state_file)

        def mock_evaluate_all(send_telegram: bool) -> dict:
            return {"evaluated": 2, "alerts": 0}  # no alerts

        with patch("monitor.evaluator.evaluate_all", mock_evaluate_all):
            eod.run_position_monitor()

        # State NOT written when no alerts
        assert not state_file.exists(), (
            "Cooldown state must not be written when no alerts fired"
        )
