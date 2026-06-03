"""Tests for the micro-live gate + kill-switch (rapid pipeline #419)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from research import microlive_gate as mg  # noqa: E402


def test_cap_is_min_of_usd_and_pct():
    assert mg.microlive_cap(1336.0) == 133.6      # 10% of AUM < $150
    assert mg.microlive_cap(100000.0) == 150.0    # hard $150 cap binds


def test_killswitch_trips_on_drawdown():
    ks = mg.KillSwitch(100.0)
    assert not ks.update(101.0)
    assert not ks.update(95.0)        # -5.9% from peak, ok
    assert ks.update(80.0)            # -20.8% from peak -> trip
    assert ks.tripped and "drawdown" in (ks.reason or "")


def test_killswitch_does_not_trip_benign():
    ks = mg.KillSwitch(100.0)
    assert not any(ks.update(e) for e in [100, 101, 99.5, 100.4, 101.2])
    assert not ks.tripped


def test_drill_passes_and_records(tmp_path, monkeypatch):
    monkeypatch.setattr(mg, "DRILL_MARKER", tmp_path / "drill.json")
    res = mg.run_drill()
    assert res["passed"] is True and res["false_trip_on_benign"] is False
    assert mg.drill_recent()


def test_arming_refused_without_gates(tmp_path, monkeypatch):
    monkeypatch.setattr(mg, "DRILL_MARKER", tmp_path / "drill.json")  # no drill yet
    r = mg.arm_microlive("x", backtest_tier="FAIL", forward_verdict="INSUFFICIENT",
                         aum=1336.0, confirmed=False)
    assert r["status"] == "REFUSED"
    assert any("SCREEN/PROMOTE" in b for b in r["blockers"])
    assert any("forward-evidence" in b for b in r["blockers"])
    assert any("drill" in b for b in r["blockers"])
    assert any("confirmation" in b for b in r["blockers"])


def test_arming_allowed_only_when_all_gates_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(mg, "DRILL_MARKER", tmp_path / "drill.json")
    mg.run_drill()  # record a passed drill
    # still refused without confirmation
    assert mg.arm_microlive("x", backtest_tier="SCREEN", forward_verdict="PASS",
                            aum=1336.0, confirmed=False)["status"] == "REFUSED"
    # armed only with everything + confirmation
    r = mg.arm_microlive("x", backtest_tier="SCREEN", forward_verdict="PASS",
                         aum=1336.0, confirmed=True)
    assert r["status"] == "ARMED" and r["size_usd"] == 133.6


def test_revert_dry_run_does_not_write():
    plan = mg.revert_to_paper(dry_run=True, reason="unit-test")
    assert plan["dry_run"] is True and plan["to"]["mode"] == "paper"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
