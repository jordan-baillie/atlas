"""Rail 2 tests: FDR-aware promote bar + hypothesis registry. See INTEGRITY_RAILS_SPEC.md."""
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT))

from research.cross_oos.adapter import promote_dsr, PROMOTE_DSR, PROMOTE_DSR_CAP  # noqa: E402
from research.cross_oos import registry as R  # noqa: E402


def test_promote_dsr_base_case_regression_safe():
    assert promote_dsr(1) == PROMOTE_DSR        # 1 family -> 0.90 (today's behaviour)
    assert promote_dsr(0) == PROMOTE_DSR        # guard: <1 treated as 1


def test_promote_dsr_escalates_and_caps():
    assert promote_dsr(4) == 0.95               # 1 - 0.10/2
    assert abs(promote_dsr(9) - 0.9666666) < 1e-4
    # monotonic non-decreasing
    vals = [promote_dsr(n) for n in (1, 2, 4, 9, 25, 100, 10000)]
    assert all(b >= a for a, b in zip(vals, vals[1:]))
    assert promote_dsr(10_000) == PROMOTE_DSR_CAP   # capped
    assert all(v <= PROMOTE_DSR_CAP for v in vals)


def test_a_marginal_promote_no_longer_promotes_when_many_families():
    """A DSR that cleared 0.90 with 1 family must fail once many families have been tried."""
    marginal_dsr = 0.926                         # exactly the csm bug-artifact value
    assert marginal_dsr >= promote_dsr(1)        # would PROMOTE alone
    assert marginal_dsr < promote_dsr(9)         # fails the FDR-aware bar after 9 ideas


def test_registry_distinct_families(tmp_path, monkeypatch):
    reg = tmp_path / "hypothesis_registry.jsonl"
    monkeypatch.setattr(R, "REGISTRY", reg)
    assert R.distinct_families() == 1            # empty -> base 1
    R.append_run({"family": "csm", "final_tier": "FAIL"})
    R.append_run({"family": "csm", "final_tier": "FAIL"})   # same family, 2nd run
    R.append_run({"family": "pairs", "final_tier": "FAIL"})
    assert R.distinct_families() == 2            # csm + pairs (configs don't inflate)
    assert R.distinct_families(extra="news") == 3            # not-yet-logged family counted
    assert R.distinct_families(extra="csm") == 2            # existing family not double counted


def test_family_of():
    assert R.family_of("cross_sectional_momentum") == "cross_sectional_momentum"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
