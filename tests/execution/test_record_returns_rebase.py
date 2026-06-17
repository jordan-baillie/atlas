"""record_returns re-basing guard: a daily 'return' must NEVER bridge a capital_base reset.

2026-06-16: a drift-correction re-based val_mom_trend_smallcap's book $14,500 -> $5,000 but
left the recorder's equity_state at the stale equity, so the next cycle logged a phantom -66%
return that tripped the L4 drawdown breaker and froze the whole forward-paper track. Guard: if
the book's capital_base changed since the last record, reset the baseline and emit NO return
across the discontinuity. Long-running continuity (same capital_base) is byte-identical.
"""
import json
import types

import pytest

from atlas.execution import record_returns as rr


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "LIVE_DATA", tmp_path)
    s = types.SimpleNamespace(name="b", broker="alpaca")
    (tmp_path / s.name).mkdir(parents=True)
    return s, tmp_path / s.name


def _state(d, **kw):
    (d / "equity_state.json").write_text(json.dumps(kw))


def test_rebase_emits_no_return(book, monkeypatch):
    """capital_base $14,500 -> $5,000: no return row, baseline reset, rebaselined flag set."""
    s, d = book
    _state(d, equity=14769.06, date="2026-06-16", capital_base=14500.0)
    monkeypatch.setattr(rr, "_strategy_equity", lambda x: 5000.0)   # book reset to flat
    monkeypatch.setattr(rr, "_capital_base", lambda x: 5000.0)      # re-based
    out = rr.record_one(s, "2026-06-17")
    assert out.get("rebaselined") == {"from_capital_base": 14500.0, "to_capital_base": 5000.0}
    rj = d / "returns.jsonl"
    assert (not rj.exists()) or rj.read_text().strip() == ""        # NO phantom return logged
    st = json.loads((d / "equity_state.json").read_text())
    assert st["capital_base"] == 5000.0 and st["equity"] == 5000.0  # continuity restored forward


def test_continuous_day_emits_real_return(book, monkeypatch):
    """Same capital_base: a normal +1% day is recorded as a real return (unchanged behaviour)."""
    s, d = book
    _state(d, equity=5000.0, date="2026-06-17", capital_base=5000.0)
    monkeypatch.setattr(rr, "_strategy_equity", lambda x: 5050.0)
    monkeypatch.setattr(rr, "_capital_base", lambda x: 5000.0)
    out = rr.record_one(s, "2026-06-18")
    assert out["ret"] == pytest.approx(0.01, abs=1e-6)
    rows = (d / "returns.jsonl").read_text().splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["ret"] == pytest.approx(0.01, abs=1e-6)


def test_legacy_no_book_unchanged(book, monkeypatch):
    """No book.json -> _capital_base None -> guard inert, legacy account-equity behaviour kept."""
    s, d = book
    _state(d, equity=100.0, date="2026-06-17")     # legacy state without capital_base
    monkeypatch.setattr(rr, "_strategy_equity", lambda x: 101.0)
    monkeypatch.setattr(rr, "_capital_base", lambda x: None)
    out = rr.record_one(s, "2026-06-18")
    assert out["ret"] == pytest.approx(0.01, abs=1e-6)
