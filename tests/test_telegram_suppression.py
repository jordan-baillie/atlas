"""Regression tests for Telegram notification suppression (anti-spam fixes).

These tests guard the 4 suppression rules added to fix daily Telegram spam:
1. Empty plan → no Telegram approval message
2. Postclose with no activity → no Telegram summary
3. Auto-approve of empty plan → no Telegram message
4. Execution with 0/0 and no errors → no Telegram message
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock


def test_send_plan_for_approval_suppresses_empty(tmp_path: Path):
    """Empty plan (no entries, no exits) must NOT send a Telegram message."""
    from services.telegram_bot import send_plan_for_approval

    plan = {
        "trade_date": "2026-04-27",
        "proposed_entries": [],
        "proposed_exits": [],
    }
    plan_path = tmp_path / "plan_sp500_2026-04-27.json"
    plan_path.write_text(json.dumps(plan))

    with patch("services.telegram_bot._load_credentials", return_value=("token", "chat")), \
         patch("urllib.request.urlopen") as mock_urlopen:
        result = send_plan_for_approval(str(plan_path), "sp500")
        assert result is True
        # Crucially: no HTTP request was made
        mock_urlopen.assert_not_called()


def test_send_plan_for_approval_sends_when_entries(tmp_path: Path):
    """Plan with entries: buffer file written, NO Telegram HTTP request sent.

    Phase 4 refactor: send_plan_for_approval no longer sends Telegram directly.
    It only writes a buffer file for later consolidation via send_plan_rollup().
    """
    from services.telegram_bot import send_plan_for_approval

    plan = {
        "trade_date": "2026-04-27",
        "proposed_entries": [
            {"ticker": "SPY", "entry_price": 500, "position_size": 10,
             "stop_price": 490, "strategy": "momentum"}
        ],
        "proposed_exits": [],
        "risk_summary": {"total_proposed_cost": 5000, "risk_pct_of_equity": 1.0,
                         "portfolio_exposure_pct": 80.0},
    }
    plan_path = tmp_path / "plan_sp500_2026-04-27.json"
    plan_path.write_text(json.dumps(plan))
    buf_dir = tmp_path / "buf"

    with patch("services.telegram_bot._BUFFER_DIR", buf_dir), \
         patch("urllib.request.urlopen") as mock_urlopen, \
         patch("utils.config.get_active_config", return_value={"trading": {"auto_approve": False}}):
        result = send_plan_for_approval(str(plan_path), "sp500")

    assert result is True
    # Phase 4: NO HTTP request — Telegram is deferred to send_plan_rollup()
    mock_urlopen.assert_not_called()
    # Buffer file must have been written
    buf_file = buf_dir / "sp500_2026-04-27.json"
    assert buf_file.exists(), "Buffer file must be created for non-empty plan"
    buf = json.loads(buf_file.read_text())
    assert buf["n_entries"] == 1


def test_send_postclose_summary_suppresses_empty(tmp_path, monkeypatch):
    """Postclose with no closed trades and no stop/TP exits must not send."""
    from utils import telegram as tg

    # Mock dashboard data with no closed trades today
    fake_dash = {
        "closed_trades": [],
        "markets": {},
    }
    monkeypatch.setattr(tg, "_read_dashboard_data", lambda: fake_dash)
    monkeypatch.setattr(tg, "_read_eod_summary", lambda d: {"stop_exits": 0, "tp_exits": 0, "halted": False})

    sent = {"called": False}
    def fake_send(*a, **k):
        sent["called"] = True
        return True
    monkeypatch.setattr(tg, "send_message", fake_send)

    result = tg.send_postclose_summary("sp500")
    assert result is True
    assert sent["called"] is False, "Should not have sent Telegram for empty postclose"


def test_send_postclose_summary_sends_on_closed_trade(tmp_path, monkeypatch):
    """Postclose with closed trades must send."""
    from utils import telegram as tg
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")
    fake_dash = {
        "closed_trades": [{"ticker": "SPY", "exit_date": today, "pnl": 100, "pnl_pct": 1.5, "exit_reason": "tp"}],
        "markets": {},
    }
    monkeypatch.setattr(tg, "_read_dashboard_data", lambda: fake_dash)
    monkeypatch.setattr(tg, "_read_eod_summary", lambda d: None)
    monkeypatch.setattr(tg, "_build_combined_snapshot", lambda d: "snapshot")

    sent = {"called": False}
    def fake_send(*a, **k):
        sent["called"] = True
        return True
    monkeypatch.setattr(tg, "send_message", fake_send)

    tg.send_postclose_summary("sp500")
    assert sent["called"] is True


def test_notify_auto_approve_suppresses_empty(monkeypatch):
    """_notify_auto_approve must skip when n_entries==0 and n_exits==0."""
    import scripts.execute_approved as ea

    sent = {"called": False}
    def fake_send_message(*a, **k):
        sent["called"] = True
        return True
    monkeypatch.setattr("utils.telegram.send_message", fake_send_message, raising=False)

    ea._notify_auto_approve("sp500", "2026-04-27", 0, 0)
    assert sent["called"] is False


def test_notify_auto_approve_sends_when_entries(monkeypatch):
    import scripts.execute_approved as ea

    sent = {"called": False}
    def fake_send_message(*a, **k):
        sent["called"] = True
        return True
    monkeypatch.setattr("utils.telegram.send_message", fake_send_message, raising=False)

    ea._notify_auto_approve("sp500", "2026-04-27", 2, 0)
    assert sent["called"] is True


def test_notify_execution_suppresses_zero_zero(monkeypatch):
    """_notify_execution must skip when nothing executed AND no errors."""
    import scripts.execute_approved as ea

    sent = {"called": False}
    def fake_send_message(*a, **k):
        sent["called"] = True
        return True
    monkeypatch.setattr("utils.telegram.send_message", fake_send_message, raising=False)

    report = {
        "successful_entries": 0, "successful_exits": 0,
        "total_entries": 0, "total_exits": 0,
    }
    ea._notify_execution("sp500", "2026-04-27", report)
    assert sent["called"] is False


def test_notify_execution_sends_on_errors(monkeypatch):
    """If 0/0 succeeded but there were errors (total > successful), still send."""
    import scripts.execute_approved as ea

    sent = {"called": False}
    def fake_send_message(*a, **k):
        sent["called"] = True
        return True
    monkeypatch.setattr("utils.telegram.send_message", fake_send_message, raising=False)

    report = {
        "successful_entries": 0, "successful_exits": 0,
        "total_entries": 2, "total_exits": 0,  # 2 entry attempts, 0 succeeded → 2 errors
    }
    ea._notify_execution("sp500", "2026-04-27", report)
    assert sent["called"] is True


def test_notify_execution_sends_on_success(monkeypatch):
    import scripts.execute_approved as ea

    sent = {"called": False}
    def fake_send_message(*a, **k):
        sent["called"] = True
        return True
    monkeypatch.setattr("utils.telegram.send_message", fake_send_message, raising=False)

    report = {
        "successful_entries": 1, "successful_exits": 0,
        "total_entries": 1, "total_exits": 0,
    }
    ea._notify_execution("sp500", "2026-04-27", report)
    assert sent["called"] is True
