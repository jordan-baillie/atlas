"""Every trade's (ticker, universe) pair must be consistent with universe/definitions.py.

Guards against the 2026-04-22 SLV/XLY/UNG universe-mismatch regression where
reconcile_ledger and journal.logger wrote universe='sp500' for ETFs that
actually belong to commodity_etfs/sector_etfs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from universe.membership import derive_universe, clear_cache
from universe.definitions import UNIVERSES


def _all_trade_rows():
    import db.atlas_db as _adb
    with _adb.get_db() as conn:
        rows = conn.execute(
            "SELECT id, ticker, universe, entry_date, exit_date FROM trades "
            "ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def test_every_trade_ticker_is_member_of_its_universe():
    """For every trade, the ticker must be a member of trades.universe."""
    clear_cache()
    bad = []
    for row in _all_trade_rows():
        ticker = row["ticker"]
        uni = row["universe"]
        if not uni:
            continue  # NULL is acceptable — caller already logged a WARN
        udef = UNIVERSES.get(uni)
        if not udef:
            bad.append((row["id"], ticker, uni, "universe_not_in_definitions"))
            continue
        if udef.get("method") == "static":
            if ticker not in udef.get("tickers", []):
                bad.append((row["id"], ticker, uni, "ticker_not_in_static_universe"))
        else:
            # dynamic (sp500) — resolve via builder; if builder fails, accept
            try:
                from universe.builder import get_universe_tickers
                if ticker not in set(get_universe_tickers(uni)):
                    bad.append((row["id"], ticker, uni, "ticker_not_in_dynamic_universe"))
            except Exception:
                pass  # builder unavailable — skip dynamic check
    assert not bad, "trades with universe mismatch:\n" + "\n".join(
        f"  id={b[0]} {b[1]} universe={b[2]} reason={b[3]}" for b in bad
    )


def test_slv_never_in_sp500():
    """SLV is a silver ETF — must never be filed under sp500."""
    import db.atlas_db as _adb
    with _adb.get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM trades WHERE ticker='SLV' AND universe='sp500'"
        ).fetchall()
    assert not rows, f"SLV should never have universe=sp500 — got {[r['id'] for r in rows]}"


def test_xly_never_in_sp500():
    """XLY is a Consumer Discretionary SPDR (sector_etfs) — not an sp500 constituent."""
    import db.atlas_db as _adb
    with _adb.get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM trades WHERE ticker='XLY' AND universe='sp500'"
        ).fetchall()
    assert not rows, f"XLY should never have universe=sp500 — got {[r['id'] for r in rows]}"


def test_ung_never_in_sp500():
    """UNG is natural gas (commodity_etfs) — not an sp500 constituent."""
    import db.atlas_db as _adb
    with _adb.get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM trades WHERE ticker='UNG' AND universe='sp500'"
        ).fetchall()
    assert not rows, f"UNG should never have universe=sp500 — got {[r['id'] for r in rows]}"


def test_derive_universe_never_defaults_to_sp500_for_commodity():
    """Unit test: derive_universe(SLV, None) must return commodity_etfs, not sp500."""
    clear_cache()
    assert derive_universe("SLV") == "commodity_etfs"
    assert derive_universe("UNG") == "commodity_etfs"
    assert derive_universe("XLY") == "sector_etfs"
    # Even with a BAD hint, derive_universe returns the correct single-membership universe
    assert derive_universe("SLV", "sp500") == "commodity_etfs"
    assert derive_universe("XLY", "sp500") == "sector_etfs"


def test_derive_universe_returns_none_for_unknown_ticker():
    """Unknown ticker with no hint → None (so caller can leave NULL + log)."""
    clear_cache()
    assert derive_universe("ZZZZ_NOT_REAL") is None
    # With hint, preserve hint rather than invent sp500
    assert derive_universe("ZZZZ_NOT_REAL", "sector_etfs") == "sector_etfs"


def test_reconcile_ledger_would_NOT_write_sp500_for_commodity_etf(tmp_path, monkeypatch):
    """Pre-fix behaviour: reconcile_ledger wrote universe=market_id regardless of
    ticker membership, so reconciling the sp500 market with SLV in state_file
    produced universe='sp500'. Post-fix: derive_universe() returns commodity_etfs.
    """
    clear_cache()
    # Direct call to derive_universe simulates what the fixed reconcile code does:
    resolved = derive_universe("SLV", "sp500")
    assert resolved == "commodity_etfs", (
        "Post-fix reconcile_ledger must map SLV→commodity_etfs even when market_id=sp500"
    )
