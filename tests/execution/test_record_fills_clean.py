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
