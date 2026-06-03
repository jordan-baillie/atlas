"""Tests for the rapid-pipeline orchestrator (#420)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from research import pipeline as pl  # noqa: E402


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "REGISTRY", tmp_path / "candidates.json")


def test_register_and_stage(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pl.register("cross_sectional_momentum", label="csm@default", stage="paper",
                battery_tier="SCREEN")
    rows = pl.status()
    assert len(rows) == 1 and rows[0]["stage"] == "paper" and rows[0]["battery_tier"] == "SCREEN"
    pl.set_stage("csm@default", "microlive_gate", forward_verdict="PASS")
    assert pl.status()[0]["stage"] == "microlive_gate"


def test_next_action_queued_runs_battery(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pl.register("x", label="x@q", stage="queued")
    assert pl.next_action("x@q")["action"] == "run_battery"


def test_next_action_paper_insufficient_then_pass(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pl.register("x", label="x@p", stage="paper", battery_tier="SCREEN")
    # no returns -> accumulate
    assert pl.next_action("x@p")["action"] == "accumulate_paper"
    # strong positive forward -> microlive_gate (but arm refused w/o drill+confirm)
    rng = np.random.default_rng(0)
    r = rng.normal(0.0008, 0.006, 40)
    act = pl.next_action("x@p", forward_returns=r, clv=0.3, aum=1336.0, confirmed=False)
    assert act["action"] == "microlive_gate"
    assert act["forward"]["verdict"] == "PASS"
    assert act["arm"]["status"] == "REFUSED"   # drill/confirm gate still holds


def test_next_action_forward_fail(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pl.register("x", label="x@f", stage="paper", battery_tier="SCREEN")
    rng = np.random.default_rng(1)
    r = rng.normal(-0.0007, 0.006, 40)
    assert pl.next_action("x@f", forward_returns=r)["action"] == "fail"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
