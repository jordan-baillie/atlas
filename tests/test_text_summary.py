"""
Tests for research.discovery.text_summary (#314 upgrade).

All tests are self-contained — no DB writes, no network calls.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_ticker_data(n: int = 60, trend: float = 0.001) -> dict:
    """Return a synthetic ticker_data dict with OHLCV arrays (ascending)."""
    rng = np.random.RandomState(42)
    closes = 100.0 * np.cumprod(1 + trend + rng.normal(0, 0.01, n))
    volumes = rng.uniform(800_000, 1_200_000, n)
    opens = closes * 0.999
    highs = closes * 1.005
    lows = closes * 0.995
    return {
        "close": closes.tolist(),
        "volume": volumes.tolist(),
        "open": opens.tolist(),
        "high": highs.tolist(),
        "low": lows.tolist(),
    }


# ── Test 1: Volume features added ─────────────────────────────────────────────


def test_volume_features_added():
    """add_volume_features populates obv_slope_20d, pv_divergence, volume_trend."""
    from research.discovery.text_summary import add_volume_features

    td = _make_ticker_data(n=60)
    base = {"trend": "bullish", "rsi": 58.0}
    result = add_volume_features(base, td)

    # OBV slope must be present and labelled
    assert "obv_slope_20d" in result, "obv_slope_20d key missing"
    assert isinstance(result["obv_slope_20d"], float)

    # volume_trend must be one of the expected strings
    assert result["volume_trend"] in ("rising", "falling", "flat")

    # pv_divergence must be bool
    assert isinstance(result["pv_divergence"], bool)

    # volume_vs_median must be a positive float
    assert isinstance(result["volume_vs_median"], float)
    assert result["volume_vs_median"] > 0

    # candle_pattern must be a string
    assert isinstance(result["candle_pattern"], str)

    # Build a text summary and check OBV appears
    from research.discovery.text_summary import structure_summary
    summary = structure_summary(result)
    assert "OBV slope" in summary, f"Expected 'OBV slope:' in summary, got:\n{summary}"


# ── Test 2: Summary structured by section ─────────────────────────────────────


def test_summary_structured_by_section():
    """structure_summary returns string with ## Price Action, ## Volume, ## Risk Overlay."""
    from research.discovery.text_summary import structure_summary

    sample = {
        "trend": "bullish",
        "rsi": 55.0,
        "rsi_status": "neutral",
        "volume_ratio": 1.2,
        "obv_slope_20d": 0.0012,
        "volume_trend": "rising",
        "pv_divergence": False,
        "regime": "bull_risk_on",
        "candle_pattern": "none",
    }
    summary = structure_summary(sample)

    for header in ("## Price Action", "## Volume", "## Risk Overlay"):
        assert header in summary, (
            f"Expected section header '{header}' in summary, got:\n{summary}"
        )

    # Volatility and Indicators sections also expected
    assert "## Volatility" in summary
    assert "## Indicators" in summary


# ── Test 3: Cross-asset context includes regime ────────────────────────────────


def test_cross_asset_context_includes_regime():
    """add_cross_asset_context injects a 'regime' key; structure_summary surfaces it."""
    from research.discovery.text_summary import add_cross_asset_context, structure_summary

    # Patch get_current_regime_state to return a known value.
    with patch(
        "research.discovery.text_summary.get_current_regime_state",
        return_value="bull_risk_on",
    ):
        enriched = add_cross_asset_context({"trend": "bullish"}, market_id="sp500")

    assert enriched.get("regime") == "bull_risk_on", (
        f"Expected regime='bull_risk_on', got {enriched.get('regime')!r}"
    )

    summary = structure_summary(enriched)
    assert "Regime: bull_risk_on" in summary or "bull" in summary.lower(), (
        f"Expected regime in summary, got:\n{summary}"
    )


def test_cross_asset_context_regime_fallback_on_db_error():
    """DB error → regime defaults to 'unknown', no exception raised."""
    from research.discovery.text_summary import add_cross_asset_context

    with patch(
        "research.discovery.text_summary.get_current_regime_state",
        side_effect=RuntimeError("db offline"),
    ):
        result = add_cross_asset_context({}, market_id="sp500")

    # Should not raise; regime key defaults to "unknown"
    assert result.get("regime") == "unknown"


# ── Test 4: Telemetry logs feature inclusion ──────────────────────────────────


def test_telemetry_logs_feature_inclusion(tmp_path, monkeypatch):
    """log_summary_telemetry appends a JSON line to the telemetry log."""
    import research.discovery.text_summary as ts_mod

    log_path = tmp_path / "text_summary_telemetry.log"
    monkeypatch.setattr(ts_mod, "_TELEMETRY_LOG", log_path)

    sample = {
        "trend": "bullish",
        "rsi": 60.0,
        "obv_slope_20d": 0.0015,
        "pv_divergence": False,
        "volume_trend": "rising",
        "candle_pattern": "none",
        "regime": "bull_risk_on",
    }

    ts_mod.log_summary_telemetry(sample, ticker="AAPL")

    assert log_path.exists(), "Telemetry log file was not created"
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1, f"Expected 1 log line, got {len(lines)}"

    entry = json.loads(lines[0])
    assert entry["ticker"] == "AAPL"
    assert "features" in entry
    assert isinstance(entry["features"], list)
    assert entry["n_features"] > 0
    assert "regime" in entry["features"]
    assert entry["has_regime"] is True
    assert "summary_chars" in entry


def test_telemetry_is_non_fatal_on_write_error(tmp_path, monkeypatch):
    """I/O error in telemetry must never propagate — just logs a warning."""
    import research.discovery.text_summary as ts_mod

    # Point log path to a dir that cannot be written to (file as parent dir)
    bad_path = tmp_path / "is_a_file" / "log.txt"
    bad_path.parent.touch()  # create a *file* where a directory is expected
    monkeypatch.setattr(ts_mod, "_TELEMETRY_LOG", bad_path)

    # Should not raise
    ts_mod.log_summary_telemetry({"trend": "bullish"}, ticker="TEST")


# ── Test 5: Feature flag OFF → flat format ────────────────────────────────────


def test_feature_flag_off_returns_flat_format(monkeypatch):
    """With TEXT_SUMMARY_V2_ENABLED=False, build_enriched_summary returns simple blob."""
    import research.discovery.text_summary as ts_mod

    monkeypatch.setattr(ts_mod, "TEXT_SUMMARY_V2_ENABLED", False)

    td = _make_ticker_data()
    result = ts_mod.build_enriched_summary(
        ticker_data=td,
        base_fields={"trend": "bullish", "rsi": 58.0},
        market_id="sp500",
    )
    # Flat format: no section headers
    assert "## " not in result
    assert "trend=bullish" in result or "trend" in result


def test_feature_flag_on_returns_sections(monkeypatch):
    """With TEXT_SUMMARY_V2_ENABLED=True, output contains ## headers."""
    import research.discovery.text_summary as ts_mod

    monkeypatch.setattr(ts_mod, "TEXT_SUMMARY_V2_ENABLED", True)

    td = _make_ticker_data()

    # Patch DB calls to avoid touching prod
    with (
        patch("research.discovery.text_summary.get_current_regime_state", return_value="bull_risk_on"),
        patch("research.discovery.text_summary.get_db", side_effect=RuntimeError("no db")),
    ):
        monkeypatch.setattr(ts_mod, "_TELEMETRY_LOG", Path("/tmp/atlas_test_telemetry.log"))
        result = ts_mod.build_enriched_summary(
            ticker_data=td,
            base_fields={"trend": "bullish", "rsi": 55.0, "rsi_status": "neutral",
                         "volume_ratio": 1.1},
            market_id="sp500",
            ticker="SPY",
        )

    assert "## Price Action" in result
    assert "## Volume" in result
    assert "## Risk Overlay" in result


# ── Test 6: Candle-pattern detection ─────────────────────────────────────────


def test_candle_pattern_detects_doji():
    """A bar with tiny body relative to range → 'doji'."""
    from research.discovery.text_summary import _detect_candle_pattern

    # Range=10, body=0.5 → body_ratio=0.05 < 0.1 → doji
    opens = np.array([100.0, 105.0])
    closes = np.array([100.0, 105.25])  # body=0.25, range=5 → ratio=0.05
    highs = np.array([102.0, 110.0])
    lows = np.array([98.0, 105.0])

    pattern = _detect_candle_pattern(opens, highs, lows, closes)
    assert pattern == "doji", f"Expected 'doji', got {pattern!r}"


def test_candle_pattern_none_on_insufficient_data():
    """Single bar → cannot determine pattern → 'none'."""
    from research.discovery.text_summary import _detect_candle_pattern

    opens = np.array([100.0])
    closes = np.array([101.0])
    highs = np.array([102.0])
    lows = np.array([99.0])
    assert _detect_candle_pattern(opens, highs, lows, closes) == "none"
