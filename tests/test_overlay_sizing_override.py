"""tests/test_overlay_sizing_override.py

Regression tests for P1-B: overlay sizing_override + avoid_tickers wiring.
"""
import logging
import sys
import os
from pathlib import Path
from unittest.mock import MagicMock
import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
os.chdir(PROJECT)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_plan(entries, overlay_context=None):
    plan = {
        "status": "APPROVED",
        "proposed_entries": entries,
        "proposed_exits": [],
    }
    if overlay_context is not None:
        plan["overlay_context"] = overlay_context
    return plan


def _entry(ticker, qty=100, price=10.0, stop=9.0):
    return {
        "ticker": ticker,
        "position_size": qty,
        "entry_price": price,
        "stop_price": stop,
        "strategy": "test_strat",
        "confidence": 0.7,
    }


def _make_dry_config():
    return {
        "trading": {
            "mode": "live",
            "live_safety": {"dry_run_first": True},
        },
        "market_id": "sp500",
        "fees": {},
        "strategies": {},
        "overlay": {"shadow_mode": False},
    }


# ── LiveExecutor tests ────────────────────────────────────────────────────────

class TestLiveExecutorOverlay:

    def _make_executor(self):
        from brokers.live_executor import LiveExecutor
        ex = LiveExecutor(_make_dry_config())
        ex._connected = True
        ex._halted = False
        ex._halt_reason = ""
        ex._daily_date = "2026-04-24"
        ex._daily_order_count = 0
        return ex

    def _patch_side_effects(self, ex):
        ex._run_volatility_gate = MagicMock(return_value={
            "action": "none", "size_multiplier": 1.0, "message": "",
            "flags": [], "gate_enabled": False, "triggered_count": 0,
        })
        ex._check_circuit_breaker = MagicMock(return_value=False)
        ex._capture_start_equity = MagicMock()
        ex.check_market_state = MagicMock(return_value={
            "is_tradeable": True, "states": [], "message": "",
        })
        ex.place_stops_for_plan = MagicMock(return_value={})

    # ── test 1: sizing_override=0.5 halves qty ────────────────────────────────
    def test_sizing_override_halves_qty(self, caplog):
        ex = self._make_executor()
        self._patch_side_effects(ex)

        calls = []

        def fake_entry(entry, td):
            calls.append(dict(entry))
            return {"success": True, "ticker": entry["ticker"],
                    "qty": entry["position_size"], "status": "SUBMITTED"}

        ex._execute_entry = fake_entry

        plan = _make_plan(
            [_entry("MSFT", qty=100)],
            overlay_context={"sizing_override": 0.5, "tickers_to_avoid": [], "action": "reduce"},
        )
        with caplog.at_level(logging.DEBUG, logger="atlas.live_executor"):
            report = ex.execute_plan(plan, "2026-04-24")

        assert len(calls) == 1, "Expected exactly 1 entry call"
        assert calls[0]["position_size"] == 50, (
            f"Expected position_size=50, got {calls[0]['position_size']}"
        )

        # log line present: use getMessage() not .message
        overlay_logs = [
            r.getMessage() for r in caplog.records
            if "overlay_applied" in r.getMessage()
        ]
        assert any("MSFT" in m and "qty=100→50" in m for m in overlay_logs), (
            f"Missing qty=100→50 log.\noverlay_logs={overlay_logs}\n"
            f"all_records={[r.getMessage() for r in caplog.records]}"
        )

    # ── test 2: sizing_override=0.0 → skip ───────────────────────────────────
    def test_sizing_override_zero_skips_order(self, caplog):
        ex = self._make_executor()
        self._patch_side_effects(ex)

        calls = []

        def fake_entry(entry, td):
            calls.append(dict(entry))
            return {"success": True, "ticker": entry["ticker"], "qty": 0, "status": "SUBMITTED"}

        ex._execute_entry = fake_entry

        plan = _make_plan(
            [_entry("AAPL", qty=100)],
            overlay_context={"sizing_override": 0.0, "tickers_to_avoid": [], "action": "reduce"},
        )
        with caplog.at_level(logging.DEBUG, logger="atlas.live_executor"):
            report = ex.execute_plan(plan, "2026-04-24")

        assert len(calls) == 0, "Order should have been skipped (qty→0)"

        assert len(report["entries"]) == 1
        assert report["entries"][0]["reason"] == "overlay_sizing_zero"
        assert report["entries"][0]["success"] is False

        overlay_logs = [
            r.getMessage() for r in caplog.records
            if "overlay_applied" in r.getMessage()
        ]
        assert any("qty→0" in m for m in overlay_logs), (
            f"Missing qty→0 log. overlay_logs={overlay_logs}"
        )

    # ── test 3: tickers_to_avoid causes skip ─────────────────────────────────
    def test_avoid_tickers_skips_entry(self, caplog):
        ex = self._make_executor()
        self._patch_side_effects(ex)

        calls = []

        def fake_entry(entry, td):
            calls.append(dict(entry))
            return {"success": True, "ticker": entry["ticker"],
                    "qty": entry["position_size"], "status": "SUBMITTED"}

        ex._execute_entry = fake_entry

        plan = _make_plan(
            [_entry("AAPL", qty=100), _entry("GOOGL", qty=80)],
            overlay_context={
                "tickers_to_avoid": ["AAPL"],
                "sizing_override": None,
                "action": "reduce",
            },
        )
        with caplog.at_level(logging.DEBUG, logger="atlas.live_executor"):
            report = ex.execute_plan(plan, "2026-04-24")

        # Only GOOGL should reach _execute_entry
        assert len(calls) == 1, f"Expected 1 call, got {len(calls)}: {calls}"
        assert calls[0]["ticker"] == "GOOGL"

        aapl = [e for e in report["entries"] if e.get("ticker") == "AAPL"]
        assert len(aapl) == 1
        assert aapl[0]["reason"] == "overlay_avoid_tickers"
        assert aapl[0]["success"] is False

        overlay_logs = [
            r.getMessage() for r in caplog.records
            if "overlay_applied" in r.getMessage()
        ]
        assert any("AAPL" in m and "skip" in m for m in overlay_logs), (
            f"Missing AAPL skip log. overlay_logs={overlay_logs}"
        )

    # ── test 4: no overlay → original qty preserved ──────────────────────────
    def test_no_overlay_preserves_qty(self):
        ex = self._make_executor()
        self._patch_side_effects(ex)

        calls = []

        def fake_entry(entry, td):
            calls.append(dict(entry))
            return {"success": True, "ticker": entry["ticker"],
                    "qty": entry["position_size"], "status": "SUBMITTED"}

        ex._execute_entry = fake_entry

        plan = _make_plan([_entry("TSLA", qty=200)])
        report = ex.execute_plan(plan, "2026-04-24")

        assert len(calls) == 1
        assert calls[0]["position_size"] == 200

    # ── test 5: legacy "avoid_tickers" field name ─────────────────────────────
    def test_avoid_tickers_legacy_field_name(self):
        ex = self._make_executor()
        self._patch_side_effects(ex)

        calls = []

        def fake_entry(entry, td):
            calls.append(dict(entry))
            return {"success": True, "ticker": entry["ticker"],
                    "qty": entry["position_size"], "status": "SUBMITTED"}

        ex._execute_entry = fake_entry

        plan = _make_plan(
            [_entry("NVDA", qty=50)],
            overlay_context={
                "avoid_tickers": ["NVDA"],   # legacy field name
                "sizing_override": None,
                "action": "reduce",
            },
        )
        report = ex.execute_plan(plan, "2026-04-24")

        assert len(calls) == 0
        assert report["entries"][0]["reason"] == "overlay_avoid_tickers"

    # ── test 6: end-to-end mixed plan ────────────────────────────────────────
    def test_end_to_end_mixed_plan(self, caplog):
        """AAPL avoided, MSFT halved → 50, GOOGL halved → 40."""
        ex = self._make_executor()
        self._patch_side_effects(ex)

        submitted = []

        def fake_entry(entry, td):
            submitted.append({"ticker": entry["ticker"], "qty": entry["position_size"]})
            return {"success": True, "ticker": entry["ticker"],
                    "qty": entry["position_size"], "status": "SUBMITTED"}

        ex._execute_entry = fake_entry

        plan = _make_plan(
            [
                _entry("AAPL", qty=100),
                _entry("MSFT", qty=100),
                _entry("GOOGL", qty=80),
            ],
            overlay_context={
                "tickers_to_avoid": ["AAPL"],
                "sizing_override": 0.5,
                "action": "reduce",
                "confidence": 0.9,
            },
        )
        with caplog.at_level(logging.DEBUG, logger="atlas.live_executor"):
            report = ex.execute_plan(plan, "2026-04-24")

        assert all(s["ticker"] != "AAPL" for s in submitted), "AAPL should be skipped"

        msft = next((s for s in submitted if s["ticker"] == "MSFT"), None)
        assert msft is not None
        assert msft["qty"] == 50, f"MSFT expected 50, got {msft['qty']}"

        googl = next((s for s in submitted if s["ticker"] == "GOOGL"), None)
        assert googl is not None
        assert googl["qty"] == 40, f"GOOGL expected 40, got {googl['qty']}"

        # 3 report entries: 1 skipped AAPL + 2 submitted
        assert len(report["entries"]) == 3

        overlay_logs = [
            r.getMessage() for r in caplog.records
            if "overlay_applied" in r.getMessage()
        ]
        assert any("AAPL" in m for m in overlay_logs), f"No AAPL log. logs={overlay_logs}"
        assert any("MSFT" in m for m in overlay_logs), f"No MSFT log. logs={overlay_logs}"
        assert any("GOOGL" in m for m in overlay_logs), f"No GOOGL log. logs={overlay_logs}"


# ── execute_approved.py pre-filter logic tests ────────────────────────────────
# Tested via a direct re-implementation of the filter function
# (avoids full main() harness complexity while validating the logic)

class TestExecuteApprovedPreFilterLogic:
    """Validate the overlay filter algorithm as implemented in execute_approved.py."""

    @staticmethod
    def _apply_filter(entries, overlay_context):
        """Mirror of the overlay pre-filter block in execute_approved.main()."""
        overlay_ctx = overlay_context or {}
        _avoid_raw = (
            overlay_ctx.get("tickers_to_avoid")
            or overlay_ctx.get("avoid_tickers")
            or []
        )
        overlay_avoid = set(_avoid_raw)
        overlay_sizing = overlay_ctx.get("sizing_override")
        if overlay_sizing is not None:
            overlay_sizing = float(overlay_sizing)

        filtered, skipped = [], []
        for entry in entries:
            ticker = entry.get("ticker", "")
            if ticker in overlay_avoid:
                skipped.append({"ticker": ticker, "reason": "avoid_tickers"})
                continue
            if overlay_sizing is not None:
                orig_qty = entry.get("position_size", 0)
                new_qty = int(orig_qty * overlay_sizing)
                if new_qty <= 0:
                    skipped.append({"ticker": ticker, "reason": "sizing_zero"})
                    continue
                entry = dict(entry)
                entry["position_size"] = new_qty
            filtered.append(entry)

        return filtered, skipped

    def test_sizing_halved(self):
        filtered, skipped = self._apply_filter(
            [_entry("AAPL", qty=100)],
            {"sizing_override": 0.5, "tickers_to_avoid": []},
        )
        assert len(filtered) == 1
        assert filtered[0]["position_size"] == 50
        assert not skipped

    def test_sizing_zero_skipped(self):
        filtered, skipped = self._apply_filter(
            [_entry("AAPL", qty=100)],
            {"sizing_override": 0.0, "tickers_to_avoid": []},
        )
        assert not filtered
        assert skipped[0]["reason"] == "sizing_zero"

    def test_avoid_ticker_removed(self):
        filtered, skipped = self._apply_filter(
            [_entry("AAPL", qty=100), _entry("MSFT", qty=80)],
            {"tickers_to_avoid": ["AAPL"], "sizing_override": None},
        )
        assert [e["ticker"] for e in filtered] == ["MSFT"]
        assert skipped[0]["ticker"] == "AAPL"

    def test_no_overlay_passthrough(self):
        filtered, skipped = self._apply_filter([_entry("TSLA", qty=200)], {})
        assert len(filtered) == 1
        assert filtered[0]["position_size"] == 200
        assert not skipped

    def test_combined_avoid_and_sizing(self):
        filtered, skipped = self._apply_filter(
            [_entry("AAPL", qty=100), _entry("MSFT", qty=100), _entry("GOOGL", qty=80)],
            {"tickers_to_avoid": ["AAPL"], "sizing_override": 0.5},
        )
        assert len(filtered) == 2
        assert len(skipped) == 1
        assert skipped[0]["ticker"] == "AAPL"
        by_ticker = {e["ticker"]: e for e in filtered}
        assert by_ticker["MSFT"]["position_size"] == 50
        assert by_ticker["GOOGL"]["position_size"] == 40
