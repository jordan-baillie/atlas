"""L4 drawdown layer — re-pointed at live books (data/live/<name>/returns.jsonl).

RED-first discipline: the breach case MUST trip (the old implementation read a
stale sqlite table and could never fire — permanently fail-open in production).
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from atlas.execution import kill_switch as ks
from atlas.execution import registry


def _book(root, name, equities, start_days_ago=None):
    d = root / "data" / "live" / name
    d.mkdir(parents=True)
    n = len(equities)
    start = datetime.now(timezone.utc) - timedelta(days=start_days_ago or n)
    rows = []
    for i, eq in enumerate(equities):
        rows.append({"date": (start + timedelta(days=i)).strftime("%Y-%m-%d"),
                     "ret": 0.0, "equity": eq})
    (d / "returns.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


@pytest.fixture
def live_root(tmp_path, monkeypatch):
    monkeypatch.setattr(ks, "PROJECT_ROOT", tmp_path)
    # registry with one shadow strategy named "book1"
    reg = tmp_path / "config" / "live_strategies.json"
    reg.parent.mkdir(parents=True)
    reg.write_text(json.dumps([{"name": "book1", "provider": "book1",
                                "state": "shadow", "broker": "alpaca",
                                "capital": 10_000.0}]))
    monkeypatch.setattr(registry, "REGISTRY_PATH", reg, raising=False)
    if hasattr(registry, "_REGISTRY"):
        monkeypatch.setattr(registry, "_REGISTRY", None, raising=False)
    return tmp_path


def test_l4_trips_on_breach(live_root):
    """6% drawdown from peak >= 5% threshold -> the layer MUST fire."""
    _book(live_root, "book1", [10_000, 10_200, 9_900, 9_588])  # peak 10200 -> 9588 = -6.0%
    r = ks.check_l4_drawdown()
    assert r is not None, "L4 failed to trip on a 6% drawdown (fail-open regression)"
    assert r.layer == "L4"
    assert r.detail["strategy"] == "book1"
    assert r.detail["drawdown_pct"] == pytest.approx(6.0, abs=0.1)


def test_l4_clear_within_threshold(live_root):
    """2% drawdown < 5% threshold -> clear."""
    _book(live_root, "book1", [10_000, 10_200, 10_100, 9_996])  # -2.0% from peak
    assert ks.check_l4_drawdown() is None


def test_l4_new_book_too_few_points(live_root):
    """A 1-day-old book must not crash or trip (graceful no-data path)."""
    _book(live_root, "book1", [10_000])
    assert ks.check_l4_drawdown() is None


def test_l4_no_returns_file(live_root):
    """Registered book with no returns.jsonl yet -> skip, no crash."""
    assert ks.check_l4_drawdown() is None


def test_l4_old_breach_outside_window(live_root):
    """A breach 90 days ago with full recovery inside the 30d window -> clear."""
    eq = [10_000, 9_000] + [9_500 + i for i in range(60)]  # old crash, then steady grind up
    _book(live_root, "book1", eq, start_days_ago=90)
    assert ks.check_l4_drawdown(window_days=30) is None
