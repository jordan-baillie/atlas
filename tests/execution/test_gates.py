"""Go-live gates (G6 slippage / G7 broker errors / track) — atlas.execution.gates."""
from __future__ import annotations

import json
from datetime import date

import pytest

from atlas.execution import gates


TODAY = date(2026, 6, 13)


def _fill(d: str, bps, **kw):
    rec = {"date": d, "ticker": "AAA", "side": "BUY", "qty": 10, "decision_px": 100.0,
           "fill_px": 100.1, "filled_qty": 10, "status": "filled",
           "slippage_bps": bps, "order_id": f"o-{d}-{bps}"}
    rec.update(kw)
    return rec


def _run(d: str, orders, **kw):
    rec = {"date": d, "state": "shadow", "dry_run": False, "n_orders": len(orders),
           "turnover": 100.0, "blocked": None, "track": "on_track", "orders": orders}
    rec.update(kw)
    return rec


# ── G6 slippage ──────────────────────────────────────────────────────────────

class TestSlippageGate:
    def test_median_and_fail_above_bar(self):
        fills = [_fill("2026-06-10", v) for v in (10.0, 25.6, 40.0)]
        g = gates.slippage_gate(fills, today=TODAY)
        assert g["median_bps"] == 25.6
        assert g["worst_bps"] == 40.0
        assert g["n_fills"] == 3
        assert g["bar_bps"] == 16.0
        assert g["pass"] is False

    def test_pass_below_bar(self):
        fills = [_fill("2026-06-10", v) for v in (2.0, 8.0, 15.9)]
        g = gates.slippage_gate(fills, today=TODAY)
        assert g["pass"] is True

    def test_null_slippage_excluded(self):
        fills = [_fill("2026-06-10", 12.0), _fill("2026-06-10", None)]
        g = gates.slippage_gate(fills, today=TODAY)
        assert g["n_fills"] == 1

    def test_lookback_excludes_old_fills(self):
        fills = [_fill("2026-01-01", 500.0), _fill("2026-06-10", 5.0)]
        g = gates.slippage_gate(fills, today=TODAY)
        assert g["n_fills"] == 1
        assert g["median_bps"] == 5.0
        assert g["pass"] is True

    def test_empty_is_accruing(self):
        g = gates.slippage_gate([], today=TODAY)
        assert g["n_fills"] == 0
        assert g["median_bps"] is None
        assert g["pass"] is None

    def test_p75_needs_four_fills(self):
        g3 = gates.slippage_gate([_fill("2026-06-10", v) for v in (1, 2, 3)], today=TODAY)
        assert g3["p75_bps"] is None
        g4 = gates.slippage_gate([_fill("2026-06-10", v) for v in (1, 2, 3, 4)], today=TODAY)
        assert g4["p75_bps"] is not None


# ── G7 broker errors ─────────────────────────────────────────────────────────

class TestBrokerErrorGate:
    def test_dry_run_and_blocked_excluded(self):
        runs = [
            _run("2026-06-10", [{"ok": False}], dry_run=True),
            _run("2026-06-10", [{"ok": False}], blocked="L3: halt"),
            _run("2026-06-11", [{"ok": True}, {"ok": True}]),
        ]
        g = gates.broker_error_gate(runs, today=TODAY)
        assert g["n_orders"] == 2
        assert g["n_errors"] == 0
        assert g["pass"] is True

    def test_unmatched_ok_null_out_of_denominator(self):
        runs = [_run("2026-06-11", [{"ok": True}, {"ok": None}, {}])]
        g = gates.broker_error_gate(runs, today=TODAY)
        assert g["n_orders"] == 1
        assert g["n_unmatched"] == 2

    def test_error_rate_fails_above_bar(self):
        orders = [{"ok": True}] * 49 + [{"ok": False}]
        g = gates.broker_error_gate([_run("2026-06-11", orders)], today=TODAY)
        assert g["n_orders"] == 50
        assert g["error_rate_pct"] == 2.0
        assert g["pass"] is False

    def test_zero_errors_pass(self):
        g = gates.broker_error_gate([_run("2026-06-11", [{"ok": True}] * 50)], today=TODAY)
        assert g["error_rate_pct"] == 0.0
        assert g["pass"] is True

    def test_no_runs_is_accruing(self):
        g = gates.broker_error_gate([], today=TODAY)
        assert g["pass"] is None
        assert g["error_rate_pct"] is None

    def test_lookback_excludes_old_runs(self):
        runs = [_run("2026-01-01", [{"ok": False}] * 10), _run("2026-06-11", [{"ok": True}])]
        g = gates.broker_error_gate(runs, today=TODAY)
        assert g["n_orders"] == 1
        assert g["n_errors"] == 0


# ── Track gate ───────────────────────────────────────────────────────────────

EXPECTATION = {"daily_mean": 0.0005, "daily_std": 0.01, "sharpe": 0.62}


class TestTrackGate:
    def test_on_track(self):
        realized = [0.001, -0.002, 0.0015, 0.0007] * 6  # 24 obs, positive mean
        g = gates.track_gate(realized, EXPECTATION)
        assert g["status"] == "on_track"
        assert g["pass"] is True
        assert g["n_obs"] == 24
        assert g["expected_sharpe"] == 0.62

    def test_insufficient_is_nan_safe_and_passes(self):
        g = gates.track_gate([0.001, 0.002], EXPECTATION)
        assert g["status"] == "insufficient"
        assert g["pass"] is True  # TrackVerdict.ok includes insufficient
        # the whole point: must be valid strict JSON (no NaN)
        json.dumps(g, allow_nan=False)
        assert g["realized_mean"] is None
        assert g["mean_z"] is None

    def test_halt_on_negative_expectancy(self):
        g = gates.track_gate([-0.001] * 25, EXPECTATION)
        assert g["status"] == "halt"
        assert g["pass"] is False

    def test_no_expectation_is_null(self):
        g = gates.track_gate([0.001] * 25, None)
        assert g["status"] is None
        assert g["pass"] is None


# ── evaluate_gates + rollup ──────────────────────────────────────────────────

class TestEvaluateGates:
    def _write(self, d, fname, rows):
        d.mkdir(parents=True, exist_ok=True)
        (d / fname).write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def test_composed_from_files(self, tmp_path):
        d = tmp_path / "strat_a"
        self._write(d, "fills.jsonl", [_fill("2026-06-10", 5.0), _fill("2026-06-10", 7.0)])
        self._write(d, "runs.jsonl", [_run("2026-06-10", [{"ok": True}] * 4)])
        self._write(d, "returns.jsonl",
                    [{"date": f"2026-05-{i:02d}", "ret": 0.001, "equity": 10000 + i} for i in range(1, 26)])
        g = gates.evaluate_gates("strat_a", EXPECTATION, base=tmp_path)
        assert g["slippage"]["pass"] is True
        assert g["broker_errors"]["pass"] is True
        assert g["track"]["status"] == "on_track"
        assert g["pass"] is True
        json.dumps(g, allow_nan=False)

    def test_missing_dir_is_null_shape(self, tmp_path):
        g = gates.evaluate_gates("ghost", EXPECTATION, base=tmp_path)
        assert g["slippage"]["pass"] is None
        assert g["broker_errors"]["pass"] is None
        assert g["pass"] is None

    def test_malformed_lines_skipped(self, tmp_path):
        d = tmp_path / "strat_b"
        d.mkdir()
        (d / "fills.jsonl").write_text('{"date": "2026-06-10", "slippage_bps": 4.0}\nNOT JSON\n')
        g = gates.evaluate_gates("strat_b", None, base=tmp_path)
        assert g["slippage"]["n_fills"] == 1

    def test_any_false_fails_overall(self, tmp_path):
        d = tmp_path / "strat_c"
        self._write(d, "fills.jsonl", [_fill("2026-06-10", 99.0)] * 3)  # median way over bar
        g = gates.evaluate_gates("strat_c", None, base=tmp_path)
        assert g["slippage"]["pass"] is False
        assert g["pass"] is False


class TestRollup:
    def test_tri_state(self):
        assert gates.rollup({})["pass"] is None
        assert gates.rollup({"a": {"pass": True}, "b": {"pass": True}})["pass"] is True
        assert gates.rollup({"a": {"pass": True}, "b": {"pass": None}})["pass"] is None
        r = gates.rollup({"a": {"pass": True}, "b": {"pass": False}})
        assert r["pass"] is False
        assert r["failing"] == ["b"]
        assert r["n_pass"] == 1 and r["n_fail"] == 1
