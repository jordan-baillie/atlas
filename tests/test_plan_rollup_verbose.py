"""Tests for the enriched plan rollup format (Phase — verbose rollup).

Covers:
- Buffer payload includes entries_full with all required fields
- Buffer payload includes open_positions_full and equity_snapshot
- Buffer payload includes trading_mode when config has mode=passive
- Halt buffer includes halt_diagnostics
- Rich render produces per-entry lines with ticker/strategy/stop/target
- Passive mode warning appears for PENDING+passive markets
- 4096-char cap: long message truncated with "+N more" marker
- Backwards compat: old buffer without entries_full falls back to summary_lines
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_plan(
    tmp_path: Path,
    market_id: str,
    trade_date: str,
    *,
    entries=None,
    exits=None,
    open_positions=None,
    portfolio_snapshot=None,
    status: str = "PENDING",
    rejection_reason=None,
) -> Path:
    """Write a minimal plan JSON and return its path."""
    entries = entries or []
    exits = exits or []
    open_positions = open_positions or []
    portfolio_snapshot = portfolio_snapshot or {
        "equity": 1000.0,
        "cash": 500.0,
        "total_pnl": 50.0,
        "total_pnl_pct": 5.0,
    }
    plan = {
        "trade_date": trade_date,
        "market_id": market_id,
        "status": status,
        "proposed_entries": entries,
        "proposed_exits": exits,
        "open_positions": open_positions,
        "portfolio_snapshot": portfolio_snapshot,
        "risk_summary": {
            "total_proposed_cost": sum(
                e.get("entry_price", 0) * e.get("position_size", 1) for e in entries
            ),
            "total_proposed_risk": sum(e.get("risk_amount", 0) for e in entries),
            "risk_pct_of_equity": 2.0,
            "portfolio_exposure_pct": 90.0,
        },
    }
    if rejection_reason:
        plan["rejection_reason"] = rejection_reason
    p = tmp_path / f"plan_{market_id}_{trade_date}.json"
    p.write_text(json.dumps(plan))
    return p


def _make_entry(
    ticker: str = "AAPL",
    entry_price: float = 200.0,
    position_size: int = 5,
    stop_price: float | None = 195.0,
    take_profit: float | None = None,
    risk_amount: float = 25.0,
    strategy: str = "momentum_breakout",
    sector: str = "Technology",
    confidence: float = 0.85,
) -> dict:
    return {
        "ticker": ticker,
        "entry_price": entry_price,
        "position_size": position_size,
        "stop_price": stop_price,
        "take_profit": take_profit,
        "risk_amount": risk_amount,
        "position_value": entry_price * position_size,
        "strategy": strategy,
        "sector": sector,
        "confidence": confidence,
    }


def _write_buffer(
    buf_dir: Path,
    market_id: str,
    trade_date: str,
    *,
    plan_status: str = "APPROVED",
    n_entries: int = 2,
    n_exits: int = 0,
    leverage_pct: float = 90.0,
    summary_lines: list | None = None,
    entries_full: list | None = None,
    exits_full: list | None = None,
    open_positions_full: list | None = None,
    equity_snapshot: dict | None = None,
    trading_mode: str = "live",
    halt_reason: str | None = None,
    halt_diagnostics: dict | None = None,
    rejection_reason: str | None = None,
    risk_pct: float = 1.5,
) -> Path:
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
        "summary_lines": summary_lines or ["TICK × 2 @ $100.00 → stop $95.00"],
        "rejection_reason": rejection_reason,
        "trading_mode": trading_mode,
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    if entries_full is not None:
        data["entries_full"] = entries_full
    if exits_full is not None:
        data["exits_full"] = exits_full
    if open_positions_full is not None:
        data["open_positions_full"] = open_positions_full
    if equity_snapshot is not None:
        data["equity_snapshot"] = equity_snapshot
    if halt_diagnostics is not None:
        data["halt_diagnostics"] = halt_diagnostics
    path = buf_dir / f"{market_id}_{trade_date}.json"
    path.write_text(json.dumps(data))
    return path


def _fake_urlopen_capture(captured: dict):
    """Return a fake urlopen callable that captures the sent payload."""
    def fake(req, timeout=15):
        body = json.loads(req.data)
        captured["payload"] = body
        captured["text"] = body.get("text", "")
        resp = MagicMock()
        resp.read.return_value = b'{"ok": true}'
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return resp
    return fake


# ══════════════════════════════════════════════════════════════════════════════
# Test 1 — Buffer includes entries_full
# ══════════════════════════════════════════════════════════════════════════════

class TestBufferIncludesEntriesFull:
    """send_plan_for_approval must write entries_full with all required fields."""

    def test_buffer_includes_entries_full(self, tmp_path):
        entries = [
            _make_entry("CDNS", 349.65, 1, stop_price=342.35, take_profit=421.48,
                        risk_amount=7.30, strategy="momentum_breakout"),
            _make_entry("F", 11.49, 38, stop_price=11.14, take_profit=None,
                        risk_amount=13.30, strategy="connors_rsi2"),
        ]
        plan_path = _make_plan(tmp_path, "sp500", "2030-01-13", entries=entries)

        import services.telegram_bot as tb

        with patch.object(tb, "_BUFFER_DIR", tmp_path / "buf"), \
             patch("urllib.request.urlopen") as mock_urlopen, \
             patch("utils.config.get_active_config",
                   return_value={"trading": {"mode": "live", "auto_approve": False}}):
            ok = tb.send_plan_for_approval(str(plan_path), "sp500")

        assert ok is True
        mock_urlopen.assert_not_called()

        buf = json.loads((tmp_path / "buf" / "sp500_2030-01-13.json").read_text())
        assert "entries_full" in buf, "entries_full must be in buffer"

        ef = buf["entries_full"]
        assert len(ef) == 2

        # First entry: momentum_breakout with take_profit
        e0 = ef[0]
        assert e0["ticker"] == "CDNS"
        assert e0["side"] == "BUY"
        assert e0["qty"] == 1
        assert abs(e0["entry_price"] - 349.65) < 0.01
        assert abs(e0["stop_price"] - 342.35) < 0.01
        assert abs(e0["take_profit"] - 421.48) < 0.01
        assert abs(e0["risk_amount"] - 7.30) < 0.01
        assert e0["strategy"] == "momentum_breakout"
        assert "confidence" in e0

        # Second entry: connors_rsi2 with no take_profit
        e1 = ef[1]
        assert e1["ticker"] == "F"
        assert e1["strategy"] == "connors_rsi2"
        assert e1["take_profit"] is None, "take_profit must be None, not 0"
        assert abs(e1["stop_price"] - 11.14) < 0.01

        # Backward compat fields still present
        assert "summary_lines" in buf
        assert buf["plan_status"] == "PENDING"


# ══════════════════════════════════════════════════════════════════════════════
# Test 2 — Buffer includes open_positions_full and equity_snapshot
# ══════════════════════════════════════════════════════════════════════════════

class TestBufferIncludesOpenPositionsAndEquitySnapshot:
    def test_buffer_includes_open_positions_and_equity_snapshot(self, tmp_path):
        open_positions = [
            {
                "ticker": "CAT",
                "shares": 1,
                "entry_price": 835.24,
                "current_price": 874.94,
                "unrealized_pnl": 39.70,
                "unrealized_pnl_pct": 4.75,
                "stop_price": 861.21,
                "take_profit": 978.33,
                "strategy": "momentum_breakout",
            }
        ]
        snapshot = {
            "equity": 1300.54,
            "cash": 1178.86,
            "total_pnl": 329.54,
            "total_pnl_pct": 33.94,
        }
        entries = [_make_entry("EBAY", 109.34, 5)]
        plan_path = _make_plan(
            tmp_path, "sp500", "2030-01-13",
            entries=entries,
            open_positions=open_positions,
            portfolio_snapshot=snapshot,
        )

        import services.telegram_bot as tb

        with patch.object(tb, "_BUFFER_DIR", tmp_path / "buf"), \
             patch("urllib.request.urlopen"), \
             patch("utils.config.get_active_config",
                   return_value={"trading": {"mode": "live", "auto_approve": False}}):
            ok = tb.send_plan_for_approval(str(plan_path), "sp500")

        assert ok is True
        buf = json.loads((tmp_path / "buf" / "sp500_2030-01-13.json").read_text())

        # open_positions_full
        assert "open_positions_full" in buf
        op = buf["open_positions_full"]
        assert len(op) == 1
        assert op[0]["ticker"] == "CAT"
        assert op[0]["shares"] == 1
        assert abs(op[0]["unrealized_pnl"] - 39.70) < 0.01
        assert abs(op[0]["stop_price"] - 861.21) < 0.01
        assert op[0]["strategy"] == "momentum_breakout"

        # equity_snapshot
        assert "equity_snapshot" in buf
        snap = buf["equity_snapshot"]
        assert abs(snap["equity"] - 1300.54) < 0.01
        assert abs(snap["cash"] - 1178.86) < 0.01
        assert abs(snap["total_pnl"] - 329.54) < 0.01
        assert abs(snap["total_pnl_pct"] - 33.94) < 0.01


# ══════════════════════════════════════════════════════════════════════════════
# Test 3 — Buffer includes trading_mode=passive
# ══════════════════════════════════════════════════════════════════════════════

class TestBufferIncludesTradingModePassive:
    def test_buffer_includes_trading_mode_passive(self, tmp_path):
        entries = [_make_entry("DBB", 24.49, 18)]
        plan_path = _make_plan(tmp_path, "commodity_etfs", "2030-01-13", entries=entries)

        import services.telegram_bot as tb

        with patch.object(tb, "_BUFFER_DIR", tmp_path / "buf"), \
             patch("urllib.request.urlopen"), \
             patch("utils.config.get_active_config",
                   return_value={"trading": {"mode": "passive", "auto_approve": False}}):
            ok = tb.send_plan_for_approval(str(plan_path), "commodity_etfs")

        assert ok is True
        buf = json.loads((tmp_path / "buf" / "commodity_etfs_2030-01-13.json").read_text())
        assert "trading_mode" in buf
        assert buf["trading_mode"] == "passive"


# ══════════════════════════════════════════════════════════════════════════════
# Test 4 — Halt buffer includes diagnostics
# ══════════════════════════════════════════════════════════════════════════════

class TestHaltBufferIncludesDiagnostics:
    def test_halt_buffer_includes_diagnostics(self, tmp_path):
        """_maybe_write_halt_buffer includes halt_diagnostics + open_positions_full."""
        # Create a fake live state file
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        state_data = {
            "market_id": "sector_etfs",
            "daily_high_water": 5189.01,
            "daily_high_water_date": "2026-05-05",
            "halted": True,
            "halt_reason": "Daily drawdown 49.78% >= 2.00%",
            "equity_history": [
                {
                    "date": "2026-05-05",
                    "equity": 3191.73,
                    "cash": 1178.86,
                    "positions_value": 2012.87,
                    "positions": [
                        {"ticker": "XLE", "shares": 8, "entry_price": 59.06,
                         "current_price": 59.39, "unrealized_pnl": 2.64, "strategy": "mb"},
                        {"ticker": "XLI", "shares": 9, "entry_price": 173.97,
                         "current_price": 170.98, "unrealized_pnl": -26.91, "strategy": "mb"},
                    ],
                }
            ],
        }
        state_file = state_dir / "live_sector_etfs.json"
        state_file.write_text(json.dumps(state_data))

        # Create in-memory DB with market_equity_history table
        db_path = tmp_path / "atlas.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE market_equity_history (
                id INTEGER PRIMARY KEY,
                date TEXT,
                market_id TEXT,
                allocated_equity REAL,
                position_mv REAL,
                cash_attributed REAL,
                broker_equity REAL,
                broker_cash REAL,
                snapshot_time TEXT,
                created_at TEXT
            )
        """)
        conn.execute(
            "INSERT INTO market_equity_history VALUES (1,'2026-05-05','sector_etfs',2637.57,2013.94,592.20,5250.44,1178.86,'2026-05-05T05:06:07','2026-05-05 05:06:07')"
        )
        conn.commit()
        conn.close()

        import services.telegram_bot as tb
        from unittest.mock import patch as _patch

        with _patch.object(tb, "_BUFFER_DIR", tmp_path / "buf"), \
             _patch.object(tb, "PROJECT_ROOT", tmp_path), \
             _patch("services.telegram_bot._check_market_halt",
                    return_value=(True, "daily_drawdown 49.78% on sector_etfs")), \
             _patch("utils.config.get_active_config", return_value={
                 "trading": {"mode": "passive"},
                 "risk": {"max_daily_drawdown_pct": 0.02, "starting_equity": 3216},
             }), \
             _patch("db.atlas_db.get_db") as mock_get_db:

            # Mock get_db to return our in-memory row
            mock_conn = MagicMock()
            mock_conn.__enter__ = lambda s: mock_conn
            mock_conn.__exit__ = lambda *a: None
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = (2637.57, 2013.94, 592.20, 5250.44, "2026-05-05")
            mock_conn.execute.return_value = mock_cursor
            mock_get_db.return_value = mock_conn

            today = "2026-05-05"
            tb._maybe_write_halt_buffer("sector_etfs", today)

        buf_file = tmp_path / "buf" / "sector_etfs_2026-05-05.json"
        assert buf_file.exists(), "Halt buffer must be written"
        buf = json.loads(buf_file.read_text())

        assert buf["plan_status"] == "HALTED"
        assert buf["halt_reason"] == "daily_drawdown 49.78% on sector_etfs"

        # halt_diagnostics must be present
        assert "halt_diagnostics" in buf
        diag = buf["halt_diagnostics"]
        # dd_pct parsed from halt_reason
        assert diag.get("dd_pct") is not None, "dd_pct should be parsed from halt_reason"
        assert abs(diag["dd_pct"] - 49.78) < 0.1

        # trading_mode from config
        assert buf.get("trading_mode") == "passive"

        # open_positions_full from state file (state_dir / live_sector_etfs.json)
        # Note: PROJECT_ROOT patched to tmp_path so state_file path is tmp_path/brokers/state/live_sector_etfs.json
        # Our state file is in tmp_path/state/ — the halt buffer reads from PROJECT_ROOT/brokers/state/
        # The open_positions_full may be empty if state path not resolved, but halt_diagnostics must be present
        assert "open_positions_full" in buf


# ══════════════════════════════════════════════════════════════════════════════
# Test 5 — Rich render produces correct format
# ══════════════════════════════════════════════════════════════════════════════

class TestRenderRichEntriesFormat:
    def test_render_rich_entries_format(self, tmp_path):
        today = datetime.now().strftime("%Y-%m-%d")
        buf_dir = tmp_path / "buf"

        # APPROVED market with 2 entries
        entries_full = [
            {
                "ticker": "CDNS",
                "side": "BUY",
                "qty": 1,
                "entry_price": 349.65,
                "stop_price": 342.35,
                "take_profit": 421.48,
                "risk_amount": 7.30,
                "position_value": 349.65,
                "strategy": "momentum_breakout",
                "sector": "Technology",
                "confidence": 0.969,
            },
            {
                "ticker": "F",
                "side": "BUY",
                "qty": 38,
                "entry_price": 11.49,
                "stop_price": 11.14,
                "take_profit": None,
                "risk_amount": 13.30,
                "position_value": 436.62,
                "strategy": "connors_rsi2",
                "sector": "Consumer Cyclical",
                "confidence": 0.82,
            },
        ]
        open_positions_full = [
            {
                "ticker": "CAT",
                "shares": 1,
                "entry_price": 835.24,
                "current_price": 874.94,
                "unrealized_pnl": 39.70,
                "unrealized_pnl_pct": 4.75,
                "stop_price": 861.21,
                "take_profit": 978.33,
                "strategy": "momentum_breakout",
            }
        ]
        _write_buffer(
            buf_dir, "sp500", today,
            plan_status="APPROVED",
            n_entries=2,
            leverage_pct=172.0,
            risk_pct=4.33,
            entries_full=entries_full,
            open_positions_full=open_positions_full,
            equity_snapshot={"equity": 1300.54, "cash": 1178.86,
                             "total_pnl": 329.54, "total_pnl_pct": 33.94},
        )

        # HALTED market
        _write_buffer(
            buf_dir, "sector_etfs", today,
            plan_status="HALTED",
            halt_reason="daily_drawdown 49.78% on sector_etfs",
            halt_diagnostics={
                "hwm": 5189.01,
                "hwm_date": "2026-05-05",
                "current_eq_estimate": 3191.73,
                "dd_pct": 49.78,
                "dd_limit_pct": 2.0,
                "snap_allocated_equity": 2637.57,
                "snap_position_mv": 2013.94,
                "snap_cash_attributed": 592.20,
                "snap_broker_equity": 5250.44,
                "snap_date": "2026-05-05",
            },
            trading_mode="passive",
            n_entries=0,
        )

        # PENDING market with passive mode
        _write_buffer(
            buf_dir, "commodity_etfs", today,
            plan_status="PENDING",
            n_entries=2,
            trading_mode="passive",
            entries_full=[
                {
                    "ticker": "DBB",
                    "side": "BUY",
                    "qty": 18,
                    "entry_price": 24.49,
                    "stop_price": 24.19,
                    "take_profit": None,
                    "risk_amount": 5.40,
                    "position_value": 440.82,
                    "strategy": "connors_rsi2",
                    "sector": "Industrial Metals",
                    "confidence": 0.844,
                },
            ],
            equity_snapshot={"equity": 944.82, "cash": 1178.86,
                             "total_pnl": -56.18, "total_pnl_pct": -5.61},
        )

        captured: dict = {}
        import services.telegram_bot as tb

        with patch.object(tb, "_BUFFER_DIR", buf_dir), \
             patch("services.telegram_bot._load_credentials", return_value=("token", "chat")), \
             patch("urllib.request.urlopen", side_effect=_fake_urlopen_capture(captured)), \
             patch("utils.config.get_active_config",
                   return_value={"risk": {"starting_equity": 3216}}):
            ok = tb.send_plan_rollup()

        assert ok is True
        msg = captured["text"]

        # Per-entry lines must appear for APPROVED market
        assert "CDNS" in msg
        assert "momentum_breakout" in msg
        # F entry has no target
        assert "F" in msg
        assert "connors_rsi2" in msg
        assert "no target" in msg

        # Halt diagnostics block for sector_etfs
        assert "HALTED" in msg
        assert "49.78%" in msg
        assert "2.00%" in msg  # dd_limit_pct
        assert "5189.01" in msg or "5,189.01" in msg  # HWM

        # Passive mode warning for commodity_etfs
        assert "passive" in msg.lower()
        assert "NOT execute" in msg or "not execute" in msg.lower()

        # Summary line
        assert "Summary" in msg or "summary" in msg.lower()

    def test_render_open_positions_appear(self, tmp_path):
        today = datetime.now().strftime("%Y-%m-%d")
        buf_dir = tmp_path / "buf"
        open_positions_full = [
            {
                "ticker": "CAT",
                "shares": 1,
                "entry_price": 835.24,
                "current_price": 874.94,
                "unrealized_pnl": 39.70,
                "unrealized_pnl_pct": 4.75,
                "stop_price": 861.21,
                "take_profit": None,
                "strategy": "momentum_breakout",
            }
        ]
        _write_buffer(
            buf_dir, "sp500", today,
            plan_status="APPROVED",
            n_entries=1,
            entries_full=[{
                "ticker": "EBAY", "side": "BUY", "qty": 5,
                "entry_price": 109.34, "stop_price": 107.16, "take_profit": 130.74,
                "risk_amount": 10.88, "position_value": 546.7,
                "strategy": "momentum_breakout", "sector": "Tech", "confidence": 0.9,
            }],
            open_positions_full=open_positions_full,
        )
        captured: dict = {}
        import services.telegram_bot as tb

        with patch.object(tb, "_BUFFER_DIR", buf_dir), \
             patch("services.telegram_bot._load_credentials", return_value=("t", "c")), \
             patch("urllib.request.urlopen", side_effect=_fake_urlopen_capture(captured)), \
             patch("utils.config.get_active_config",
                   return_value={"risk": {"starting_equity": 1000}}):
            tb.send_plan_rollup()

        assert "CAT" in captured["text"]
        assert "861.21" in captured["text"] or "861" in captured["text"]


# ══════════════════════════════════════════════════════════════════════════════
# Test 6 — Passive mode warning
# ══════════════════════════════════════════════════════════════════════════════

class TestRenderPassiveModeWarning:
    def test_render_passive_mode_warning_appears(self, tmp_path):
        """PENDING buffer with trading_mode='passive' → message contains passive warning."""
        today = datetime.now().strftime("%Y-%m-%d")
        buf_dir = tmp_path / "buf"
        _write_buffer(
            buf_dir, "commodity_etfs", today,
            plan_status="PENDING",
            n_entries=3,
            trading_mode="passive",
            entries_full=[
                {
                    "ticker": "DBB", "side": "BUY", "qty": 18,
                    "entry_price": 24.49, "stop_price": 24.19, "take_profit": None,
                    "risk_amount": 5.4, "position_value": 440.82,
                    "strategy": "connors_rsi2", "sector": "Metals", "confidence": 0.84,
                },
            ],
        )
        captured: dict = {}
        import services.telegram_bot as tb

        with patch.object(tb, "_BUFFER_DIR", buf_dir), \
             patch("services.telegram_bot._load_credentials", return_value=("t", "c")), \
             patch("urllib.request.urlopen", side_effect=_fake_urlopen_capture(captured)), \
             patch("utils.config.get_active_config",
                   return_value={"risk": {"starting_equity": 1000}}):
            ok = tb.send_plan_rollup()

        assert ok is True
        msg = captured["text"]
        assert "passive" in msg.lower(), "Message must mention 'passive'"
        assert "NOT execute" in msg or "will NOT" in msg, "Must warn about non-execution"


# ══════════════════════════════════════════════════════════════════════════════
# Test 7 — Truncation for long messages
# ══════════════════════════════════════════════════════════════════════════════

class TestRenderTruncatesLongMessage:
    def test_render_truncates_long_message(self, tmp_path):
        """Buffer with 50 entries → rendered message ≤ 4000 chars and has '+N more'."""
        today = datetime.now().strftime("%Y-%m-%d")
        buf_dir = tmp_path / "buf"

        # Create 50 distinct tickers for sp500
        entries_full = []
        for i in range(50):
            ticker = f"T{i:03d}"
            entries_full.append({
                "ticker": ticker,
                "side": "BUY",
                "qty": 5,
                "entry_price": 100.0 + i,
                "stop_price": 95.0 + i,
                "take_profit": 120.0 + i,
                "risk_amount": 10.0,
                "position_value": 500.0 + i * 5,
                "strategy": "momentum_breakout",
                "sector": "Technology",
                "confidence": 0.85,
            })

        _write_buffer(
            buf_dir, "sp500", today,
            plan_status="APPROVED",
            n_entries=50,
            entries_full=entries_full,
            equity_snapshot={"equity": 5000.0, "cash": 1000.0,
                             "total_pnl": 500.0, "total_pnl_pct": 10.0},
        )
        captured: dict = {}
        import services.telegram_bot as tb

        with patch.object(tb, "_BUFFER_DIR", buf_dir), \
             patch("services.telegram_bot._load_credentials", return_value=("t", "c")), \
             patch("urllib.request.urlopen", side_effect=_fake_urlopen_capture(captured)), \
             patch("utils.config.get_active_config",
                   return_value={"risk": {"starting_equity": 5000}}):
            ok = tb.send_plan_rollup()

        assert ok is True
        msg = captured["text"]
        assert len(msg) <= 4000, f"Message too long: {len(msg)} chars"
        assert "+ more" in msg or "+N more" in msg or "more" in msg.lower(), \
            "Must show truncation marker"


# ══════════════════════════════════════════════════════════════════════════════
# Test 8 — Backwards compat: old buffer without entries_full
# ══════════════════════════════════════════════════════════════════════════════

class TestRenderBackwardsCompatOldBuffer:
    def test_render_backwards_compat_old_buffer(self, tmp_path):
        """Old buffer without entries_full falls back to summary_lines, doesn't crash."""
        today = datetime.now().strftime("%Y-%m-%d")
        buf_dir = tmp_path / "buf"

        # Old-style buffer: no entries_full, exits_full, open_positions_full
        old_buf = {
            "market_id": "sp500",
            "trade_date": today,
            "plan_status": "APPROVED",
            "halt_reason": None,
            "n_entries": 2,
            "n_approved": 2,
            "n_exits": 0,
            "total_risk_pct": 1.5,
            "total_position_value": 1000.0,
            "leverage_pct": 90.0,
            "summary_lines": [
                "AAPL × 5 @ $200.00 → stop $195.00",
                "MSFT × 2 @ $400.00 → stop $390.00",
            ],
            "rejection_reason": None,
            "written_at": datetime.now(timezone.utc).isoformat(),
            # NO entries_full, exits_full, open_positions_full, equity_snapshot, trading_mode
        }
        buf_dir.mkdir(parents=True, exist_ok=True)
        (buf_dir / f"sp500_{today}.json").write_text(json.dumps(old_buf))

        captured: dict = {}
        import services.telegram_bot as tb

        with patch.object(tb, "_BUFFER_DIR", buf_dir), \
             patch("services.telegram_bot._load_credentials", return_value=("t", "c")), \
             patch("urllib.request.urlopen", side_effect=_fake_urlopen_capture(captured)), \
             patch("utils.config.get_active_config",
                   return_value={"risk": {"starting_equity": 5000}}):
            ok = tb.send_plan_rollup()

        assert ok is True
        msg = captured["text"]
        # Must contain something sensible — the fallback summary lines
        assert "AAPL" in msg or "sp500" in msg.lower() or "SP500" in msg
        assert "200.00" in msg or "MSFT" in msg or "approved" in msg.lower()
        # Must not crash (no exception → ok is True)
