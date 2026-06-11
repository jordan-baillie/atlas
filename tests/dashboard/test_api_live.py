"""/api/live response shape — legacy keys + the additive gates object; never-500 behavior."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def app_client(monkeypatch, tmp_path):
    """TestClient with auth overridden and live-data roots pointed at tmp."""
    monkeypatch.setattr("atlas.dashboard.auth._get_credentials", lambda: ("test", "test"))
    import atlas.dashboard.api.live as live_mod
    import atlas.execution.gates as gates_mod
    import atlas.execution.registry as reg
    from atlas.execution import kill_switch as ks

    monkeypatch.setattr(live_mod, "_LIVE", tmp_path)
    monkeypatch.setattr(gates_mod, "LIVE_DATA", tmp_path)
    monkeypatch.setattr(reg, "REGISTRY_PATH", tmp_path / "live_strategies.json")
    monkeypatch.setattr(ks, "check_all_layers", lambda **k: None)

    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(live_mod.router)
    return TestClient(app), tmp_path


def _seed_strategy(tmp_path, name="strat_x"):
    (tmp_path / "live_strategies.json").write_text(json.dumps([{
        "name": name, "provider": name, "state": "shadow", "broker": "alpaca",
        "capital": 10000.0, "approved": False, "specs": {},
        "expectation": {"daily_mean": 0.0005, "daily_std": 0.01, "sharpe": 0.62},
    }]))
    d = tmp_path / name
    d.mkdir()
    (d / "fills.jsonl").write_text("\n".join(json.dumps(
        {"date": "2026-06-10", "ticker": "AAA", "side": "BUY", "qty": 5,
         "decision_px": 100.0, "fill_px": 100.05, "filled_qty": 5,
         "status": "filled", "slippage_bps": bps, "order_id": f"o{i}"})
        for i, bps in enumerate((4.0, 6.0, 9.0))) + "\n")
    (d / "runs.jsonl").write_text(json.dumps(
        {"date": "2026-06-10", "state": "shadow", "dry_run": False, "n_orders": 3,
         "turnover": 500.0, "blocked": None, "track": "insufficient",
         "orders": [{"ticker": "AAA", "side": "BUY", "qty": 5, "px": 100.0,
                     "order_id": "o0", "ok": True}]}) + "\n")
    (d / "returns.jsonl").write_text("\n".join(json.dumps(
        {"date": f"2026-05-{i:02d}", "ret": 0.001, "equity": 10000 + i})
        for i in range(1, 26)) + "\n")
    (d / "book.json").write_text(json.dumps(
        {"cash": 5000.0, "positions": {"AAA": 50}, "capital_base": 10000.0}))
    (d / "equity_state.json").write_text(json.dumps({"equity": 10250.0, "date": "2026-06-10"}))


AUTH = ("test", "test")


def test_live_shape_with_gates(app_client):
    client, tmp_path = app_client
    _seed_strategy(tmp_path)

    resp = client.get("/api/live", auth=AUTH)
    assert resp.status_code == 200
    body = resp.json()

    # legacy keys unchanged (backward compat)
    for key in ("deployed", "portfolio", "daily", "kill_switch"):
        assert key in body
    assert body["deployed"][0]["name"] == "strat_x"
    assert body["deployed"][0]["book"]["book_equity"] == 10250.0
    assert body["kill_switch"]["blocked"] is False

    # additive gates object
    g = body["gates"]["per_strategy"]["strat_x"]
    assert g["slippage"]["pass"] is True          # median 6 bps <= 16
    assert g["slippage"]["n_fills"] == 3
    assert g["broker_errors"]["pass"] is True     # 0/1 errors
    assert g["track"]["status"] == "on_track"     # 25 obs, positive mean
    assert g["pass"] is True
    overall = body["gates"]["overall"]
    assert overall["pass"] is True and overall["n_strategies"] == 1


def test_live_never_500_empty(app_client):
    client, _ = app_client  # no registry file, no live data at all
    resp = client.get("/api/live", auth=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["deployed"] == []
    assert body["gates"]["per_strategy"] == {}
    assert body["gates"]["overall"]["pass"] is None


def test_live_never_500_when_gates_raise(app_client, monkeypatch):
    client, tmp_path = app_client
    _seed_strategy(tmp_path)
    import atlas.execution.gates as gates_mod
    monkeypatch.setattr(gates_mod, "evaluate_gates",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    resp = client.get("/api/live", auth=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["deployed"]  # legacy sections intact
    # gates degraded but present (empty per_strategy after per-strategy failure)
    assert body["gates"]["per_strategy"] == {}
