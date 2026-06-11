"""B3 — Guard tests for price_arbiter.json authority flip (alpaca → tiingo).

Wave 1 commit was supposed to flip this but the change never landed in the
working tree. NFLX was mis-marked at $97 (real $107.79, 11.12% spread).
These tests prevent silent regression on the authority field and require
that any future change carries a documented _change_log entry.
"""
import json
from pathlib import Path

import pytest

from atlas.brokers.price_arbiter import _CONFIG_PATH as CFG_PATH


@pytest.fixture(scope="module")
def cfg():
    assert CFG_PATH.exists(), f"{CFG_PATH} must exist"
    return json.loads(CFG_PATH.read_text())


def test_authority_on_mismatch_is_tiingo(cfg):
    """authority_on_mismatch must be 'tiingo' — Alpaca IEX feed is structurally
    stale for NYSE/NASDAQ mega-caps (NFLX/AAPL/MSFT). See B3 investigation."""
    assert cfg.get("authority_on_mismatch") == "tiingo", (
        "Authority must be 'tiingo'. If you intentionally flipped this back, "
        "add a _change_log entry documenting why and update this test."
    )


def test_change_log_documents_flip(cfg):
    """_change_log must contain an entry documenting the alpaca → tiingo flip."""
    log = cfg.get("_change_log")
    assert isinstance(log, list) and len(log) > 0, (
        "_change_log must be a non-empty list documenting authority changes"
    )
    flip_entries = [
        e for e in log
        if "alpaca" in e.get("change", "").lower() and "tiingo" in e.get("change", "").lower()
    ]
    assert len(flip_entries) >= 1, "Must have at least one entry documenting the alpaca→tiingo flip"
    entry = flip_entries[0]
    assert "date" in entry and "reason" in entry


def test_required_schema_fields_present(cfg):
    """Schema validation: warn_pct, halt_pct, authority_on_mismatch must all be present."""
    assert "warn_pct" in cfg
    assert "halt_pct" in cfg
    assert "authority_on_mismatch" in cfg
    assert isinstance(cfg["warn_pct"], (int, float)) and cfg["warn_pct"] > 0
    assert isinstance(cfg["halt_pct"], (int, float)) and cfg["halt_pct"] > cfg["warn_pct"]
    assert cfg["authority_on_mismatch"] in ("tiingo", "alpaca")
