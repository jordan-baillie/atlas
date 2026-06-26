"""Self-healing and idempotency tests for reconcile_book.

Verifies the non-terminal status re-query fix:
  A – submitted->filled transition: upsert (not duplicate), book updated exactly once.
  B – idempotency: third pass with terminal order is a zero-query NO-OP.
  C – self-heal beyond old 15-row window: non-terminal order in old run is still re-queried.
  D – terminal 'canceled' order is never re-queried and never booked.
  E – broker exception on one order leaves its existing row intact, others still processed.
"""
from __future__ import annotations

import json

import pytest

from atlas.execution import record_fills as rf
import atlas.execution.virtual_book as vb


class _FakeStatus:
    def __init__(self, status, fill_price, filled_qty):
        self.status = status
        self.fill_price = fill_price
        self.filled_qty = filled_qty


class _FakeBroker:
    def __init__(self, by_oid):
        self.by_oid = by_oid
        self.calls: list = []

    def get_order_status(self, oid):
        self.calls.append(oid)
        result = self.by_oid[oid]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture()
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "LIVE_DATA", tmp_path)
    monkeypatch.setattr(vb, "LIVE_DATA", tmp_path)
    monkeypatch.setattr(rf, "_fetch_open_map", lambda *a, **k: {})
    return tmp_path


def _setup(live, name, orders_by_date, cap=10_000.0):
    """Set up strategy dir with runs.jsonl (one run per entry) and empty fills.jsonl."""
    d = live / name
    d.mkdir(parents=True)
    (d / "book.json").write_text(
        json.dumps({"cash": cap, "positions": {}, "capital_base": cap})
    )
    lines = [
        json.dumps({"date": date, "dry_run": False, "blocked": False, "orders": orders})
        for date, orders in orders_by_date
    ]
    (d / "runs.jsonl").write_text("\n".join(lines) + "\n")
    (d / "fills.jsonl").write_text("")
    return d


def _fills(live, name):
    return rf._jsonl(live / name / "fills.jsonl")


def _book(live, name):
    return json.loads((live / name / "book.json").read_text())


# ── A: submitted -> filled transition ─────────────────────────────────────────

def test_A_nonterminal_then_filled(live):
    """Pass 1 returns 'submitted': written, book NOT updated.
    Pass 2 returns 'filled': row UPSERTED (exactly one row), book updated exactly once."""
    _setup(live, "sa", [("2026-06-19", [
        {"ticker": "AAA", "side": "BUY", "qty": 5, "px": 100.0, "order_id": "oa1"},
    ])])

    # Pass 1: non-terminal
    n1 = rf.reconcile_book("sa", _FakeBroker({"oa1": _FakeStatus("submitted", 0.0, 0)}))
    assert n1 == 1
    rows = _fills(live, "sa")
    assert len(rows) == 1
    assert rows[0]["status"] == "submitted"
    assert _book(live, "sa")["positions"] == {}, "non-terminal must NOT update the book"

    # Pass 2: now filled
    n2 = rf.reconcile_book("sa", _FakeBroker({"oa1": _FakeStatus("filled", 101.0, 5)}))
    assert n2 == 1, "non-terminal order must be re-queried"
    rows = _fills(live, "sa")
    assert len(rows) == 1, "UPSERT: exactly one row per order_id"
    assert rows[0]["status"] == "filled"
    assert rows[0]["fill_px"] == 101.0
    book = _book(live, "sa")
    assert book["positions"] == {"AAA": 5}, "book updated exactly once on transition"
    assert round(book["cash"], 2) == round(10_000.0 - 5 * 101.0, 2)


# ── B: idempotency — third pass is a zero-query NO-OP ────────────────────────

def test_B_idempotent_after_terminal(live):
    """Once an order is terminal, a third reconcile pass makes zero broker calls."""
    _setup(live, "sb", [("2026-06-19", [
        {"ticker": "BBB", "side": "BUY", "qty": 3, "px": 50.0, "order_id": "ob1"},
    ])])
    broker = _FakeBroker({"ob1": _FakeStatus("filled", 50.0, 3)})
    rf.reconcile_book("sb", broker)   # settle
    rf.reconcile_book("sb", broker)   # already terminal -> 0

    call_log: list = []

    class _NeverCall:
        def get_order_status(self, oid):
            call_log.append(oid)
            raise AssertionError(f"broker must not be called for terminal order {oid}")

    n = rf.reconcile_book("sb", _NeverCall())
    assert n == 0
    assert call_log == [], "zero broker calls for already-terminal order"
    assert _book(live, "sb")["positions"] == {"BBB": 3}, "book unchanged by idempotent pass"


# ── C: self-heal beyond the old 15-row window ─────────────────────────────────

def test_C_selfheal_beyond_old_window(live):
    """Non-terminal order buried under >15 later runs is still re-queried and settled."""
    old_order = {"ticker": "OLD", "side": "BUY", "qty": 2, "px": 10.0, "order_id": "oc_old"}
    later_orders = [
        (f"2026-06-{20 + i:02d}", [
            {"ticker": f"X{i}", "side": "BUY", "qty": 1, "px": 1.0, "order_id": f"oc_new_{i}"}
        ])
        for i in range(20)
    ]
    _setup(live, "sc", [("2026-06-01", [old_order])] + later_orders)

    # Pre-populate: old order non-terminal; 20 later orders already terminal
    pre_fills = [
        {"date": "2026-06-01", "ticker": "OLD", "side": "BUY", "qty": 2,
         "decision_px": 10.0, "fill_px": None, "filled_qty": 0,
         "status": "submitted", "slippage_bps": None, "order_id": "oc_old"},
    ]
    for i in range(20):
        pre_fills.append({
            "date": f"2026-06-{20 + i:02d}", "ticker": f"X{i}", "side": "BUY", "qty": 1,
            "decision_px": 1.0, "fill_px": 1.0, "filled_qty": 1,
            "status": "filled", "slippage_bps": 0.0, "order_id": f"oc_new_{i}",
        })
    (live / "sc" / "fills.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in pre_fills)
    )

    broker = _FakeBroker({"oc_old": _FakeStatus("filled", 10.5, 2)})
    n = rf.reconcile_book("sc", broker)
    assert n == 1, "only the non-terminal old order should be re-queried"
    assert broker.calls == ["oc_old"], "only the stale non-terminal order is queried"

    rows = _fills(live, "sc")
    assert len(rows) == 21, "21 rows total: 20 existing terminal + 1 upserted"
    old_row = next(r for r in rows if r["order_id"] == "oc_old")
    assert old_row["status"] == "filled"
    assert old_row["fill_px"] == 10.5
    assert _book(live, "sc")["positions"].get("OLD") == 2, "old order now booked"


# ── D: terminal 'canceled' is never re-queried and never booked ───────────────

def test_D_canceled_not_requeried(live):
    """A terminal 'canceled' fill is never re-queried and never applied to the book."""
    _setup(live, "sd", [("2026-06-10", [
        {"ticker": "CAN", "side": "SELL", "qty": 4, "px": 20.0, "order_id": "od1"},
    ])])
    existing = [{"date": "2026-06-10", "ticker": "CAN", "side": "SELL", "qty": 4,
                 "decision_px": 20.0, "fill_px": None, "filled_qty": 0,
                 "status": "canceled", "slippage_bps": None, "order_id": "od1"}]
    (live / "sd" / "fills.jsonl").write_text(json.dumps(existing[0]) + "\n")

    call_log: list = []

    class _NeverCall:
        def get_order_status(self, oid):
            call_log.append(oid)
            raise AssertionError(f"canceled order must not be re-queried: {oid}")

    n = rf.reconcile_book("sd", _NeverCall())
    assert n == 0
    assert call_log == []
    assert "CAN" not in _book(live, "sd")["positions"], "canceled order must not be booked"


# ── E: broker exception leaves existing row intact, processes others ───────────

def test_E_exception_leaves_existing_intact(live):
    """Exception on one order: existing row preserved unchanged; other orders still processed."""
    orders = [
        {"ticker": "ERR", "side": "BUY", "qty": 1, "px": 10.0, "order_id": "oe_err"},
        {"ticker": "OK",  "side": "BUY", "qty": 2, "px": 20.0, "order_id": "oe_ok"},
    ]
    _setup(live, "se", [("2026-06-20", orders)])
    # Pre-existing non-terminal row for the error order
    existing_err = {"date": "2026-06-20", "ticker": "ERR", "side": "BUY", "qty": 1,
                    "decision_px": 10.0, "fill_px": None, "filled_qty": 0,
                    "status": "pending", "slippage_bps": None, "order_id": "oe_err"}
    (live / "se" / "fills.jsonl").write_text(json.dumps(existing_err) + "\n")

    broker = _FakeBroker({
        "oe_err": RuntimeError("broker timeout"),
        "oe_ok":  _FakeStatus("filled", 20.5, 2),
    })

    n = rf.reconcile_book("se", broker)
    assert n == 1, "only the successful query counts"

    rows = _fills(live, "se")
    assert len(rows) == 2, "two rows: preserved err row + new ok row"

    err_row = next(r for r in rows if r["order_id"] == "oe_err")
    assert err_row["status"] == "pending", "failed query must leave existing row unchanged"

    ok_row = next(r for r in rows if r["order_id"] == "oe_ok")
    assert ok_row["status"] == "filled"
    assert ok_row["fill_px"] == 20.5

    book = _book(live, "se")
    assert book["positions"].get("OK") == 2, "successful order applied to book"
    assert "ERR" not in book["positions"],   "errored order must not be applied to book"


# ── F: crash-window recovery — order ONLY in submitted.jsonl (not in runs.jsonl) ──

def test_F_submitted_jsonl_only_order_is_reconciled(live):
    """Crash-window case: process dies after placement but before _record_run writes runs.jsonl.
    The order exists only in submitted.jsonl; reconcile_book must recover and settle it."""
    name = "sf"
    d = live / name
    d.mkdir(parents=True)
    cap = 10_000.0
    (d / "book.json").write_text(json.dumps({"cash": cap, "positions": {}, "capital_base": cap}))
    (d / "runs.jsonl").write_text("")        # empty — crash before _record_run
    (d / "fills.jsonl").write_text("")
    (d / "submitted.jsonl").write_text(
        json.dumps({"date": "2026-06-26", "ticker": "AAA", "side": "BUY",
                    "qty": 5, "px": 100.0, "order_id": "sf_oid1"}) + "\n"
    )

    broker = _FakeBroker({"sf_oid1": _FakeStatus("filled", 101.0, 5)})
    n = rf.reconcile_book(name, broker)
    assert n == 1, "crash-window order must be reconciled from submitted.jsonl"

    rows = _fills(live, name)
    assert len(rows) == 1
    assert rows[0]["order_id"] == "sf_oid1"
    assert rows[0]["status"] == "filled"
    assert rows[0]["fill_px"] == 101.0

    book = _book(live, name)
    assert book["positions"].get("AAA") == 5, "crash-window order must be booked after reconciliation"


# ── G: order in BOTH submitted.jsonl and runs.jsonl — queried exactly once ──

def test_G_order_in_both_submitted_and_runs_queried_once(live):
    """An order present in both submitted.jsonl and runs.jsonl is queried exactly once (no dup fill)."""
    name = "sg"
    d = live / name
    d.mkdir(parents=True)
    cap = 10_000.0
    (d / "book.json").write_text(json.dumps({"cash": cap, "positions": {}, "capital_base": cap}))
    (d / "runs.jsonl").write_text(
        json.dumps({"date": "2026-06-26", "dry_run": False, "blocked": False,
                    "orders": [{"ticker": "BBB", "side": "BUY", "qty": 3,
                                "px": 50.0, "order_id": "sg_oid1"}]}) + "\n"
    )
    (d / "fills.jsonl").write_text("")
    (d / "submitted.jsonl").write_text(
        json.dumps({"date": "2026-06-26", "ticker": "BBB", "side": "BUY",
                    "qty": 3, "px": 50.0, "order_id": "sg_oid1"}) + "\n"
    )

    broker = _FakeBroker({"sg_oid1": _FakeStatus("filled", 50.5, 3)})
    n = rf.reconcile_book(name, broker)
    assert n == 1
    assert broker.calls == ["sg_oid1"], "order present in both sources must be queried exactly once"

    rows = _fills(live, name)
    assert len(rows) == 1, "exactly one fill row — no duplicate from the dual source"
    assert rows[0]["status"] == "filled"
