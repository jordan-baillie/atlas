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

BUILD = _fill("2026-06-01", 999.0)  # day-1 book build — always excluded from G6


class TestSlippageGate:
    def test_median_and_fail_above_bar(self):
        fills = [BUILD] + [_fill("2026-06-10", v) for v in (10.0, 25.6, 40.0)]
        g = gates.slippage_gate(fills, today=TODAY)
        assert g["median_bps"] == 25.6
        assert g["worst_bps"] == 40.0
        assert g["n_fills"] == 3
        assert g["bar_bps"] == 16.0
        assert g["pass"] is False

    def test_pass_below_bar(self):
        fills = [BUILD] + [_fill("2026-06-10", v) for v in (2.0, 8.0, 15.9)]
        g = gates.slippage_gate(fills, today=TODAY)
        assert g["pass"] is True

    def test_build_day_excluded(self):
        # canonical methodology (crucible evidence._g6): the day-1 book build is a
        # one-off establishment cost, not the steady-state rebalance the gate measures
        fills = [_fill("2026-06-09", 172.8), _fill("2026-06-09", 1795.0),
                 _fill("2026-06-10", 5.0), _fill("2026-06-11", 7.0)]
        g = gates.slippage_gate(fills, today=TODAY)
        assert g["build_day_excluded"] == "2026-06-09"
        assert g["n_fills"] == 2
        assert g["median_bps"] == 6.0
        assert g["pass"] is True

    def test_single_day_history_is_accruing(self):
        # only the build day exists -> zero steady-state evidence, not a verdict
        fills = [_fill("2026-06-10", v) for v in (10.0, 25.6, 40.0)]
        g = gates.slippage_gate(fills, today=TODAY)
        assert g["n_fills"] == 0
        assert g["pass"] is None

    def test_null_slippage_excluded(self):
        fills = [BUILD, _fill("2026-06-10", 12.0), _fill("2026-06-10", None)]
        g = gates.slippage_gate(fills, today=TODAY)
        assert g["n_fills"] == 1

    def test_lookback_excludes_old_fills(self):
        # 2026-01-01 is both the build day AND outside the 60d window; either way out
        fills = [_fill("2026-01-01", 500.0), _fill("2026-06-09", 6.0), _fill("2026-06-10", 5.0)]
        g = gates.slippage_gate(fills, today=TODAY)
        assert g["n_fills"] == 2
        assert g["median_bps"] == 5.5
        assert g["pass"] is True

    def test_empty_is_accruing(self):
        g = gates.slippage_gate([], today=TODAY)
        assert g["n_fills"] == 0
        assert g["median_bps"] is None
        assert g["pass"] is None
        assert g["build_day_excluded"] is None

    def test_p75_needs_four_fills(self):
        g3 = gates.slippage_gate([BUILD] + [_fill("2026-06-10", v) for v in (1, 2, 3)], today=TODAY)
        assert g3["p75_bps"] is None
        g4 = gates.slippage_gate([BUILD] + [_fill("2026-06-10", v) for v in (1, 2, 3, 4)], today=TODAY)
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

    def test_insufficient_is_nan_safe_and_accruing(self):
        g = gates.track_gate([0.001, 0.002], EXPECTATION)
        assert g["status"] == "insufficient"
        # tri-state honesty: insufficient = ACCRUING (None), never PASS off 2 obs
        assert g["pass"] is None
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
        self._write(d, "fills.jsonl",
                    [_fill("2026-06-01", 999.0),  # build day (excluded from G6)
                     _fill("2026-06-10", 5.0), _fill("2026-06-10", 7.0)])
        self._write(d, "runs.jsonl", [_run("2026-06-10", [{"ok": True}] * 4)])
        self._write(d, "returns.jsonl",
                    [{"date": f"2026-05-{i:02d}", "ret": 0.001, "equity": 10000 + i} for i in range(1, 26)])
        # synthetic book is unregistered; strict=False exercises the legacy SCORING path
        # (this test is about composition mechanics, not the unregistered-gap path)
        g = gates.evaluate_gates("strat_a", EXPECTATION, base=tmp_path, strict_modeled_cost=False)
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
        (d / "fills.jsonl").write_text('{"date": "2026-06-01", "slippage_bps": 999.0}\n'
                                       '{"date": "2026-06-10", "slippage_bps": 4.0}\nNOT JSON\n')
        g = gates.evaluate_gates("strat_b", None, base=tmp_path, strict_modeled_cost=False)
        assert g["slippage"]["n_fills"] == 1

    def test_any_false_fails_overall(self, tmp_path):
        d = tmp_path / "strat_c"
        self._write(d, "fills.jsonl",
                    [_fill("2026-06-01", 1.0)] + [_fill("2026-06-10", 99.0)] * 3)  # median way over bar
        g = gates.evaluate_gates("strat_c", None, base=tmp_path, strict_modeled_cost=False)
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


class TestBrokerErrorClassification:
    """Task #19: wash-trade exclusion + error-class breakdown from the persisted err field."""

    def test_wash_trade_excluded_from_rate(self):
        # 1 wash collision + 99 clean: rate must be 0%, collision counted separately
        orders = [{"ok": True}] * 99 + [{"ok": False, "err": "potential wash trade detected"}]
        g = gates.broker_error_gate([_run("2026-06-11", orders)], today=TODAY)
        assert g["n_orders"] == 99
        assert g["n_errors"] == 0
        assert g["n_excluded_wash"] == 1
        assert g["pass"] is True

    def test_htb_and_halt_remain_in_rate_with_classes(self):
        orders = [{"ok": True}] * 96 + [
            {"ok": False, "err": 'only day orders are allowed for hard-to-borrow asset "WEN"'},
            {"ok": False, "err": "market order rejected due to trading halt on symbol: KALV"},
            {"ok": False, "err": "asset SLNO is not active"},
            {"ok": False},  # no err recorded (legacy row)
        ]
        g = gates.broker_error_gate([_run("2026-06-11", orders)], today=TODAY)
        assert g["n_orders"] == 100
        assert g["n_errors"] == 4
        assert g["pass"] is False  # 4% > 1% bar — real frictions stay in
        assert g["error_classes"] == {"htb": 1, "halt": 1, "inactive_asset": 1, "unknown": 1}

    def test_classifier_buckets(self):
        cases = {
            "42210000 whatever": "htb",
            "insufficient buying power": "buying_power",
            "opg orders must be submitted after 7:00pm": "order_window",
            "pdt_preempt: daytrade limit": "pdt",
            "something novel": "other",
            "": "unknown",
        }
        for err, want in cases.items():
            assert gates._classify_broker_error(err) == want, err


# ── G6 futures (tick-space) slippage — pre-reg 2026-06-12 ────────────────────


def _ffill(d, ticker, ticks):
    return {"date": d, "ticker": ticker, "side": "BUY", "qty": 1,
            "slippage_bps": 1.0, "slippage_ticks": ticks, "status": "FILLED"}


class TestFuturesSlippageGate:
    def test_frozen_bar_values(self):
        from atlas.brokers.ib.broker import futures_cost_spec
        mes = futures_cost_spec("MES")            # $1.25/tick -> 2 + 0.85/1.25 = 2.68
        assert mes["tick_value"] == 1.25 and mes["bar_ticks"] == 2.68
        mnq = futures_cost_spec("MNQ")            # $0.50/tick -> 2 + 1.7 = 3.7
        assert mnq["bar_ticks"] == 3.7
        assert futures_cost_spec("AAPL") is None  # equities have no tick spec

    def test_pass_and_fail_per_symbol(self):
        fills = [_ffill("2026-06-01", "MES", 99.0),                     # build day — excluded
                 _ffill("2026-06-10", "MES", 1.0), _ffill("2026-06-10", "MES", 2.0),
                 _ffill("2026-06-10", "MNQ", 5.0), _ffill("2026-06-10", "MNQ", 6.0)]
        g = gates.futures_slippage_gate(fills, today=TODAY)
        assert g["per_symbol"]["MES"]["pass"] is True     # median 1.5 <= 2.68
        assert g["per_symbol"]["MNQ"]["pass"] is False    # median 5.5 > 3.7
        assert g["pass"] is False                          # any symbol failing fails G6
        assert g["build_day_excluded"] == "2026-06-01"

    def test_all_symbols_pass(self):
        fills = [_ffill("2026-06-01", "MES", 99.0),
                 _ffill("2026-06-10", "MES", 1.0), _ffill("2026-06-11", "MES", 2.0)]
        g = gates.futures_slippage_gate(fills, today=TODAY)
        assert g["pass"] is True and g["n_fills"] == 2

    def test_empty_is_accruing(self):
        assert gates.futures_slippage_gate([], today=TODAY)["pass"] is None

    def test_evaluate_gates_routes_by_ruler(self, tmp_path):
        import json as _j
        d = tmp_path / "futbook"; d.mkdir()
        rows = [_ffill("2026-06-01", "MES", 99.0)] + [_ffill("2026-06-10", "MES", 1.0)] * 3
        (d / "fills.jsonl").write_text("\n".join(_j.dumps(r) for r in rows) + "\n")
        (d / "runs.jsonl").write_text("")
        out = gates.evaluate_gates("futbook", None, base=tmp_path)
        assert out["slippage"]["ruler"] == "ticks"
        assert out["slippage"]["per_symbol"]["MES"]["pass"] is True

    def test_equity_book_keeps_bps_ruler(self, tmp_path):
        import json as _j
        d = tmp_path / "eqbook"; d.mkdir()
        rows = [{"date": "2026-06-10", "ticker": "AAA", "side": "BUY", "qty": 1,
                 "slippage_bps": 5.0, "status": "FILLED"}] * 3
        (d / "fills.jsonl").write_text("\n".join(_j.dumps(r) for r in rows) + "\n")
        (d / "runs.jsonl").write_text("")
        out = gates.evaluate_gates("eqbook", None, base=tmp_path)
        assert out["slippage"]["ruler"] == "bps"


# ── G6 slip_ref (Leg B Phase 2 — open vs decision_px picker) ─────────────────

class TestSlipRef:
    """Mirror crucible._slip semantics: prefer slippage_open_bps, fall back to slippage_bps."""

    def test_all_open_fields_gives_open_ref(self):
        fills = [BUILD,
                 _fill("2026-06-10", 5.0, slippage_open_bps=5.0),
                 _fill("2026-06-11", 7.0, slippage_open_bps=7.0)]
        g = gates.slippage_gate(fills, today=TODAY)
        assert g["slip_ref"] == "open"

    def test_no_open_field_falls_back_to_decision_px_stale(self):
        fills = [BUILD,
                 _fill("2026-06-10", 5.0),
                 _fill("2026-06-11", 7.0)]
        g = gates.slippage_gate(fills, today=TODAY)
        assert g["slip_ref"] == "decision_px(stale)"

    def test_mixed_when_some_open_some_decision_px(self):
        fills = [BUILD,
                 _fill("2026-06-10", 5.0, slippage_open_bps=5.0),  # open
                 _fill("2026-06-11", 7.0)]                          # decision_px only
        g = gates.slippage_gate(fills, today=TODAY)
        assert g["slip_ref"] == "mixed"

    def test_slip_ref_none_when_no_fills(self):
        g = gates.slippage_gate([], today=TODAY)
        assert g["slip_ref"] is None


# ── Per-book modeled cost + unregistered gap (FIX 2) ─────────────────────────

class TestEvaluateGatesModeled:
    def _write(self, d, fname, rows):
        d.mkdir(parents=True, exist_ok=True)
        (d / fname).write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def test_unregistered_book_strict_returns_none_not_false(self, tmp_path):
        """Deployed book absent from MODELED_COST_BPS → pass=None + reason='unregistered_modeled_cost'."""
        name = "ghost_unregistered_book"  # fabricated; intentionally absent from MODELED_COST_BPS
        assert name not in gates.MODELED_COST_BPS
        d = tmp_path / name
        self._write(d, "fills.jsonl",
                    [_fill("2026-06-01", 999.0), _fill("2026-06-10", 5.0)])
        self._write(d, "runs.jsonl", [])
        self._write(d, "returns.jsonl", [])
        g = gates.evaluate_gates(name, None, base=tmp_path, strict_modeled_cost=True)
        assert g["slippage"]["pass"] is None           # NOT False — unscored gap
        assert g["slippage"]["reason"] == "unregistered_modeled_cost"
        assert g["pass"] is None                       # overall must also be None, not False

    def test_unregistered_gap_is_the_DEFAULT(self, tmp_path):
        """Canonical default: an unregistered deployed book is NOT scored on contaminated
        data even without passing strict_modeled_cost — locks crucible-parity so the
        old false-FAIL behavior cannot silently regress (the production dashboard calls
        evaluate_gates with no strict flag)."""
        name = "ghost_unregistered_book"  # genuinely absent from MODELED_COST_BPS
        assert name not in gates.MODELED_COST_BPS
        d = tmp_path / name
        self._write(d, "fills.jsonl",
                    [_fill("2026-06-01", 999.0), _fill("2026-06-10", 145.0)])  # contaminated decision_px
        self._write(d, "runs.jsonl", [])
        self._write(d, "returns.jsonl", [])
        g = gates.evaluate_gates(name, None, base=tmp_path)   # NO strict flag — default path
        assert g["slippage"]["reason"] == "unregistered_modeled_cost"
        assert g["slippage"]["pass"] is None          # honest 'cannot evaluate', not a false FAIL
        assert g["pass"] is None

    def test_registered_book_uses_per_book_bar(self, tmp_path):
        """val_mom_trend_smallcap: modeled=8.0 bps → bar=16.0 bps; median 10 bps must PASS."""
        name = "val_mom_trend_smallcap"
        d = tmp_path / name
        self._write(d, "fills.jsonl",
                    [_fill("2026-06-01", 999.0)] + [_fill("2026-06-10", 10.0)] * 3)
        self._write(d, "runs.jsonl", [])
        self._write(d, "returns.jsonl", [])
        g = gates.evaluate_gates(name, None, base=tmp_path)
        assert g["slippage"]["bar_bps"] == pytest.approx(gates.SLIPPAGE_MULT * 8.0)
        assert g["slippage"]["pass"] is True  # median 10 bps <= 16 bps bar


# ── FIX 3: fill_quality (advisory) ───────────────────────────────────────────

class TestFillQuality:
    def test_counts_filled_cancelled_unresolved(self):
        fills = [
            {"date": "2026-06-10", "status": "filled"},
            {"date": "2026-06-10", "status": "FILLED"},     # uppercase — case-insensitive
            {"date": "2026-06-10", "status": "cancelled"},
            {"date": "2026-06-10", "status": "pending"},    # unresolved
            {"date": "2026-06-10", "status": "submitted"},  # unresolved
        ]
        q = gates.fill_quality(fills, today=TODAY)
        assert q["n_filled"] == 2
        assert q["n_cancelled"] == 1
        assert q["n_unresolved"] == 2
        assert q["n_total"] == 5
        assert q["fill_rate_pct"] == pytest.approx(40.0)
        assert q["pass"] is None   # ADVISORY — never a hard gate

    def test_fill_quality_does_not_flip_overall_pass(self, tmp_path):
        """fill_quality advisory must NOT change the evaluate_gates overall pass tri-state."""
        d = tmp_path / "fq_advisory_book"
        d.mkdir()
        fills = (
            [_fill("2026-06-01", 999.0),     # build day — excluded from G6
             _fill("2026-06-10", 5.0)]       # under bar
            + [{"date": "2026-06-10", "status": "pending"}]  # unresolved (advisory only)
        )
        import json as _j
        (d / "fills.jsonl").write_text("\n".join(_j.dumps(r) for r in fills) + "\n")
        (d / "runs.jsonl").write_text("")
        (d / "returns.jsonl").write_text("")
        g = gates.evaluate_gates("fq_advisory_book", None, base=tmp_path)
        assert g["fill_quality"]["pass"] is None   # advisory stays None
        # overall pass: broker_errors=None, track=None → tri-state None (not flipped by advisory)
        assert g["pass"] is None


class TestFuturesFillRecording:
    def test_futures_slippage_fields(self):
        from atlas.execution.record_fills import _futures_slippage
        # MES BUY: decision 5000.00, fill 5000.50 = +2 ticks adverse, 2 contracts
        r = _futures_slippage("MES", "BUY", 5000.00, 5000.50, 2)
        assert r["slippage_ticks"] == 2.0 and r["slippage_usd"] == 5.0   # 2t x $1.25 x 2
        # SELL favorable: fill above decision = negative (favorable) for a sell? No:
        # SELL received MORE -> favorable -> negative adverse ticks
        r2 = _futures_slippage("MES", "SELL", 5000.00, 5000.50, 1)
        assert r2["slippage_ticks"] == -2.0
        assert _futures_slippage("AAPL", "BUY", 100.0, 100.1, 1) == {}   # equities untouched
