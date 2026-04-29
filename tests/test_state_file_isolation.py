"""Verify that the autouse LivePortfolio state-file isolation fixtures work.

These tests assert that the session+function-scope autouse fixtures in conftest.py
correctly redirect brokers.live_portfolio._STATE_DIR away from the production path
so that test-time LivePortfolio.save_state() / record_equity() calls never write to
/root/atlas/brokers/state/live_*.json.

See: conftest._isolate_live_portfolio_state_session + _isolate_live_portfolio_state
Root cause fixed: 2026-04-29 — pytest run at 19:56 emptied live_sp500.json positions
(CAT was lost from state).  Second in a series: first was kill_switch._HALT_FILE
pollution fixed in commit dede8d62.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch


PROD_STATE_FILES = [
    "/root/atlas/brokers/state/live_sp500.json",
    "/root/atlas/brokers/state/live_commodity_etfs.json",
    "/root/atlas/brokers/state/live_sector_etfs.json",
]


def test_state_dir_does_not_point_to_production() -> None:
    """_STATE_DIR must NOT be the production path during any test."""
    import brokers.live_portfolio as _lp

    sd = str(_lp._STATE_DIR)
    assert sd != "/root/atlas/brokers/state", (
        f"Test isolation broken — _STATE_DIR still points to production: {sd}"
    )
    # Should be somewhere in the tmp dir hierarchy
    assert (
        "/tmp" in sd
        or "pytest" in sd
    ), f"_STATE_DIR should be in a tmp dir, got: {sd}"


def test_state_path_follows_state_dir(tmp_path) -> None:
    """LivePortfolio._state_path() must return a path under _STATE_DIR."""
    import brokers.live_portfolio as _lp
    from brokers.live_portfolio import LivePortfolio

    # Autouse fixture has already redirected _STATE_DIR to tmp_path/lp_state
    with patch.object(LivePortfolio, "_load_local_state", return_value=None):
        lp = LivePortfolio({"risk": {}, "fees": {}}, market_id="sp500")

    state_path = lp._state_path()
    state_dir = str(_lp._STATE_DIR)

    assert str(state_path).startswith(state_dir), (
        f"_state_path() {state_path} does not start with _STATE_DIR {state_dir}"
    )
    assert str(state_path) != "/root/atlas/brokers/state/live_sp500.json", (
        f"_state_path() returned production path — isolation broken"
    )


def test_save_state_during_test_does_not_touch_production(tmp_path) -> None:
    """LivePortfolio.save_state() during test must write to tmp, not production."""
    import brokers.live_portfolio as _lp
    from brokers.live_portfolio import LivePortfolio

    # Record production file mtimes before the save
    pre_mtimes = {
        f: os.path.getmtime(f) for f in PROD_STATE_FILES if os.path.exists(f)
    }

    cfg = {
        "risk": {
            "starting_equity": 5000,
            "max_risk_per_trade_pct": 0.005,
            "max_open_positions": 10,
            "max_sector_concentration": 2,
            "max_daily_drawdown_pct": 0.02,
            "leverage": 1.0,
        },
        "fees": {},
        "dual_write_market_state": False,  # skip SQLite to keep test simple
    }

    with patch.object(LivePortfolio, "_load_local_state", return_value=None):
        lp = LivePortfolio(cfg, market_id="sp500")

    lp.broker_data_valid = True
    lp.positions = []

    # This should write to _STATE_DIR (tmp), NOT to production
    with patch.object(lp, "_trigger_dashboard_refresh"):
        lp.save_state()

    # Production files must be untouched
    for f, pre_mtime in pre_mtimes.items():
        cur_mtime = os.path.getmtime(f)
        assert cur_mtime == pre_mtime, (
            f"save_state() touched production file {f} — isolation broken! "
            f"mtime changed {pre_mtime:.3f} → {cur_mtime:.3f}"
        )

    # Written file must be in _STATE_DIR (tmp), not production
    written_path = lp._state_path()
    assert written_path.exists(), (
        f"Expected state file to be written to {written_path}"
    )
    assert str(written_path) != "/root/atlas/brokers/state/live_sp500.json", (
        "save_state() wrote to production path — isolation broken"
    )

    # And the written file must have positions=[] (what we set)
    state = json.loads(written_path.read_text())
    assert state["positions"] == [], (
        f"Unexpected positions in written state: {state['positions']}"
    )
