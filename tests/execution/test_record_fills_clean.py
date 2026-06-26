"""Leg B Phase 2: clean official-open slippage in record_fills (replaces contaminated decision_px)."""
from atlas.execution import record_fills as rf


def test_slippage_sign_convention():
    # BUY: filling ABOVE the reference is adverse (positive)
    assert rf._slippage_bps("BUY", 100.0, 101.0) == 100.0       # +100bps adverse
    assert rf._slippage_bps("BUY", 100.0, 99.0) == -100.0       # filled cheap = favorable
    # SELL: filling BELOW the reference is adverse (positive)
    assert rf._slippage_bps("SELL", 100.0, 99.0) == 100.0       # received less = adverse
    assert rf._slippage_bps("SELL", 100.0, 101.0) == -100.0
    # missing reference -> 0 (never fabricate a number)
    assert rf._slippage_bps("BUY", 0.0, 101.0) == 0.0
    assert rf._slippage_bps("BUY", 100.0, 0.0) == 0.0


def test_clean_fields_from_official_open():
    ref = {"open": 50.0, "prev_close": 49.0}
    out = rf._clean_fields("BUY", 50.5, ref)
    assert out["official_open"] == 50.0
    assert out["slippage_open_bps"] == round((50.5 - 50.0) / 50.0 * 1e4, 2)   # +100bps
    assert out["prev_close"] == 49.0
    assert out["slippage_prevclose_bps"] == round((50.5 - 49.0) / 49.0 * 1e4, 2)
    # no fill / no ref -> empty (never a fake clean field)
    assert rf._clean_fields("BUY", 0.0, ref) == {}
    assert rf._clean_fields("BUY", 50.5, None) == {}
    # partial ref (open only) still yields the open measure, no prev_close keys
    only_open = rf._clean_fields("SELL", 50.0, {"open": 50.0, "prev_close": None})
    assert "official_open" in only_open and "prev_close" not in only_open


def test_fetch_open_map_graceful_on_empty():
    assert rf._fetch_open_map([], []) == {}
    assert rf._fetch_open_map(["AAPL"], []) == {}


def test_status_written_lowercase(tmp_path, monkeypatch):
    """FIX 4: reconcile_book normalises broker fill status to lowercase at write time.

    The broker can return status strings in any casing (e.g. 'FILLED', 'Filled').
    reconcile_book must always write the lowercase canonical form so that
    fill_quality's case-insensitive classifier and future readers never see
    'FILLED'/'filled' coexist in the same fills.jsonl.
    """
    import json

    # rf.LIVE_DATA is already patched to tmp_path/"live" by conftest._isolate_live_data
    book_dir = rf.LIVE_DATA / "status_norm_book"
    book_dir.mkdir(parents=True, exist_ok=True)

    run = {
        "date": "2026-06-10", "dry_run": False, "blocked": None,
        "orders": [{"ticker": "AAPL", "side": "BUY", "qty": 10,
                    "px": 100.0, "order_id": "ord-norm-001"}],
    }
    (book_dir / "runs.jsonl").write_text(json.dumps(run) + "\n")
    (book_dir / "fills.jsonl").write_text("")

    # Suppress market-data fetch (no live calls in tests)
    monkeypatch.setattr(rf, "_fetch_open_map", lambda *a, **kw: {})

    # Suppress VirtualBook (book-state update not relevant for this test)
    import atlas.execution.virtual_book as _vb
    monkeypatch.setattr(_vb.VirtualBook, "__init__", lambda self, name: None)
    monkeypatch.setattr(_vb.VirtualBook, "apply_fill", lambda *a, **kw: None)
    monkeypatch.setattr(_vb.VirtualBook, "save", lambda *a, **kw: None)

    class _MockStatus:
        value = "FILLED"   # broker returns UPPERCASE — must be normalised

    class _MockResult:
        status = _MockStatus()
        fill_price = 100.5
        filled_qty = 10

    class _MockBroker:
        def get_order_status(self, oid):
            return _MockResult()

    rf.reconcile_book("status_norm_book", _MockBroker())

    fills = rf._jsonl(book_dir / "fills.jsonl")
    assert len(fills) == 1, f"expected 1 fill written, got {len(fills)}"
    assert fills[0]["status"] == "filled", (
        f"status must be lowercase 'filled', got {fills[0]['status']!r}"
    )
