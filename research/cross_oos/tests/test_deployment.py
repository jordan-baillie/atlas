"""Rail 3 deployment-sanity tests. See research/INTEGRITY_RAILS_SPEC.md."""
import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT))

from research.cross_oos.deployment import deployment_sanity, expected_positions  # noqa: E402

SP500_META = {"max_positions": 35, "max_sector_concentration": 2}  # expected = min(35, 2*11)=22
SECTORS = ["Technology", "Healthcare", "Energy", "Financials", "Industrials",
           "Utilities", "Materials", "Staples", "Discretionary", "RealEstate", "Comms"]


def _trade(ticker, start, hold, sector, pv=500.0):
    e = pd.Timestamp(start)
    return {"ticker": ticker, "entry_date": e, "exit_date": e + pd.Timedelta(days=hold),
            "hold_days": hold, "position_value": pv, "features": {"sector": sector}}


def _book(n_concurrent, n_trades, sectors=SECTORS, hold=20, step=7):
    """Build a book that holds ~n_concurrent names at once across n_trades total."""
    trades = []
    base = pd.Timestamp("2020-01-06")
    for i in range(n_trades):
        slot = i % n_concurrent
        start = base + pd.Timedelta(days=(i // n_concurrent) * (hold + step) + slot * step)
        trades.append(_trade(f"T{i % (n_concurrent*3)}", start, hold, sectors[i % len(sectors)]))
    return trades


def test_expected_positions_sector_capped():
    assert expected_positions({"top_n": 30}, SP500_META) == 22   # min(30,35,22)
    assert expected_positions({}, SP500_META) == 22              # falls back to sector cap
    assert expected_positions({"top_n": 5}, SP500_META) == 5     # top_n binds


def test_two_position_artifact_fails():
    """The csm 2026-06-05 bug: many trades but only ~2 concurrent -> must FAIL."""
    trades = _book(n_concurrent=2, n_trades=78)
    r = deployment_sanity(trades, primary_config={}, strategy_meta=SP500_META)
    assert r["peak_concurrent"] <= 3
    assert r["passed"] is False
    assert any("peak_concurrent" in x or "realized_vs_design" in x for x in r["forced_fail_reasons"])


def test_healthy_breadth_book_passes():
    """A ~14-name book across sectors with enough trades -> deployment PASS."""
    trades = _book(n_concurrent=14, n_trades=246)
    r = deployment_sanity(trades, primary_config={}, strategy_meta=SP500_META)
    assert r["peak_concurrent"] >= 8        # comfortably above the 5.5 design floor
    assert r["realized_vs_design"] >= 0.5
    assert r["passed"] is True, r["forced_fail_reasons"]


def test_too_few_trades_fails():
    trades = _book(n_concurrent=10, n_trades=20)
    r = deployment_sanity(trades, primary_config={}, strategy_meta=SP500_META)
    assert r["passed"] is False
    assert any("n_trades" in x for x in r["forced_fail_reasons"])


def test_single_name_concentration_fails():
    """Peak concurrency is fine, but one ticker dominates position-days -> FAIL."""
    trades = []
    base = pd.Timestamp("2020-01-06")
    # 60 long holds of one dominant name (overlapping) + many small others
    for i in range(60):
        trades.append(_trade("DOM", base + pd.Timedelta(days=i * 3), 120, "Technology", pv=2000.0))
    for i in range(60):
        trades.append(_trade(f"X{i}", base + pd.Timedelta(days=i * 3), 10, SECTORS[i % 11], pv=300.0))
    r = deployment_sanity(trades, primary_config={}, strategy_meta=SP500_META)
    assert r["single_name_share"] > 0.40
    assert r["passed"] is False
    assert any("single_name_share" in x for x in r["forced_fail_reasons"])


def test_empty_trades_fails():
    r = deployment_sanity([], primary_config={}, strategy_meta=SP500_META)
    assert r["passed"] is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
