"""Tests for W5 ceasefire_factors.json freshness check (2026-04-28)."""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from overlay.sources import news as news_mod


@pytest.fixture
def tmp_ceasefire(tmp_path, monkeypatch):
    """Create a temp ceasefire_factors.json and patch the module path."""
    p = tmp_path / "ceasefire_factors.json"
    monkeypatch.setattr(news_mod, "_CEASEFIRE_JSON", p)
    return p


def test_stale_input_triggers_placeholder(tmp_ceasefire):
    """File >7 days old should return placeholder, not parse the data."""
    stale_data = {
        "probability": 43,
        "probability_label": "COIN FLIP",
        "last_updated": "2026-03-27T23:00:53",
        "factors": [],
    }
    tmp_ceasefire.write_text(json.dumps(stale_data))
    # Force file mtime to be old too
    old_ts = (datetime.now() - timedelta(days=20)).timestamp()
    os.utime(tmp_ceasefire, (old_ts, old_ts))

    result = news_mod._fetch_geopolitical_risk()
    assert result is not None
    assert "STALE" in result
    assert "43%" not in result, "stale data leaked into placeholder"


def test_fresh_input_passes_through(tmp_ceasefire):
    """File with recent last_updated should parse normally."""
    fresh_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    fresh_data = {
        "probability": 55,
        "probability_label": "MODERATE",
        "last_updated": fresh_iso,
        "factors": [
            {"active": True, "direction": "ceasefire", "weight": 0.5, "label": "test_ceasefire"},
        ],
        "portfolio_action": "no action",
    }
    tmp_ceasefire.write_text(json.dumps(fresh_data))

    result = news_mod._fetch_geopolitical_risk()
    assert result is not None
    assert "55%" in result
    assert "STALE" not in result


def test_missing_file_returns_none(monkeypatch, tmp_path):
    """Missing file returns None (existing behavior preserved)."""
    monkeypatch.setattr(news_mod, "_CEASEFIRE_JSON", tmp_path / "nonexistent.json")
    result = news_mod._fetch_geopolitical_risk()
    assert result is None


def test_malformed_json_returns_none(tmp_ceasefire):
    """Bad JSON returns None (existing behavior preserved)."""
    tmp_ceasefire.write_text("{not valid json")
    result = news_mod._fetch_geopolitical_risk()
    assert result is None


def test_no_last_updated_uses_mtime(tmp_ceasefire):
    """If last_updated missing, fall back to file mtime."""
    data = {"probability": 60, "factors": []}
    tmp_ceasefire.write_text(json.dumps(data))
    # File is brand-new → mtime is fresh → should pass through
    result = news_mod._fetch_geopolitical_risk()
    assert result is not None
    assert "STALE" not in result
    assert "60%" in result


def test_threshold_constant_present():
    """Verify the constant exists for operator visibility."""
    assert hasattr(news_mod, "_CEASEFIRE_MAX_AGE_DAYS")
    assert news_mod._CEASEFIRE_MAX_AGE_DAYS == 7
