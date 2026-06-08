"""Rail 1 holdout tests (pure logic; no backtest). See research/INTEGRITY_RAILS_SPEC.md."""
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT))

from research.cross_oos import holdout as H  # noqa: E402


def test_config_hash_deterministic_and_order_independent():
    a = H.config_hash("csm", {"top_n": 30, "w_qual": 0.5}, "sp500")
    b = H.config_hash("csm", {"w_qual": 0.5, "top_n": 30}, "sp500")   # key order swapped
    assert a == b
    assert a != H.config_hash("csm", {"top_n": 20}, "sp500")
    assert a != H.config_hash("csm", {"top_n": 30, "w_qual": 0.5}, "asx")


def test_holdout_gate_pass():
    ok, reasons = H.holdout_gate(holdout_sharpe=0.8, degradation_pct=-20.0, deployment_passed=True)
    assert ok is True and reasons == []


def test_holdout_gate_fails_on_negative_sharpe():
    ok, reasons = H.holdout_gate(-0.1, -10.0, True)
    assert ok is False and any("holdout_sharpe" in r for r in reasons)


def test_holdout_gate_fails_on_degradation():
    ok, reasons = H.holdout_gate(0.5, -70.0, True)
    assert ok is False and any("degradation" in r for r in reasons)


def test_holdout_gate_fails_on_deployment():
    ok, reasons = H.holdout_gate(0.9, -10.0, False)
    assert ok is False and any("deployment" in r for r in reasons)


def test_ledger_single_use(tmp_path, monkeypatch):
    led = tmp_path / "holdout_ledger.jsonl"
    monkeypatch.setattr(H, "LEDGER", led)
    h = H.config_hash("csm", {"top_n": 30}, "sp500")
    assert H.ledger_lookup(h) is None                      # not used yet
    H.ledger_append({"config_hash": h, "passed": True, "holdout_sharpe": 0.7})
    rec = H.ledger_lookup(h)
    assert rec is not None and rec["config_hash"] == h     # now recorded -> single-use blocks reuse
    assert H.ledger_lookup("deadbeef") is None             # other configs unaffected


def test_holdout_config_loads():
    cfg = H.load_holdout_config()
    assert cfg is not None and cfg.get("holdout_start")    # config/holdout.json exists & valid
    assert H.holdout_start_ts() is not None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
