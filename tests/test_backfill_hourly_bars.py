"""Smoke tests for scripts/backfill_hourly_bars.py.

Validates target list construction without touching Alpaca / the network.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "backfill_hourly_bars.py"

# Load the script as a module (it has no .py-package nesting).
spec = importlib.util.spec_from_file_location("backfill_hourly_bars", SCRIPT_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules["backfill_hourly_bars"] = mod
spec.loader.exec_module(mod)


class TestBuildTargetList:
    def test_includes_reference_tickers(self):
        targets = mod.build_target_list()
        for t in ["SPY", "QQQ", "VIX"]:
            assert t in targets, f"{t} missing from target list"

    def test_includes_sp500_tickers(self):
        from universe.builder import get_universe_tickers
        sp500 = set(get_universe_tickers("sp500"))
        targets = set(mod.build_target_list())
        # At least 80% of SP500 should land in target list
        assert len(sp500 & targets) >= 0.8 * len(sp500), \
            f"sp500 coverage too low: {len(sp500 & targets)}/{len(sp500)}"

    def test_includes_held_positions(self, tmp_path, monkeypatch):
        # Point at a fake state dir with one known position
        fake_state = tmp_path / "state"
        fake_state.mkdir()
        (fake_state / "live_sp500.json").write_text(json.dumps({
            "positions": [{"ticker": "FAKEXYZ"}, {"ticker": "AAPL"}]
        }))
        monkeypatch.setattr(mod, "STATE_DIR", fake_state)
        targets = mod.build_target_list()
        assert "FAKEXYZ" in targets
        assert "AAPL" in targets

    def test_deduplicates(self):
        # If SPY appears in both sp500 and references, target list contains it once
        targets = mod.build_target_list()
        assert len(targets) == len(set(targets)), "duplicates in target list"


class TestDryRun:
    def test_dry_run_does_not_call_load_hourly(self, monkeypatch, capsys):
        # Sentinel: if load_hourly is called, raise to detect
        def boom(*args, **kwargs):
            raise AssertionError("load_hourly called during --dry-run")
        monkeypatch.setattr(mod, "load_hourly", boom)
        monkeypatch.setattr(sys, "argv", ["backfill_hourly_bars.py", "--dry-run", "--limit", "5"])
        rc = mod.main()
        assert rc == 0
        captured = capsys.readouterr()
        assert "Would load" in captured.out
