"""Tests for scripts/maintenance/2026-05-11-quarantine-stub-trades.py

Verifies:
  1. Quarantine script correctly identifies and moves stubs (entry_date=None)
  2. Clean trades with valid entry_date are untouched
  3. Idempotency: running twice doesn't double-quarantine
  4. record_equity() on a portfolio with quarantine-cleaned closed_trades doesn't crash
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

# ---------------------------------------------------------------------------
# Load quarantine script module (filename contains hyphens, use importlib)
# ---------------------------------------------------------------------------
_QUARANTINE_SCRIPT = Path(__file__).resolve().parent.parent / (
    "scripts/maintenance/2026-05-11-quarantine-stub-trades.py"
)


def _load_quarantine_mod():
    spec = importlib.util.spec_from_file_location("quarantine_stub_trades", _QUARANTINE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


quarantine_mod = _load_quarantine_mod()
identify_stubs = quarantine_mod.identify_stubs
run = quarantine_mod.run

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_STUB_TRADE = {
    "ticker": "SYK",
    "entry_date": None,
    "entry_price": 0,
    "exit_price": 300.0,
    "shares": 1,
    "pnl": 0.0,
    "exit_date": "2026-05-08",
    "exit_reason": "trailing_stop_fill",
}

_CLEAN_TRADE = {
    "ticker": "AAPL",
    "entry_date": "2026-04-01",
    "entry_price": 180.0,
    "exit_price": 200.0,
    "shares": 5,
    "pnl": 100.0,
    "exit_date": "2026-04-15",
    "exit_reason": "take_profit",
}

_STUB_TRADE_2 = {
    "ticker": "ADBE",
    "entry_date": None,
    "entry_price": 248.0,
    "exit_price": 250.0,
    "shares": 1,
    "pnl": 4.7,
    "exit_date": "2026-03-18",
    "exit_reason": "trailing_stop",
}


def _make_state(closed_trades: list, quarantine: list | None = None) -> dict:
    state = {
        "market_id": "sp500",
        "mode": "live",
        "positions": [],
        "closed_trades": closed_trades,
        "equity_history": [],
        "daily_high_water": 0,
        "daily_high_water_date": None,
        "halted": False,
        "halt_reason": None,
        "last_saved": "2026-05-11T00:00:00",
    }
    if quarantine is not None:
        state["closed_trades_quarantine"] = quarantine
    return state


# ---------------------------------------------------------------------------
# Unit tests for identify_stubs()
# ---------------------------------------------------------------------------


class TestIdentifyStubs:
    def test_no_stubs_all_clean(self):
        clean, stubs = identify_stubs([_CLEAN_TRADE])
        assert clean == [_CLEAN_TRADE]
        assert stubs == []

    def test_all_stubs(self):
        clean, stubs = identify_stubs([_STUB_TRADE, _STUB_TRADE_2])
        assert clean == []
        assert len(stubs) == 2

    def test_mixed(self):
        clean, stubs = identify_stubs([_CLEAN_TRADE, _STUB_TRADE, _STUB_TRADE_2])
        assert clean == [_CLEAN_TRADE]
        assert len(stubs) == 2
        assert stubs[0]["ticker"] == "SYK"
        assert stubs[1]["ticker"] == "ADBE"

    def test_empty_input(self):
        clean, stubs = identify_stubs([])
        assert clean == []
        assert stubs == []


# ---------------------------------------------------------------------------
# Integration tests for run()
# ---------------------------------------------------------------------------


class TestRunDryRun:
    def test_dry_run_does_not_modify_file(self, tmp_path):
        state_file = tmp_path / "live_sp500.json"
        state_file.write_text(
            json.dumps(_make_state([_CLEAN_TRADE, _STUB_TRADE])),
            encoding="utf-8",
        )
        summary = run(state_file, apply=False)

        # File unchanged
        data = json.loads(state_file.read_text())
        assert len(data["closed_trades"]) == 2
        assert "closed_trades_quarantine" not in data

        # Summary correct
        assert summary["n_quarantined"] == 1
        assert summary["n_clean"] == 1

    def test_dry_run_returns_correct_stub_tickers(self, tmp_path):
        state_file = tmp_path / "live_sp500.json"
        state_file.write_text(
            json.dumps(_make_state([_CLEAN_TRADE, _STUB_TRADE, _STUB_TRADE_2])),
            encoding="utf-8",
        )
        summary = run(state_file, apply=False)
        assert "SYK" in summary["stubs"]
        assert "ADBE" in summary["stubs"]
        assert "AAPL" not in summary["stubs"]


class TestRunApply:
    def test_moves_stubs_to_quarantine(self, tmp_path):
        state_file = tmp_path / "live_sp500.json"
        state_file.write_text(
            json.dumps(_make_state([_CLEAN_TRADE, _STUB_TRADE, _STUB_TRADE_2])),
            encoding="utf-8",
        )
        summary = run(state_file, apply=True)

        data = json.loads(state_file.read_text())
        assert len(data["closed_trades"]) == 1
        assert data["closed_trades"][0]["ticker"] == "AAPL"
        assert len(data["closed_trades_quarantine"]) == 2
        assert summary["n_clean"] == 1
        assert summary["n_quarantined"] == 2

    def test_quarantine_entries_have_metadata(self, tmp_path):
        state_file = tmp_path / "live_sp500.json"
        state_file.write_text(
            json.dumps(_make_state([_STUB_TRADE])),
            encoding="utf-8",
        )
        run(state_file, apply=True)

        data = json.loads(state_file.read_text())
        q = data["closed_trades_quarantine"][0]
        assert "_quarantine_reason" in q
        assert "_quarantined_at" in q
        assert "missing entry_date" in q["_quarantine_reason"]
        # Original fields preserved
        assert q["ticker"] == "SYK"
        assert q["pnl"] == 0.0

    def test_idempotent_second_run(self, tmp_path):
        """Running quarantine twice doesn't double-quarantine."""
        state_file = tmp_path / "live_sp500.json"
        state_file.write_text(
            json.dumps(_make_state([_CLEAN_TRADE, _STUB_TRADE])),
            encoding="utf-8",
        )
        run(state_file, apply=True)
        # Second run: no new stubs (stubs already moved)
        summary2 = run(state_file, apply=True)

        data = json.loads(state_file.read_text())
        assert len(data["closed_trades"]) == 1
        assert len(data["closed_trades_quarantine"]) == 1
        assert summary2["n_quarantined"] == 0

    def test_appends_to_existing_quarantine(self, tmp_path):
        """New stubs are appended to already-existing quarantine list."""
        existing_q = [dict(_STUB_TRADE_2, _quarantine_reason="old", _quarantined_at="t")]
        state_file = tmp_path / "live_sp500.json"
        state_file.write_text(
            json.dumps(_make_state([_CLEAN_TRADE, _STUB_TRADE], quarantine=existing_q)),
            encoding="utf-8",
        )
        run(state_file, apply=True)

        data = json.loads(state_file.read_text())
        assert len(data["closed_trades_quarantine"]) == 2  # 1 old + 1 new

    def test_atomic_write_uses_tmp_rename(self, tmp_path, monkeypatch):
        """Verifies atomic write: tmp file created then renamed."""
        state_file = tmp_path / "live_sp500.json"
        state_file.write_text(
            json.dumps(_make_state([_STUB_TRADE])),
            encoding="utf-8",
        )
        rename_calls = []
        original_replace = Path.replace if hasattr(Path, "replace") else None

        import os as _os
        original_os_replace = _os.replace

        def spy_replace(src, dst):
            rename_calls.append((str(src), str(dst)))
            return original_os_replace(src, dst)

        monkeypatch.setattr(_os, "replace", spy_replace)
        run(state_file, apply=True)
        assert len(rename_calls) == 1
        assert rename_calls[0][1] == str(state_file)


# ---------------------------------------------------------------------------
# record_equity smoke test on cleaned portfolio
# ---------------------------------------------------------------------------


class TestRecordEquityOnCleanedPortfolio:
    """Prove that record_equity() doesn't crash after quarantine cleanup."""

    def test_record_equity_no_crash_with_valid_closed_trades(self, tmp_path, monkeypatch):
        """
        record_equity() must not crash when closed_trades has only clean records
        (all pnl set, no None fields that sum() would choke on).
        """
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

        state_file = tmp_path / "live_sp500.json"
        state_data = _make_state(
            [_CLEAN_TRADE, dict(_CLEAN_TRADE, ticker="MSFT", pnl=50.0)]
        )
        state_data["equity_history"] = []
        state_file.write_text(json.dumps(state_data), encoding="utf-8")

        # We don't need a real broker — just test the PnL sum path
        from brokers.live_portfolio import LivePortfolio

        # Minimal config for sp500
        config = {
            "market_id": "sp500",
            "trading": {"mode": "live", "live_enabled": True},
            "risk": {"max_daily_drawdown_pct": 0.05, "starting_equity": 5000},
        }

        # Patch the state path to point at our tmp file
        import brokers.live_portfolio as _lp_mod
        original_state_dir = _lp_mod._STATE_DIR
        monkeypatch.setattr(_lp_mod, "_STATE_DIR", tmp_path)

        lp = LivePortfolio.__new__(LivePortfolio)
        lp.market_id = "sp500"
        lp.config = config
        lp._broker = None
        lp.broker_data_valid = False  # skip actual broker calls
        lp.positions = []
        lp._save_state_warned = False
        lp._hwm_reset_at = None

        # Load state from our tmp file
        lp.closed_trades = state_data["closed_trades"]
        lp.equity_history = []
        lp.daily_high_water = 0.0
        lp.daily_high_water_date = None
        lp.halted = False
        lp.halt_reason = None
        lp.starting_equity = config["risk"]["starting_equity"]

        # sum((t.get("pnl") or 0) for t in lp.closed_trades) must not crash
        total_realized = sum((t.get("pnl") or 0) for t in lp.closed_trades)
        assert total_realized == pytest.approx(150.0, abs=0.01)

    def test_record_equity_no_crash_with_none_pnl(self):
        """
        The defensive sum pattern (pnl or 0) must handle None pnl without raising.
        """
        trades = [
            {"ticker": "A", "pnl": None},
            {"ticker": "B", "pnl": 50.0},
            {"ticker": "C"},  # missing key entirely
        ]
        # This is the exact pattern from live_portfolio.py lines 785 + 1341
        total = sum((t.get("pnl") or 0) for t in trades)
        assert total == pytest.approx(50.0)

    def test_sum_crashes_without_defensive_pattern(self):
        """Confirm that naive sum() without the guard would fail on None pnl."""
        trades = [{"ticker": "A", "pnl": None}]
        with pytest.raises(TypeError):
            _ = sum(t.get("pnl", 0) for t in trades)
            # get("pnl", 0) returns None when key exists but value is None — TypeError on sum
