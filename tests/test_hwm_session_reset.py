"""Regression tests for HWM session-reset bug (kill_switch incident 2026-04-28).

Prior bug: daily_high_water persisted across sessions because no date guard
existed. Internal equity calc had accounting holes (e.g. AMD trailing-stop
fill not reflected) which inflated apparent drawdown vs broker reality.
"""
import pytest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch
from brokers.live_portfolio import LivePortfolio


@pytest.fixture
def portfolio(tmp_path, monkeypatch):
    # Redirect state path to tmp dir so _load_local_state() finds nothing
    # and we start with clean defaults (daily_high_water=5000, date=None).
    import brokers.live_portfolio as lp_mod
    monkeypatch.setattr(lp_mod, "PROJECT_ROOT", tmp_path)
    cfg = {
        "market_id": "sp500_test",
        "starting_equity": 5000.0,
        "trading": {"live_enabled": True, "max_daily_drawdown_pct": 0.025},
        "risk": {"max_positions": 8, "max_risk_per_trade_pct": 0.01, "leverage": 1.0},
        "fees": {},
        "dual_write_market_state": False,  # skip SQLite dual-write in tests
    }
    p = LivePortfolio(cfg)
    p.broker_data_valid = True
    return p


def test_hwm_resets_when_date_changes(portfolio):
    """HWM must reset to snapshot anchor (not broker equity) when session date advances.

    Updated 2026-05-06: Old assertion checked HWM==broker_eq — that WAS the bug.
    Fix A anchors HWM to _latest_snapshot_allocated_equity() which returns None
    in this test (no market_equity_history table in isolated test DB), so the
    fallback is starting_equity=5000.  effective_eq=broker_eq=5200 > HWM=5000
    → dd is negative (portfolio up) → no halt.
    """
    portfolio.daily_high_water = 5500.0
    portfolio.daily_high_water_date = (date.today() - timedelta(days=1)).isoformat()
    portfolio._broker_equity = 5200.0  # broker eq today

    halted, dd = portfolio.check_daily_drawdown()

    # HWM anchors to starting_equity (5000.0) when no snapshot available —
    # NOT to broker_eq (5200.0) which is the phantom-HWM bug.
    assert portfolio.daily_high_water == portfolio.starting_equity, (
        "HWM should reset to starting_equity when no snapshot available"
    )
    assert portfolio.daily_high_water_date == date.today().isoformat()
    # effective_eq (5200) > HWM (5000) → dd is negative — portfolio grew, no halt
    assert dd <= 0.0, f"After reset with broker_eq > HWM, dd should be non-positive, got {dd:.4f}"
    assert halted is False


def test_drawdown_uses_broker_equity_not_internal(portfolio):
    """Drawdown must use broker equity, ignoring stale internal equity()."""
    portfolio.daily_high_water = 5400.0
    portfolio.daily_high_water_date = date.today().isoformat()
    portfolio._broker_equity = 5350.0  # broker says minor drawdown
    # Make internal equity() return a number that would breach if used
    with patch.object(LivePortfolio, "equity", return_value=5000.0):
        halted, dd = portfolio.check_daily_drawdown()
    assert abs(dd - (5400.0 - 5350.0) / 5400.0) < 1e-6, "dd must use broker equity"
    assert halted is False, "0.93% dd should not halt"


def test_amd_style_midflight_closure_no_phantom_drawdown(portfolio):
    """AMD trailing-stop fill mid-session should NOT trigger phantom HALT.

    Scenario: AMD position closes via broker-side trailing stop, broker cash
    goes UP by $118 but Atlas internal equity() lags because reconcile hasn't
    run yet. HWM uses broker equity, so dd stays at 0 (or even negative).
    """
    portfolio.daily_high_water = 5300.0
    portfolio.daily_high_water_date = date.today().isoformat()
    portfolio._broker_equity = 5418.57  # broker after trailing stop fill (+$118)
    with patch.object(LivePortfolio, "equity", return_value=5000.0):  # stale
        halted, dd = portfolio.check_daily_drawdown()
    assert halted is False, "Mid-flight closure should not halt"
    # HWM should ratchet up to new broker equity
    assert portfolio.daily_high_water >= 5418.57


def test_idempotent_within_session(portfolio):
    """Two calls in same session with stable broker eq must not change HWM."""
    portfolio.daily_high_water = 5300.0
    portfolio.daily_high_water_date = date.today().isoformat()
    portfolio._broker_equity = 5250.0
    halted1, dd1 = portfolio.check_daily_drawdown()
    halted2, dd2 = portfolio.check_daily_drawdown()
    assert (halted1, dd1) == (halted2, dd2)
    assert portfolio.daily_high_water == 5300.0  # unchanged


def test_falls_back_to_internal_equity_when_broker_zero(portfolio, caplog):
    """If broker_equity()==0, fall back to equity() and WARN."""
    portfolio.daily_high_water = 5300.0
    portfolio.daily_high_water_date = date.today().isoformat()
    portfolio._broker_equity = 0.0  # broker invalid
    with patch.object(LivePortfolio, "equity", return_value=5200.0):
        import logging
        with caplog.at_level(logging.WARNING, logger="atlas.live_portfolio"):
            halted, dd = portfolio.check_daily_drawdown()
    # Should still compute dd from internal eq, with warning
    assert any("broker_equity" in r.message.lower() or "fallback" in r.message.lower()
               for r in caplog.records if r.levelname == "WARNING"), "Expected fallback warning"
