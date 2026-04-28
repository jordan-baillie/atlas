"""
tests/test_alt_data_observe_mode.py

Regression tests for the alt-data observation mode (C3).

Safety contract:
  - alt_data NEVER writes to signals/
  - mode=observe is the default; unknown modes abort early
  - Structured [alt_data] log lines are emitted for observability
  - sp500.json alt_data.tickers matches data/processed/sp500/universe.json exactly
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Root of the project (used for path resolution in static analysis tests)
_ROOT = Path(__file__).resolve().parents[1]


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_cfg(tickers: list[str] | None = None, mode: str = "observe") -> dict:
    """Return a minimal alt_data config dict suitable for AltDataCollector."""
    return {
        "enabled": True,
        "mode": mode,
        "tickers": tickers if tickers is not None else ["AAPL", "MSFT"],
        "max_per_ticker": 3,
    }


# ── Test 1: scraper is invoked per ticker and does not raise ──────────────────

@patch("overlay.sources.alt_data.time.sleep")
@patch("overlay.sources.alt_data._get_with_retry")
def test_enabled_with_tickers_calls_scraper(mock_get, mock_sleep):
    """Scraper functions are called at least once per ticker; no exception raised."""
    # Return None — both scrapers gracefully handle no response
    mock_get.return_value = None

    from overlay.sources.alt_data import AltDataCollector

    cfg = _make_cfg(tickers=["AAPL", "MSFT"], mode="observe")
    collector = AltDataCollector(dry_run=True, config=cfg)
    result = collector.run()

    # _get_with_retry should have been called: 2 tickers × 2 scrapers = 4 calls
    assert mock_get.call_count >= 2, (
        f"Expected ≥2 HTTP calls for 2 tickers, got {mock_get.call_count}"
    )
    assert result is not None
    assert "tickers" in result
    assert result["tickers"] == ["AAPL", "MSFT"]


# ── Test 2: empty hits do not crash ──────────────────────────────────────────

@patch("overlay.sources.alt_data.time.sleep")
@patch("overlay.sources.alt_data._get_with_retry")
def test_enabled_empty_hits_does_not_crash(mock_get, mock_sleep):
    """When HTTP returns no data, run() completes cleanly with zero records."""
    mock_get.return_value = None  # no HTTP response → scrapers return []

    from overlay.sources.alt_data import AltDataCollector

    cfg = _make_cfg(tickers=["AAPL"], mode="observe")
    collector = AltDataCollector(dry_run=True, config=cfg)
    result = collector.run()

    assert result["openinsider_records"] == 0
    assert result["finviz_records"] == 0
    assert isinstance(result["errors"], list)
    # No crash → we reach here
    assert result["tickers"] == ["AAPL"]


# ── Test 3: alt_data NEVER appears in signals/ ────────────────────────────────

def test_alt_data_never_writes_to_signals():
    """Static regression: signals/ directory must contain zero references to alt_data."""
    signals_dir = _ROOT / "signals"
    assert signals_dir.is_dir(), f"signals/ directory not found at {signals_dir}"

    proc = subprocess.run(
        ["grep", "-rn", "--include=*.py", "alt_data", str(signals_dir)],
        capture_output=True,
        text=True,
    )
    # grep returns 1 when NO lines match — that is the desired state
    assert proc.returncode == 1, (
        f"Found alt_data references in signals/ (should be zero):\n{proc.stdout}"
    )


# ── Test 4: [alt_data] log line with mode+tickers+hits is emitted ─────────────

@patch("overlay.sources.alt_data.time.sleep")
@patch("overlay.sources.alt_data._get_with_retry")
def test_observation_log_line_emitted(mock_get, mock_sleep, caplog):
    """End-of-batch structured log line must contain mode=observe, tickers=, hits=."""
    mock_get.return_value = None

    from overlay.sources.alt_data import AltDataCollector

    cfg = _make_cfg(tickers=["AAPL", "MSFT"], mode="observe")
    collector = AltDataCollector(dry_run=True, config=cfg)

    with caplog.at_level(logging.INFO, logger="overlay.sources.alt_data"):
        collector.run()

    # Find the structured end-of-batch log line
    messages = [r.getMessage() for r in caplog.records]
    matching = [
        m for m in messages
        if m.startswith("[alt_data]")
        and "mode=observe" in m
        and "tickers=" in m
        and "hits=" in m
    ]
    assert len(matching) >= 1, (
        f"Expected ≥1 log line starting with [alt_data] and containing "
        f"mode=observe + tickers= + hits=.\n"
        f"All [alt_data] lines: {[m for m in messages if m.startswith('[alt_data]')]}\n"
        f"All messages: {messages}"
    )


# ── Test 5: alt_data.tickers in config matches sp500 universe exactly ─────────

def test_ticker_list_matches_sp500_watchlist():
    """Snapshot: sp500.json alt_data.tickers must match universe.json tickers (set equality)."""
    cfg_path = _ROOT / "config" / "active" / "sp500.json"
    universe_path = _ROOT / "data" / "processed" / "sp500" / "universe.json"

    with open(cfg_path) as f:
        cfg = json.load(f)
    with open(universe_path) as f:
        universe = json.load(f)

    alt_tickers: list[str] = cfg["alt_data"]["tickers"]
    universe_tickers: list[str] = universe["tickers"]

    assert len(alt_tickers) == len(universe_tickers), (
        f"Length mismatch: config alt_data.tickers={len(alt_tickers)}, "
        f"universe.tickers={len(universe_tickers)}"
    )
    assert set(alt_tickers) == set(universe_tickers), (
        f"Ticker sets differ.\n"
        f"In config only: {set(alt_tickers) - set(universe_tickers)}\n"
        f"In universe only: {set(universe_tickers) - set(alt_tickers)}"
    )
    # Document which equality check we use: SET equality (order may differ)
    # Exact order from universe.json is preserved in config (both sourced from same file)
    assert alt_tickers == universe_tickers, (
        "Order mismatch: lists have same elements but different order. "
        "Config was written from universe.json directly, so order should be identical."
    )
