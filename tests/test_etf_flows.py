"""Tests for signals.etf_flows — ETF Flow Proxy signal."""
from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from signals.etf_flows import (
    CYCLICAL_ETFS,
    DEFENSIVE_ETFS,
    DROUGHT_THRESHOLD,
    SURGE_THRESHOLD,
    detect_rotation,
    get_etf_flow_signal,
    compute_volume_zscores,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_zscores_df(cyc_z: float, def_z: float) -> pd.DataFrame:
    """Build a minimal z-score DataFrame with controlled values."""
    rows = []
    for t in CYCLICAL_ETFS:
        signal = "surge" if cyc_z > SURGE_THRESHOLD else ("drought" if cyc_z < DROUGHT_THRESHOLD else "normal")
        rows.append({"ticker": t, "volume_zscore": cyc_z, "signal": signal})
    for t in DEFENSIVE_ETFS:
        signal = "surge" if def_z > SURGE_THRESHOLD else ("drought" if def_z < DROUGHT_THRESHOLD else "normal")
        rows.append({"ticker": t, "volume_zscore": def_z, "signal": signal})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# detect_rotation — signal classification
# ---------------------------------------------------------------------------

class TestDetectRotation:
    def test_empty_df_returns_neutral(self):
        result = detect_rotation(pd.DataFrame())
        assert result["rotation_signal"] == "neutral"
        assert result["confidence"] == 0.0

    def test_risk_on_cyclicals_up_defensives_flat(self):
        df = _make_zscores_df(cyc_z=1.5, def_z=0.0)
        result = detect_rotation(df)
        assert result["rotation_signal"] == "risk_on"
        assert result["confidence"] > 0.0

    def test_risk_off_defensives_up_cyclicals_flat(self):
        df = _make_zscores_df(cyc_z=0.0, def_z=1.5)
        result = detect_rotation(df)
        assert result["rotation_signal"] == "risk_off"
        assert result["confidence"] > 0.0

    def test_neutral_both_flat(self):
        df = _make_zscores_df(cyc_z=0.1, def_z=0.2)
        result = detect_rotation(df)
        assert result["rotation_signal"] == "neutral"

    def test_neutral_both_elevated(self):
        """Both sides elevated → neither clear rotation."""
        df = _make_zscores_df(cyc_z=1.5, def_z=1.5)
        result = detect_rotation(df)
        assert result["rotation_signal"] == "neutral"

    def test_confidence_capped_at_1(self):
        """Very large divergence should not push confidence above 1.0."""
        df = _make_zscores_df(cyc_z=10.0, def_z=-5.0)
        result = detect_rotation(df)
        assert result["confidence"] <= 1.0

    def test_risk_on_via_surge_drought_count(self):
        """2+ cyclical surges + 1 defensive drought → risk_on even if avg z is low."""
        rows = [
            {"ticker": "XLK", "volume_zscore": 2.5, "signal": "surge"},
            {"ticker": "XLF", "volume_zscore": 2.5, "signal": "surge"},
            {"ticker": "XLI", "volume_zscore": 0.5, "signal": "normal"},
            {"ticker": "XLY", "volume_zscore": 0.5, "signal": "normal"},
            {"ticker": "XLU", "volume_zscore": -2.0, "signal": "drought"},
            {"ticker": "XLP", "volume_zscore": 0.3, "signal": "normal"},
            {"ticker": "XLV", "volume_zscore": 0.3, "signal": "normal"},
        ]
        df = pd.DataFrame(rows)
        result = detect_rotation(df)
        assert result["rotation_signal"] == "risk_on"
        assert result["confidence"] == 0.7

    def test_risk_off_via_surge_drought_count(self):
        """2+ defensive surges + 1 cyclical drought → risk_off."""
        rows = [
            {"ticker": "XLK", "volume_zscore": -2.0, "signal": "drought"},
            {"ticker": "XLF", "volume_zscore": 0.3, "signal": "normal"},
            {"ticker": "XLI", "volume_zscore": 0.3, "signal": "normal"},
            {"ticker": "XLY", "volume_zscore": 0.3, "signal": "normal"},
            {"ticker": "XLU", "volume_zscore": 2.5, "signal": "surge"},
            {"ticker": "XLP", "volume_zscore": 2.5, "signal": "surge"},
            {"ticker": "XLV", "volume_zscore": 0.2, "signal": "normal"},
        ]
        df = pd.DataFrame(rows)
        result = detect_rotation(df)
        assert result["rotation_signal"] == "risk_off"

    def test_result_contains_required_keys(self):
        df = _make_zscores_df(cyc_z=0.0, def_z=0.0)
        result = detect_rotation(df)
        for key in ("rotation_signal", "confidence", "cyclical_avg_zscore", "defensive_avg_zscore", "details"):
            assert key in result, f"Missing key: {key}"

    def test_cyclical_defensive_averages_correct(self):
        df = _make_zscores_df(cyc_z=1.2, def_z=-0.4)
        result = detect_rotation(df)
        assert abs(result["cyclical_avg_zscore"] - 1.2) < 0.01
        assert abs(result["defensive_avg_zscore"] - (-0.4)) < 0.01


# ---------------------------------------------------------------------------
# get_etf_flow_signal — integration (mocked download)
# ---------------------------------------------------------------------------

def _make_fake_raw(tickers, n_rows=30):
    """Build a fake multi-index DataFrame as yfinance would return."""
    dates = pd.date_range("2026-01-01", periods=n_rows, freq="B")
    rng = np.random.default_rng(42)
    arrays = [
        ["Volume"] * len(tickers),
        tickers,
    ]
    cols = pd.MultiIndex.from_arrays(arrays)
    data = rng.integers(1_000_000, 50_000_000, size=(n_rows, len(tickers))).astype(float)
    # Make last row a "surge" for XLK to exercise the path
    data[-1, tickers.index("XLK")] = data[:-1, tickers.index("XLK")].mean() * 5
    return pd.DataFrame(data, index=dates, columns=cols)


class TestGetEtfFlowSignal:
    def test_returns_required_keys(self):
        tickers = list(__import__("signals.etf_flows", fromlist=["SECTOR_ETFS"]).SECTOR_ETFS.keys())
        fake_raw = _make_fake_raw(tickers)
        with patch("yfinance.download", return_value=fake_raw):
            result = get_etf_flow_signal()
        for key in ("rotation_signal", "confidence", "cyclical_avg_zscore", "defensive_avg_zscore", "details", "zscores"):
            assert key in result, f"Missing key: {key}"

    def test_rotation_signal_is_valid_value(self):
        tickers = list(__import__("signals.etf_flows", fromlist=["SECTOR_ETFS"]).SECTOR_ETFS.keys())
        fake_raw = _make_fake_raw(tickers)
        with patch("yfinance.download", return_value=fake_raw):
            result = get_etf_flow_signal()
        assert result["rotation_signal"] in ("risk_on", "risk_off", "neutral")

    def test_confidence_in_range(self):
        tickers = list(__import__("signals.etf_flows", fromlist=["SECTOR_ETFS"]).SECTOR_ETFS.keys())
        fake_raw = _make_fake_raw(tickers)
        with patch("yfinance.download", return_value=fake_raw):
            result = get_etf_flow_signal()
        assert 0.0 <= result["confidence"] <= 1.0

    def test_zscores_list_has_entries(self):
        tickers = list(__import__("signals.etf_flows", fromlist=["SECTOR_ETFS"]).SECTOR_ETFS.keys())
        fake_raw = _make_fake_raw(tickers)
        with patch("yfinance.download", return_value=fake_raw):
            result = get_etf_flow_signal()
        assert len(result["zscores"]) > 0

    def test_zscore_entry_has_required_fields(self):
        tickers = list(__import__("signals.etf_flows", fromlist=["SECTOR_ETFS"]).SECTOR_ETFS.keys())
        fake_raw = _make_fake_raw(tickers)
        with patch("yfinance.download", return_value=fake_raw):
            result = get_etf_flow_signal()
        z = result["zscores"][0]
        for field in ("ticker", "name", "volume", "avg_volume_20d", "volume_zscore", "signal", "date"):
            assert field in z, f"Missing field: {field}"

    def test_empty_download_returns_neutral(self):
        with patch("yfinance.download", return_value=pd.DataFrame()):
            result = get_etf_flow_signal()
        assert result["rotation_signal"] == "neutral"
        assert result["zscores"] == []

    def test_xlk_surge_detected(self):
        """XLK with 5x avg volume should register as a surge."""
        tickers = list(__import__("signals.etf_flows", fromlist=["SECTOR_ETFS"]).SECTOR_ETFS.keys())
        fake_raw = _make_fake_raw(tickers)
        with patch("yfinance.download", return_value=fake_raw):
            result = get_etf_flow_signal()
        xlk_entry = next((z for z in result["zscores"] if z["ticker"] == "XLK"), None)
        assert xlk_entry is not None
        assert xlk_entry["signal"] == "surge"
        assert xlk_entry["volume_zscore"] > SURGE_THRESHOLD
