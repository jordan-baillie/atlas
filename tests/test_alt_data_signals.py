"""
Tests for #220 alt-data signal pipeline:
  - signals.openinsider_signals
  - signals.finviz_signals
  - signals.alt_data_signals
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers / fixtures
# ═══════════════════════════════════════════════════════════════════════════════

def _make_insider_rows(ticker: str, buys: list[float], sells: list[float]) -> list[dict]:
    """Build synthetic news_intel rows for openinsider."""
    rows = []
    for v in buys:
        detail = {
            "trade_type": "P - Purchase",
            "insider_name": "CEO Test",
            "title": "CEO",
            "value": v,
            "qty": v / 100,
            "price": 100.0,
            "trade_date": "2026-01-05",
            "filing_date": "2026-01-07 12:00:00",
        }
        rows.append(
            {
                "id": 1,
                "timestamp": "2026-01-10 12:00:00",
                "source": "openinsider",
                "headline": f"Insider Buy: CEO bought shares of {ticker}",
                "url": f"http://openinsider.com/{ticker}",
                "relevance_score": 0.8,
                "category": "insider_trade",
                "summary": json.dumps(detail),
                "created_at": "2026-01-10 12:00:00",
            }
        )
    for v in sells:
        detail = {
            "trade_type": "S - Sale",
            "insider_name": "CFO Test",
            "title": "CFO",
            "value": -v,
            "qty": -v / 100,
            "price": 100.0,
            "trade_date": "2026-01-05",
            "filing_date": "2026-01-07 12:00:00",
        }
        rows.append(
            {
                "id": 2,
                "timestamp": "2026-01-10 12:00:00",
                "source": "openinsider",
                "headline": f"Insider Sale: CFO sold shares of {ticker}",
                "url": f"http://openinsider.com/{ticker}",
                "relevance_score": 0.6,
                "category": "insider_trade",
                "summary": json.dumps(detail),
                "created_at": "2026-01-10 12:00:00",
            }
        )
    return rows


def _make_finviz_rows(ticker: str, perf_week: str = "2.5%", short_float: str = "1.5%") -> list[dict]:
    """Build synthetic news_intel rows for finviz."""
    detail = {
        "pe": "22.5",
        "eps_ttm": "4.50",
        "perf_week": perf_week,
        "perf_month": "5.0%",
        "perf_quarter": "12.0%",
        "rel_volume": "1.2",
        "short_float": short_float,
        "inst_own": "72.0%",
        "market_cap": "500B",
    }
    return [
        {
            "id": 10,
            "timestamp": "2026-01-10 13:00:00",
            "source": "finviz",
            "headline": f"{ticker} Snapshot: P/E 22.5, Short Float 1.5%, Rel Volume 1.2x",
            "url": f"https://finviz.com/quote.ashx?t={ticker}",
            "relevance_score": 0.5,
            "category": "fundamentals",
            "summary": json.dumps(detail),
            "created_at": "2026-01-10 13:00:00",
        }
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# Task 19 — OpenInsider signals
# ═══════════════════════════════════════════════════════════════════════════════


class TestLoadOpeninsiderData:
    def test_load_openinsider_data_returns_dataframe(self):
        """load_openinsider_data always returns a DataFrame with correct schema."""
        from signals.openinsider_signals import load_openinsider_data

        rows = _make_insider_rows("AAPL", buys=[1_000_000.0], sells=[])

        with patch("signals.openinsider_signals.get_news", return_value=rows):
            df = load_openinsider_data()

        assert isinstance(df, pd.DataFrame), "Should return a DataFrame"
        required_cols = {"ticker", "trade_type", "value", "qty", "title", "timestamp"}
        assert required_cols.issubset(df.columns), (
            f"Missing columns: {required_cols - set(df.columns)}"
        )

    def test_load_returns_empty_df_on_db_error(self):
        """DB error → empty DataFrame, no exception."""
        from signals.openinsider_signals import load_openinsider_data

        with patch("signals.openinsider_signals.get_news", side_effect=RuntimeError("db down")):
            df = load_openinsider_data()

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    def test_load_filters_to_openinsider_source(self):
        """Only rows with source='openinsider' are loaded (not finviz etc.)."""
        from signals.openinsider_signals import load_openinsider_data

        mixed = _make_insider_rows("NVDA", buys=[500_000], sells=[])
        mixed[0]["source"] = "finviz"  # contaminate

        with patch("signals.openinsider_signals.get_news", return_value=mixed):
            df = load_openinsider_data()

        assert len(df) == 0, "finviz-sourced rows should be filtered out"


class TestScoreInsiderSignal:
    def test_score_insider_signal_in_range(self):
        """score_insider_signal always returns a value in [-1, 1]."""
        from signals.openinsider_signals import score_insider_signal, load_openinsider_data

        rows = _make_insider_rows("AAPL", buys=[5_000_000], sells=[1_000_000])
        with patch("signals.openinsider_signals.get_news", return_value=rows):
            df = load_openinsider_data()

        score = score_insider_signal("AAPL", df)
        assert -1.0 <= score <= 1.0, f"Score out of range: {score}"

    def test_score_positive_when_net_buying(self):
        """Net insider buying → positive score."""
        from signals.openinsider_signals import score_insider_signal, load_openinsider_data

        rows = _make_insider_rows("TSLA", buys=[10_000_000], sells=[])
        with patch("signals.openinsider_signals.get_news", return_value=rows):
            df = load_openinsider_data()

        score = score_insider_signal("TSLA", df)
        assert score > 0, f"Expected positive score for net buying, got {score}"

    def test_score_negative_when_net_selling(self):
        """Net insider selling → negative score."""
        from signals.openinsider_signals import score_insider_signal, load_openinsider_data

        rows = _make_insider_rows("META", buys=[], sells=[10_000_000])
        with patch("signals.openinsider_signals.get_news", return_value=rows):
            df = load_openinsider_data()

        score = score_insider_signal("META", df)
        assert score < 0, f"Expected negative score for net selling, got {score}"

    def test_score_zero_for_unknown_ticker(self):
        """Ticker not in df → score = 0.0."""
        from signals.openinsider_signals import score_insider_signal

        df = pd.DataFrame({"ticker": ["AAPL"], "value": [100.0], "trade_type": ["P - Purchase"],
                           "title": ["CEO"], "qty": [1000.0], "price": [100.0],
                           "insider_name": ["CEO"], "trade_date": ["2026-01-05"],
                           "filing_date": ["2026-01-07"], "relevance_score": [0.8],
                           "timestamp": ["2026-01-10"]})
        score = score_insider_signal("NVDA", df)
        assert score == 0.0

    def test_score_zero_for_empty_df(self):
        """Empty DataFrame → score = 0.0."""
        from signals.openinsider_signals import score_insider_signal, load_openinsider_data

        with patch("signals.openinsider_signals.get_news", return_value=[]):
            df = load_openinsider_data()

        score = score_insider_signal("AAPL", df)
        assert score == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Task 19 — Finviz signals
# ═══════════════════════════════════════════════════════════════════════════════


class TestLoadFinvizData:
    def test_load_finviz_data_returns_dataframe(self):
        """load_finviz_data always returns a DataFrame."""
        from signals.finviz_signals import load_finviz_data

        rows = _make_finviz_rows("MSFT")
        with patch("signals.finviz_signals.get_news", return_value=rows):
            df = load_finviz_data()

        assert isinstance(df, pd.DataFrame)
        required_cols = {"ticker", "perf_week", "perf_month", "short_float", "inst_own"}
        assert required_cols.issubset(df.columns)

    def test_load_returns_empty_df_on_db_error(self):
        """DB error → empty DataFrame, no exception."""
        from signals.finviz_signals import load_finviz_data

        with patch("signals.finviz_signals.get_news", side_effect=RuntimeError("db down")):
            df = load_finviz_data()

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    def test_load_deduplicates_to_most_recent(self):
        """Multiple snapshots for same ticker → most recent row kept."""
        from signals.finviz_signals import load_finviz_data

        rows = _make_finviz_rows("GOOGL")
        rows2 = _make_finviz_rows("GOOGL")
        rows2[0]["timestamp"] = "2026-01-15 13:00:00"  # newer
        rows2[0]["summary"] = json.dumps({"pe": "25.0", "perf_week": "3.0%",
                                          "perf_month": "6.0%", "perf_quarter": "15.0%",
                                          "rel_volume": "1.5", "short_float": "2.0%",
                                          "inst_own": "75.0%", "market_cap": "600B"})

        with patch("signals.finviz_signals.get_news", return_value=rows + rows2):
            df = load_finviz_data()

        assert len(df[df["ticker"] == "GOOGL"]) == 1, "Should deduplicate to 1 row per ticker"


class TestScoreFinvizSignal:
    def test_score_finviz_signal_in_range(self):
        """score_finviz_signal always returns a value in [-1, 1]."""
        from signals.finviz_signals import score_finviz_signal, load_finviz_data

        rows = _make_finviz_rows("AMD", perf_week="4.0%", short_float="3.5%")
        with patch("signals.finviz_signals.get_news", return_value=rows):
            df = load_finviz_data()

        score = score_finviz_signal("AMD", df)
        assert -1.0 <= score <= 1.0, f"Score out of range: {score}"

    def test_score_zero_for_missing_ticker(self):
        """Ticker not in df → 0.0."""
        from signals.finviz_signals import score_finviz_signal

        df = pd.DataFrame({"ticker": ["AAPL"], "perf_week": [0.02], "perf_month": [0.05],
                           "short_float": [0.01], "inst_own": [0.70], "rel_volume": [1.1],
                           "timestamp": ["2026-01-10"]})
        assert score_finviz_signal("NVDA", df) == 0.0

    def test_score_positive_for_strong_momentum_low_short(self):
        """High perf_week + low short float → positive score."""
        from signals.finviz_signals import score_finviz_signal, load_finviz_data

        rows = _make_finviz_rows("NVDA", perf_week="8.0%", short_float="0.5%")
        with patch("signals.finviz_signals.get_news", return_value=rows):
            df = load_finviz_data()

        score = score_finviz_signal("NVDA", df)
        assert score > 0, f"Expected positive score for strong momentum/low short, got {score}"


# ═══════════════════════════════════════════════════════════════════════════════
# Task 19 — Alt-data integration (feature flag)
# ═══════════════════════════════════════════════════════════════════════════════


class TestAltDataSignalIntegration:
    def _config_disabled(self):
        return {"alt_data": {"enabled": False}}

    def _config_enabled(self):
        return {"alt_data": {"enabled": True}}

    def test_signal_integration_respects_feature_flag_disabled(self):
        """alt_data.enabled=false → get_alt_data_score returns None."""
        from signals.alt_data_signals import get_alt_data_score

        with patch("signals.alt_data_signals.is_alt_data_enabled", return_value=False):
            score = get_alt_data_score("AAPL", market_id="sp500")

        assert score is None, f"Expected None when feature disabled, got {score}"

    def test_signal_integration_includes_when_enabled(self):
        """alt_data.enabled=true + data available → score is in [-1, 1]."""
        from signals.alt_data_signals import get_alt_data_score

        insider_rows = _make_insider_rows("AAPL", buys=[5_000_000], sells=[])
        finviz_rows = _make_finviz_rows("AAPL")

        with (
            patch("signals.alt_data_signals.is_alt_data_enabled", return_value=True),
            patch("signals.openinsider_signals.get_news", return_value=insider_rows),
            patch("signals.finviz_signals.get_news", return_value=finviz_rows),
        ):
            # Reset LRU caches so patched loaders are used
            from signals import alt_data_signals as ads
            ads._cached_openinsider_df.cache_clear()
            ads._cached_finviz_df.cache_clear()
            score = get_alt_data_score("AAPL", market_id="sp500")

        assert score is not None, "Expected a score when feature is enabled"
        assert -1.0 <= score <= 1.0, f"Score out of range: {score}"

    def test_get_alt_data_score_returns_none_when_no_data(self):
        """Feature enabled but no data for ticker → returns None."""
        from signals.alt_data_signals import get_alt_data_score
        from signals import alt_data_signals as ads

        with (
            patch("signals.alt_data_signals.is_alt_data_enabled", return_value=True),
            patch("signals.openinsider_signals.get_news", return_value=[]),
            patch("signals.finviz_signals.get_news", return_value=[]),
        ):
            ads._cached_openinsider_df.cache_clear()
            ads._cached_finviz_df.cache_clear()
            score = get_alt_data_score("FAKE_TICKER", market_id="sp500")

        assert score is None, "Expected None for ticker with no data"

    def test_inject_alt_data_into_signals_disabled(self):
        """When disabled, inject_alt_data_into_signals returns signals unchanged."""
        from signals.alt_data_signals import inject_alt_data_into_signals

        signals = [{"ticker": "AAPL", "score": 0.5}]
        with patch("signals.alt_data_signals.is_alt_data_enabled", return_value=False):
            result = inject_alt_data_into_signals(signals, market_id="sp500")

        # No alt_data_score key added
        assert "alt_data_score" not in result[0]
        assert result[0]["score"] == 0.5  # original preserved

    def test_inject_alt_data_into_signals_enabled_adds_key(self):
        """When enabled + data available, inject adds alt_data_score to each signal."""
        from signals.alt_data_signals import inject_alt_data_into_signals
        from signals import alt_data_signals as ads

        insider_rows = _make_insider_rows("MSFT", buys=[3_000_000], sells=[])
        finviz_rows = _make_finviz_rows("MSFT")

        signals = [{"ticker": "MSFT", "score": 0.7}]

        with (
            patch("signals.alt_data_signals.is_alt_data_enabled", return_value=True),
            patch("signals.openinsider_signals.get_news", return_value=insider_rows),
            patch("signals.finviz_signals.get_news", return_value=finviz_rows),
        ):
            ads._cached_openinsider_df.cache_clear()
            ads._cached_finviz_df.cache_clear()
            result = inject_alt_data_into_signals(signals, market_id="sp500")

        assert "alt_data_score" in result[0], (
            f"Expected alt_data_score in signal, got: {result[0]}"
        )
        assert -1.0 <= result[0]["alt_data_score"] <= 1.0

    def test_is_alt_data_enabled_reads_config(self):
        """is_alt_data_enabled reads config/active/{market_id}.json correctly."""
        from signals.alt_data_signals import is_alt_data_enabled

        with patch("signals.alt_data_signals.get_active_config",
                   return_value={"alt_data": {"enabled": True}}):
            assert is_alt_data_enabled("sp500") is True

        with patch("signals.alt_data_signals.get_active_config",
                   return_value={"alt_data": {"enabled": False}}):
            assert is_alt_data_enabled("sp500") is False

    def test_is_alt_data_enabled_defaults_false_on_config_error(self):
        """Config load error → defaults to False (safe fallback)."""
        from signals.alt_data_signals import is_alt_data_enabled

        with patch("signals.alt_data_signals.get_active_config",
                   side_effect=RuntimeError("config missing")):
            assert is_alt_data_enabled("sp500") is False
