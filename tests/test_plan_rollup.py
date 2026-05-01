"""Tests for the plan notification buffer + daily rollup (Phase 4).

Covers:
- Buffer file write produces correct JSON shape
- send_plan_for_approval no longer sends Telegram (only buffer write)
- send_plan_rollup reads all 3 markets and builds consolidated message
- send_plan_rollup handles missing markets (no buffer file → omitted)
- Idempotency: second rollup call is a no-op when sentinel exists
- 7-day TTL cleanup removes old buffer files
- Halt-aware: HALTED buffer written when market is halted + no plan file
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_plan(tmp_path: Path, market_id: str, trade_date: str, *,
               entries=None, exits=None, status="PENDING",
               rejection_reason=None) -> Path:
    """Write a minimal plan JSON and return its path."""
    entries = entries or []
    exits = exits or []
    plan = {
        "trade_date": trade_date,
        "market_id": market_id,
        "status": status,
        "proposed_entries": entries,
        "proposed_exits": exits,
        "risk_summary": {
            "total_proposed_cost": sum(e.get("entry_price", 0) * e.get("position_size", 1)
                                      for e in entries),
            "total_proposed_risk": sum(e.get("risk_amount", 0) for e in entries),
            "risk_pct_of_equity": 1.5,
            "portfolio_exposure_pct": 85.0,
        },
        "portfolio_snapshot": {"equity": 5000.0, "cash": 1000.0, "open_positions": 2},
    }
    if rejection_reason:
        plan["rejection_reason"] = rejection_reason
    p = tmp_path / f"plan_{market_id}_{trade_date}.json"
    p.write_text(json.dumps(plan))
    return p


def _mock_urlopen_ok():
    """Return a mock that simulates a successful Telegram API response."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"ok": true}'
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = lambda *a: None
    return mock_resp


# ─── Buffer write tests ────────────────────────────────────────────────────────

class TestBufferWrite:
    """send_plan_for_approval must write a JSON buffer and NOT send Telegram."""

    def test_buffer_written_for_plan_with_entries(self, tmp_path):
        """Buffer file created with correct JSON when plan has entries."""
        from services.telegram_bot import send_plan_for_approval, _BUFFER_DIR

        entries = [
            {"ticker": "AAPL", "entry_price": 200.0, "position_size": 5,
             "stop_price": 195.0, "risk_amount": 25.0, "strategy": "momentum"}
        ]
        plan_path = _make_plan(tmp_path, "sp500", "2030-01-13", entries=entries)

        with patch("services.telegram_bot._BUFFER_DIR", tmp_path / "buf"), \
             patch("urllib.request.urlopen") as mock_urlopen, \
             patch("utils.config.get_active_config",
                   return_value={"trading": {"auto_approve": False}}):
            ok = send_plan_for_approval(str(plan_path), "sp500")

        assert ok is True
        mock_urlopen.assert_not_called()  # No Telegram message sent

        buf_file = tmp_path / "buf" / "sp500_2030-01-13.json"
        assert buf_file.exists(), "Buffer file must be created"
        buf = json.loads(buf_file.read_text())

        assert buf["market_id"] == "sp500"
        assert buf["trade_date"] == "2030-01-13"
        assert buf["plan_status"] == "PENDING"
        assert buf["n_entries"] == 1
        assert buf["n_approved"] == 0
        assert buf["n_exits"] == 0
        assert buf["summary_lines"] == ["AAPL × 5 @ $200.00 → stop $195.00"]
        assert buf["rejection_reason"] is None
        assert buf["halt_reason"] is None
        assert "written_at" in buf

    def test_buffer_empty_plan_status_is_empty(self, tmp_path):
        """Empty plan → buffer with plan_status=EMPTY, no Telegram."""
        from services.telegram_bot import send_plan_for_approval

        plan_path = _make_plan(tmp_path, "commodity_etfs", "2030-01-13")

        with patch("services.telegram_bot._BUFFER_DIR", tmp_path / "buf"), \
             patch("urllib.request.urlopen") as mock_urlopen:
            ok = send_plan_for_approval(str(plan_path), "commodity_etfs")

        assert ok is True
        mock_urlopen.assert_not_called()

        buf = json.loads((tmp_path / "buf" / "commodity_etfs_2030-01-13.json").read_text())
        assert buf["plan_status"] == "EMPTY"
        assert buf["n_entries"] == 0
        assert buf["summary_lines"] == []

    def test_buffer_approved_after_auto_approve(self, tmp_path):
        """Auto-approve sets plan status → buffer records APPROVED + n_approved=n_entries."""
        from services.telegram_bot import send_plan_for_approval

        entries = [
            {"ticker": "GLD", "entry_price": 200.0, "position_size": 3,
             "stop_price": 195.0, "risk_amount": 15.0, "strategy": "trend"}
        ]
        plan_path = _make_plan(tmp_path, "sp500", "2030-01-13", entries=entries)

        # Mock approve_plan to flip status to APPROVED in the file
        def fake_approve(trade_date, *, market_id):
            plan = json.loads(plan_path.read_text())
            plan["status"] = "APPROVED"
            plan_path.write_text(json.dumps(plan))
            return plan  # truthy

        mock_plan_gen = MagicMock()
        mock_plan_gen.approve_plan.side_effect = fake_approve

        with patch("services.telegram_bot._BUFFER_DIR", tmp_path / "buf"), \
             patch("urllib.request.urlopen") as mock_urlopen, \
             patch("utils.config.get_active_config",
                   return_value={"trading": {"auto_approve": True}}), \
             patch("services.telegram_bot.TradePlanGenerator",
                   return_value=mock_plan_gen):
            ok = send_plan_for_approval(str(plan_path), "sp500")

        assert ok is True
        mock_urlopen.assert_not_called()

        buf = json.loads((tmp_path / "buf" / "sp500_2030-01-13.json").read_text())
        assert buf["plan_status"] == "APPROVED"
        assert buf["n_entries"] == 1
        assert buf["n_approved"] == 1

    def test_buffer_rejected_plan(self, tmp_path):
        """Plan with status REJECTED → buffer records REJECTED + rejection_reason."""
        from services.telegram_bot import send_plan_for_approval

        entries = [
            {"ticker": "SPY", "entry_price": 500.0, "position_size": 2,
             "stop_price": 490.0, "risk_amount": 20.0, "strategy": "momentum"}
        ]
        plan_path = _make_plan(tmp_path, "sp500", "2030-01-13", entries=entries,
                               status="REJECTED",
                               rejection_reason="leverage 150% exceeds ceiling")

        with patch("services.telegram_bot._BUFFER_DIR", tmp_path / "buf"), \
             patch("urllib.request.urlopen") as mock_urlopen, \
             patch("utils.config.get_active_config",
                   return_value={"trading": {"auto_approve": False}}):
            ok = send_plan_for_approval(str(plan_path), "sp500")

        assert ok is True
        mock_urlopen.assert_not_called()

        buf = json.loads((tmp_path / "buf" / "sp500_2030-01-13.json").read_text())
        assert buf["plan_status"] == "REJECTED"
        assert buf["rejection_reason"] == "leverage 150% exceeds ceiling"
        assert buf["n_approved"] == 0

    def test_buffer_uses_atomic_write(self, tmp_path):
        """Buffer file must not appear as .tmp on completion."""
        from services.telegram_bot import send_plan_for_approval

        entries = [{"ticker": "X", "entry_price": 10.0, "position_size": 1,
                    "stop_price": 9.0, "risk_amount": 1.0, "strategy": "s"}]
        plan_path = _make_plan(tmp_path, "sp500", "2030-01-13", entries=entries)
        buf_dir = tmp_path / "buf"

        with patch("services.telegram_bot._BUFFER_DIR", buf_dir), \
             patch("urllib.request.urlopen"), \
             patch("utils.config.get_active_config",
                   return_value={"trading": {"auto_approve": False}}):
            send_plan_for_approval(str(plan_path), "sp500")

        tmp_files = list(buf_dir.glob("*.tmp"))
        assert tmp_files == [], f"No .tmp files should remain: {tmp_files}"

    def test_missing_plan_file_returns_false(self, tmp_path):
        """If plan file missing and market not halted, return False."""
        from services.telegram_bot import send_plan_for_approval

        with patch("services.telegram_bot._BUFFER_DIR", tmp_path / "buf"), \
             patch("services.telegram_bot._check_market_halt",
                   return_value=(False, "")):
            ok = send_plan_for_approval(str(tmp_path / "nonexistent.json"), "sp500")

        assert ok is False


# ─── Halt buffer tests ─────────────────────────────────────────────────────────

class TestHaltBuffer:
    """When market is halted and plan file missing → write HALTED buffer."""

    def test_halt_buffer_written_when_halted_and_no_plan(self, tmp_path):
        """Missing plan + halted market → HALTED buffer file."""
        from services.telegram_bot import send_plan_for_approval

        with patch("services.telegram_bot._BUFFER_DIR", tmp_path / "buf"), \
             patch("services.telegram_bot._check_market_halt",
                   return_value=(True, "daily_drawdown 22.27%")):
            ok = send_plan_for_approval(
                str(tmp_path / "no_such_plan.json"), "commodity_etfs"
            )

        assert ok is False  # plan file didn't exist → still False
        today = datetime.now().strftime("%Y-%m-%d")
        buf_file = tmp_path / "buf" / f"commodity_etfs_{today}.json"
        assert buf_file.exists(), "HALTED buffer must be created"
        buf = json.loads(buf_file.read_text())
        assert buf["plan_status"] == "HALTED"
        assert buf["halt_reason"] == "daily_drawdown 22.27%"


# ─── Rollup tests ──────────────────────────────────────────────────────────────

def _write_buffer(buf_dir: Path, market_id: str, trade_date: str, *,
                  plan_status: str = "APPROVED",
                  n_entries: int = 2,
                  n_exits: int = 0,
                  leverage_pct: float = 90.0,
                  summary_lines: list | None = None,
                  rejection_reason: str | None = None,
                  halt_reason: str | None = None,
                  risk_pct: float = 1.5) -> Path:
    """Write a mock buffer file and return its path."""
    buf_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "market_id": market_id,
        "trade_date": trade_date,
        "plan_status": plan_status,
        "halt_reason": halt_reason,
        "n_entries": n_entries,
        "n_approved": n_entries if plan_status == "APPROVED" else 0,
        "n_exits": n_exits,
        "total_risk_pct": risk_pct,
        "total_position_value": 1000.0,
        "leverage_pct": leverage_pct,
        "summary_lines": summary_lines or [f"TICK × 2 @ $100.00 → stop $95.00"],
        "rejection_reason": rejection_reason,
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    path = buf_dir / f"{market_id}_{trade_date}.json"
    path.write_text(json.dumps(data))
    return path


class TestRollupMessage:
    """send_plan_rollup builds a correct consolidated message."""

    def test_rollup_three_markets_one_message(self, tmp_path):
        """3 buffer files → exactly 1 Telegram sendMessage call."""
        today = datetime.now().strftime("%Y-%m-%d")
        buf_dir = tmp_path / "buf"
        _write_buffer(buf_dir, "sp500", today, plan_status="APPROVED", n_entries=3)
        _write_buffer(buf_dir, "sector_etfs", today, plan_status="APPROVED", n_entries=1,
                      summary_lines=["XLE × 8 @ $80.00 → stop $78.00"])
        _write_buffer(buf_dir, "commodity_etfs", today, plan_status="EMPTY", n_entries=0)

        sent_payloads: list[dict] = []

        def fake_urlopen(req, timeout=15):
            import json as _json
            body = _json.loads(req.data)
            sent_payloads.append(body)
            resp = MagicMock()
            resp.read.return_value = b'{"ok": true}'
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda *a: None
            return resp

        from services.telegram_bot import send_plan_rollup
        with patch("services.telegram_bot._BUFFER_DIR", buf_dir), \
             patch("services.telegram_bot._load_credentials",
                   return_value=("token123", "chat456")), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = send_plan_rollup()

        assert ok is True
        assert len(sent_payloads) == 1, "Must send exactly ONE Telegram message"
        msg = sent_payloads[0]["text"]
        assert "sp500" in msg
        assert "sector_etfs" in msg
        assert "commodity_etfs" in msg
        assert "Daily Plans" in msg
        assert "APPROVED" in msg or "✅" in msg

    def test_rollup_message_contains_rejected_reason(self, tmp_path):
        """Rejected plan → rejection reason truncated to ~60 chars in message."""
        today = datetime.now().strftime("%Y-%m-%d")
        buf_dir = tmp_path / "buf"
        _write_buffer(buf_dir, "sp500", today, plan_status="REJECTED", n_entries=4,
                      leverage_pct=138.6,
                      rejection_reason="leverage 138.6% breaches ceiling; NFP risk")

        sent_text = {}

        def fake_urlopen(req, timeout=15):
            sent_text["msg"] = json.loads(req.data)["text"]
            resp = MagicMock()
            resp.read.return_value = b'{"ok": true}'
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda *a: None
            return resp

        from services.telegram_bot import send_plan_rollup
        with patch("services.telegram_bot._BUFFER_DIR", buf_dir), \
             patch("services.telegram_bot._load_credentials",
                   return_value=("t", "c")), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = send_plan_rollup()

        assert ok is True
        msg = sent_text["msg"]
        assert "REJECTED" in msg
        assert "leverage 138.6%" in msg
        assert "4 entries" in msg or "4 entry" in msg

    def test_rollup_message_halted_market(self, tmp_path):
        """HALTED market → rollup shows HALTED + reason."""
        today = datetime.now().strftime("%Y-%m-%d")
        buf_dir = tmp_path / "buf"
        _write_buffer(buf_dir, "commodity_etfs", today, plan_status="HALTED",
                      n_entries=0, halt_reason="daily_drawdown 22.27%")

        sent_text = {}

        def fake_urlopen(req, timeout=15):
            sent_text["msg"] = json.loads(req.data)["text"]
            resp = MagicMock()
            resp.read.return_value = b'{"ok": true}'
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda *a: None
            return resp

        from services.telegram_bot import send_plan_rollup
        with patch("services.telegram_bot._BUFFER_DIR", buf_dir), \
             patch("services.telegram_bot._load_credentials",
                   return_value=("t", "c")), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = send_plan_rollup()

        assert ok is True
        msg = sent_text["msg"]
        assert "commodity_etfs" in msg
        assert "HALTED" in msg
        assert "22.27%" in msg

    def test_rollup_single_entry_shows_ticker_detail(self, tmp_path):
        """Single-entry market shows the ticker/size/price in the rollup line."""
        today = datetime.now().strftime("%Y-%m-%d")
        buf_dir = tmp_path / "buf"
        _write_buffer(buf_dir, "sector_etfs", today, plan_status="APPROVED",
                      n_entries=1, summary_lines=["XLE × 8 @ $80.00 → stop $78.00"])

        captured = {}

        def fake_urlopen(req, timeout=15):
            captured["msg"] = json.loads(req.data)["text"]
            resp = MagicMock()
            resp.read.return_value = b'{"ok": true}'
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda *a: None
            return resp

        from services.telegram_bot import send_plan_rollup
        with patch("services.telegram_bot._BUFFER_DIR", buf_dir), \
             patch("services.telegram_bot._load_credentials",
                   return_value=("t", "c")), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = send_plan_rollup()

        assert ok is True
        assert "XLE × 8" in captured["msg"]

    def test_rollup_missing_market_omitted(self, tmp_path):
        """If a market has no buffer file, it does not appear in the rollup."""
        today = datetime.now().strftime("%Y-%m-%d")
        buf_dir = tmp_path / "buf"
        # Only write sp500 buffer; commodity_etfs and sector_etfs are absent
        _write_buffer(buf_dir, "sp500", today, plan_status="APPROVED", n_entries=2)

        captured = {}

        def fake_urlopen(req, timeout=15):
            captured["msg"] = json.loads(req.data)["text"]
            resp = MagicMock()
            resp.read.return_value = b'{"ok": true}'
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda *a: None
            return resp

        from services.telegram_bot import send_plan_rollup
        with patch("services.telegram_bot._BUFFER_DIR", buf_dir), \
             patch("services.telegram_bot._load_credentials",
                   return_value=("t", "c")), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = send_plan_rollup()

        assert ok is True
        msg = captured["msg"]
        assert "sp500" in msg
        # Missing markets are just omitted — no error line
        assert "commodity_etfs" not in msg
        assert "sector_etfs" not in msg

    def test_rollup_empty_buffer_dir_sends_no_signals(self, tmp_path):
        """No buffer files → rollup sends 'no plans' message, not failure."""
        buf_dir = tmp_path / "buf"
        buf_dir.mkdir()

        captured = {}

        def fake_urlopen(req, timeout=15):
            captured["msg"] = json.loads(req.data)["text"]
            resp = MagicMock()
            resp.read.return_value = b'{"ok": true}'
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda *a: None
            return resp

        from services.telegram_bot import send_plan_rollup
        with patch("services.telegram_bot._BUFFER_DIR", buf_dir), \
             patch("services.telegram_bot._load_credentials",
                   return_value=("t", "c")), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = send_plan_rollup()

        assert ok is True
        assert "No plans" in captured["msg"] or "Total approved" in captured["msg"]


# ─── Idempotency tests ─────────────────────────────────────────────────────────

class TestRollupIdempotency:
    """send_plan_rollup must be idempotent within the same calendar day."""

    def test_second_call_is_noop(self, tmp_path):
        """If rollup_sent_<today>.txt sentinel exists, skip send."""
        today = datetime.now().strftime("%Y-%m-%d")
        buf_dir = tmp_path / "buf"
        buf_dir.mkdir()
        sentinel = buf_dir / f"rollup_sent_{today}.txt"
        sentinel.write_text("sent earlier")

        from services.telegram_bot import send_plan_rollup
        with patch("services.telegram_bot._BUFFER_DIR", buf_dir), \
             patch("services.telegram_bot._load_credentials",
                   return_value=("t", "c")), \
             patch("urllib.request.urlopen") as mock_urlopen:
            ok = send_plan_rollup()

        assert ok is True
        mock_urlopen.assert_not_called()

    def test_sentinel_written_after_successful_send(self, tmp_path):
        """After a successful send, sentinel file must exist."""
        today = datetime.now().strftime("%Y-%m-%d")
        buf_dir = tmp_path / "buf"
        _write_buffer(buf_dir, "sp500", today, plan_status="APPROVED", n_entries=1)

        def fake_urlopen(req, timeout=15):
            resp = MagicMock()
            resp.read.return_value = b'{"ok": true}'
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda *a: None
            return resp

        from services.telegram_bot import send_plan_rollup
        with patch("services.telegram_bot._BUFFER_DIR", buf_dir), \
             patch("services.telegram_bot._load_credentials",
                   return_value=("t", "c")), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = send_plan_rollup()

        assert ok is True
        sentinel = buf_dir / f"rollup_sent_{today}.txt"
        assert sentinel.exists(), "Sentinel must be written after successful send"


# ─── TTL cleanup tests ─────────────────────────────────────────────────────────

class TestTTLCleanup:
    """Buffer files older than 7 days must be deleted by _cleanup_old_buffers."""

    def test_old_files_deleted(self, tmp_path):
        """Files with mtime older than 7 days are removed."""
        from services.telegram_bot import _cleanup_old_buffers

        buf_dir = tmp_path / "buf"
        buf_dir.mkdir()

        old_file = buf_dir / "sp500_2020-01-01.json"
        old_file.write_text('{"plan_status": "APPROVED"}')
        # Set mtime to 10 days ago
        old_ts = time.time() - (10 * 86_400)
        os.utime(str(old_file), (old_ts, old_ts))

        new_file = buf_dir / "sp500_2099-01-01.json"
        new_file.write_text('{"plan_status": "APPROVED"}')

        with patch("services.telegram_bot._BUFFER_DIR", buf_dir):
            _cleanup_old_buffers(days=7)

        assert not old_file.exists(), "Old file (10 days) must be deleted"
        assert new_file.exists(), "New file must be preserved"

    def test_recent_files_preserved(self, tmp_path):
        """Files newer than 7 days are NOT removed."""
        from services.telegram_bot import _cleanup_old_buffers

        buf_dir = tmp_path / "buf"
        buf_dir.mkdir()

        recent_file = buf_dir / "sp500_recent.json"
        recent_file.write_text('{"plan_status": "APPROVED"}')
        # 3 days old
        recent_ts = time.time() - (3 * 86_400)
        os.utime(str(recent_file), (recent_ts, recent_ts))

        with patch("services.telegram_bot._BUFFER_DIR", buf_dir):
            _cleanup_old_buffers(days=7)

        assert recent_file.exists(), "Recent file (3 days) must be preserved"

    def test_cleanup_called_on_successful_rollup(self, tmp_path):
        """_cleanup_old_buffers is called automatically on a successful rollup."""
        today = datetime.now().strftime("%Y-%m-%d")
        buf_dir = tmp_path / "buf"
        _write_buffer(buf_dir, "sp500", today, plan_status="APPROVED", n_entries=1)

        cleanup_called = {"called": False}

        def fake_cleanup(days=7):
            cleanup_called["called"] = True

        def fake_urlopen(req, timeout=15):
            resp = MagicMock()
            resp.read.return_value = b'{"ok": true}'
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda *a: None
            return resp

        from services.telegram_bot import send_plan_rollup
        with patch("services.telegram_bot._BUFFER_DIR", buf_dir), \
             patch("services.telegram_bot._load_credentials",
                   return_value=("t", "c")), \
             patch("services.telegram_bot._cleanup_old_buffers",
                   side_effect=fake_cleanup), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = send_plan_rollup()

        assert ok is True
        assert cleanup_called["called"], "_cleanup_old_buffers must be called on success"


# ─── telegram_notify.py CLI integration ───────────────────────────────────────

class TestTelegramNotifyCLI:
    """premarket-rollup command dispatches to send_plan_rollup()."""

    def test_premarket_rollup_command_dispatches(self, tmp_path):
        """Running 'premarket-rollup' via CLI calls send_plan_rollup."""
        called = {"n": 0}

        def fake_rollup():
            called["n"] += 1
            return True

        import scripts.telegram_notify as tn
        with patch("services.telegram_bot.send_plan_rollup", fake_rollup):
            import sys
            old_argv = sys.argv
            sys.argv = ["telegram_notify.py", "premarket-rollup"]
            try:
                try:
                    tn.main()
                except SystemExit as e:
                    assert e.code == 0
            finally:
                sys.argv = old_argv

        assert called["n"] == 1
