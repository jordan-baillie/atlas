"""Tests for signals/sector_rotation.py — defensive rotation detection.

Run with:  python -m pytest tests/test_sector_rotation_signal.py -v --timeout=30
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from signals.sector_rotation import (  # noqa: E402
    DEFENSIVE_ETFS,
    SPDR_SECTORS,
    detect_defensive_rotation,
    get_sector_rotation_signal,
    rank_sectors_by_momentum,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rankings(
    order: list[str],
    roc_values: dict[str, float] | None = None,
) -> list[dict]:
    """Build a synthetic rankings list in the given ETF order."""
    roc_values = roc_values or {}
    result = []
    for i, etf in enumerate(order):
        result.append(
            {
                "etf": etf,
                "sector": SPDR_SECTORS[etf],
                "roc_63d": roc_values.get(etf, float(len(order) - i)),
                "rank": i + 1,
            }
        )
    return result


def _synthetic_prices(
    tickers: list[str],
    n_rows: int,
    roc_pct: float = 10.0,
) -> dict[str, list[tuple[str, float]]]:
    """Return fake price series with a predictable ROC."""
    base_date = date(2025, 1, 2)
    prices = {}
    for ticker in tickers:
        rows = []
        start_price = 100.0
        end_price = start_price * (1 + roc_pct / 100)
        for j in range(n_rows):
            d = base_date + timedelta(days=j)
            # Linear interpolation from start to end over n_rows
            price = start_price + (end_price - start_price) * j / (n_rows - 1)
            rows.append((d.isoformat(), round(price, 4)))
        prices[ticker] = rows
    return prices


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_spdr_sectors_has_11_etfs(self):
        assert len(SPDR_SECTORS) == 11

    def test_all_etf_tickers_present(self):
        expected = {"XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"}
        assert set(SPDR_SECTORS.keys()) == expected

    def test_defensive_etfs_subset_of_spdr(self):
        assert DEFENSIVE_ETFS.issubset(set(SPDR_SECTORS.keys()))

    def test_defensive_etfs_are_xlu_xlp(self):
        assert DEFENSIVE_ETFS == {"XLU", "XLP"}


# ---------------------------------------------------------------------------
# detect_defensive_rotation
# ---------------------------------------------------------------------------

class TestDetectDefensiveRotation:
    def test_empty_rankings_returns_no_rotation(self):
        result = detect_defensive_rotation([])
        assert result["defensive_rotation"] is False
        assert result["defensive_in_top3"] == []
        assert result["severity"] == "none"

    def test_no_defensives_in_top3(self):
        # XLK #1, XLE #2, XLB #3 — all cyclical
        rankings = _make_rankings(["XLK", "XLE", "XLB", "XLU", "XLP", "XLF", "XLI", "XLY", "XLC", "XLRE", "XLV"])
        result = detect_defensive_rotation(rankings)
        assert result["defensive_rotation"] is False
        assert result["defensive_in_top3"] == []
        assert result["severity"] == "none"

    def test_one_defensive_in_top3_moderate(self):
        # XLU #2
        rankings = _make_rankings(["XLE", "XLU", "XLB", "XLP", "XLF", "XLI", "XLY", "XLC", "XLRE", "XLV", "XLK"])
        result = detect_defensive_rotation(rankings)
        assert result["defensive_rotation"] is True
        assert result["defensive_in_top3"] == ["XLU"]
        assert result["severity"] == "moderate"

    def test_both_defensives_in_top3_high(self):
        # XLU #1, XLP #2
        rankings = _make_rankings(["XLU", "XLP", "XLB", "XLF", "XLI", "XLY", "XLC", "XLRE", "XLV", "XLK", "XLE"])
        result = detect_defensive_rotation(rankings)
        assert result["defensive_rotation"] is True
        assert set(result["defensive_in_top3"]) == {"XLU", "XLP"}
        assert result["severity"] == "high"

    def test_defensive_at_rank3_detected(self):
        # XLP exactly at rank 3
        rankings = _make_rankings(["XLE", "XLB", "XLP", "XLU", "XLF", "XLI", "XLY", "XLC", "XLRE", "XLV", "XLK"])
        result = detect_defensive_rotation(rankings)
        assert result["defensive_rotation"] is True
        assert "XLP" in result["defensive_in_top3"]

    def test_top3_keys_present(self):
        rankings = _make_rankings(["XLE", "XLU", "XLB", "XLP", "XLF", "XLI", "XLY", "XLC", "XLRE", "XLV", "XLK"])
        result = detect_defensive_rotation(rankings)
        assert len(result["top3"]) == 3
        for entry in result["top3"]:
            assert "etf" in entry
            assert "roc_63d" in entry

    def test_bottom3_keys_present(self):
        rankings = _make_rankings(["XLE", "XLU", "XLB", "XLP", "XLF", "XLI", "XLY", "XLC", "XLRE", "XLV", "XLK"])
        result = detect_defensive_rotation(rankings)
        assert len(result["bottom3"]) == 3
        for entry in result["bottom3"]:
            assert "etf" in entry
            assert "roc_63d" in entry

    def test_bottom3_are_lowest_ranked(self):
        order = ["XLE", "XLU", "XLB", "XLP", "XLF", "XLI", "XLY", "XLC", "XLRE", "XLV", "XLK"]
        rankings = _make_rankings(order)
        result = detect_defensive_rotation(rankings)
        bottom_etfs = [e["etf"] for e in result["bottom3"]]
        assert bottom_etfs == order[-3:]

    def test_severity_none_when_no_rotation(self):
        rankings = _make_rankings(["XLK", "XLE", "XLB", "XLU", "XLP", "XLF", "XLI", "XLY", "XLC", "XLRE", "XLV"])
        result = detect_defensive_rotation(rankings)
        assert result["severity"] == "none"
        assert result["defensive_rotation"] is False


# ---------------------------------------------------------------------------
# rank_sectors_by_momentum — DB path
# ---------------------------------------------------------------------------

class TestRankSectorsByMomentum:
    """Test ROC computation using mocked DB data."""

    def _mock_prices(self, roc_values: dict[str, float]) -> dict[str, list[tuple[str, float]]]:
        """Build synthetic price series that produce the desired ROC values."""
        n_rows = 65  # roc_period=63 → need 64+ rows
        prices = {}
        base_date = date(2025, 6, 1)
        for etf in SPDR_SECTORS:
            target_roc = roc_values.get(etf, 5.0)
            start = 100.0
            end = start * (1 + target_roc / 100)
            rows = []
            for j in range(n_rows):
                d = base_date + timedelta(days=j)
                price = start + (end - start) * j / (n_rows - 1)
                rows.append((d.isoformat(), round(price, 4)))
            prices[etf] = rows
        return prices

    @patch("signals.sector_rotation._load_prices_from_db")
    def test_returns_11_entries(self, mock_db):
        mock_db.return_value = self._mock_prices({})
        result = rank_sectors_by_momentum()
        assert len(result) == 11

    @patch("signals.sector_rotation._load_prices_from_db")
    def test_sorted_descending_by_roc(self, mock_db):
        mock_db.return_value = self._mock_prices({})
        result = rank_sectors_by_momentum()
        rocs = [r["roc_63d"] for r in result]
        assert rocs == sorted(rocs, reverse=True)

    @patch("signals.sector_rotation._load_prices_from_db")
    def test_rank_field_starts_at_1(self, mock_db):
        mock_db.return_value = self._mock_prices({})
        result = rank_sectors_by_momentum()
        assert result[0]["rank"] == 1
        assert result[-1]["rank"] == 11

    @patch("signals.sector_rotation._load_prices_from_db")
    def test_rank_field_sequential(self, mock_db):
        mock_db.return_value = self._mock_prices({})
        result = rank_sectors_by_momentum()
        ranks = [r["rank"] for r in result]
        assert ranks == list(range(1, 12))

    @patch("signals.sector_rotation._load_prices_from_db")
    def test_highest_roc_ranks_first(self, mock_db):
        roc_map = {etf: float(i) for i, etf in enumerate(SPDR_SECTORS)}
        roc_map["XLU"] = 999.0  # force XLU to top
        mock_db.return_value = self._mock_prices(roc_map)
        result = rank_sectors_by_momentum()
        assert result[0]["etf"] == "XLU"
        assert result[0]["rank"] == 1

    @patch("signals.sector_rotation._load_prices_from_db")
    def test_required_keys_in_each_entry(self, mock_db):
        mock_db.return_value = self._mock_prices({})
        result = rank_sectors_by_momentum()
        for entry in result:
            assert "etf" in entry
            assert "sector" in entry
            assert "roc_63d" in entry
            assert "rank" in entry

    @patch("signals.sector_rotation._load_prices_from_db")
    def test_sector_name_matches_constant(self, mock_db):
        mock_db.return_value = self._mock_prices({})
        result = rank_sectors_by_momentum()
        for entry in result:
            assert entry["sector"] == SPDR_SECTORS[entry["etf"]]

    @patch("signals.sector_rotation._load_prices_from_db")
    @patch("signals.sector_rotation._fetch_from_yfinance")
    def test_falls_back_to_yfinance_when_db_insufficient(self, mock_yf, mock_db):
        """If DB has < roc_period+1 rows for any ETF, yfinance fallback is called."""
        # DB returns only 10 rows for all tickers (far fewer than 64 needed)
        short_prices = {
            etf: [(f"2025-01-{i+1:02d}", 100.0 + i) for i in range(10)]
            for etf in SPDR_SECTORS
        }
        mock_db.return_value = short_prices
        mock_yf.return_value = self._mock_prices({})  # full data from yf
        result = rank_sectors_by_momentum()
        mock_yf.assert_called_once()
        assert len(result) == 11

    @patch("signals.sector_rotation._load_prices_from_db")
    def test_roc_63d_is_float(self, mock_db):
        mock_db.return_value = self._mock_prices({})
        result = rank_sectors_by_momentum()
        for entry in result:
            assert isinstance(entry["roc_63d"], float)

    @patch("signals.sector_rotation._load_prices_from_db")
    def test_positive_roc_when_price_increased(self, mock_db):
        roc_map = {etf: 20.0 for etf in SPDR_SECTORS}
        mock_db.return_value = self._mock_prices(roc_map)
        result = rank_sectors_by_momentum()
        for entry in result:
            assert entry["roc_63d"] > 0, f"{entry['etf']} should have positive ROC"

    @patch("signals.sector_rotation._load_prices_from_db")
    def test_negative_roc_when_price_decreased(self, mock_db):
        roc_map = {etf: -15.0 for etf in SPDR_SECTORS}
        mock_db.return_value = self._mock_prices(roc_map)
        result = rank_sectors_by_momentum()
        for entry in result:
            assert entry["roc_63d"] < 0, f"{entry['etf']} should have negative ROC"


# ---------------------------------------------------------------------------
# get_sector_rotation_signal
# ---------------------------------------------------------------------------

class TestGetSectorRotationSignal:
    """Test the combined signal output shape and semantics."""

    ALL_ETFS_ORDER = [
        "XLK", "XLE", "XLB", "XLF", "XLI",  # cyclical top 5
        "XLU",                                 # defensive at rank 6
        "XLP", "XLC", "XLRE", "XLV", "XLY",   # rest
    ]

    @patch("signals.sector_rotation.rank_sectors_by_momentum")
    def test_required_keys_present(self, mock_rank):
        mock_rank.return_value = _make_rankings(self.ALL_ETFS_ORDER)
        result = get_sector_rotation_signal()
        for key in (
            "as_of", "rankings", "defensive_rotation", "defensive_in_top3",
            "severity", "risk_off_signal", "top3_sectors", "bottom3_sectors",
        ):
            assert key in result, f"Missing key: {key}"

    @patch("signals.sector_rotation.rank_sectors_by_momentum")
    def test_as_of_is_today_by_default(self, mock_rank):
        mock_rank.return_value = _make_rankings(self.ALL_ETFS_ORDER)
        result = get_sector_rotation_signal()
        assert result["as_of"] == date.today().isoformat()

    @patch("signals.sector_rotation.rank_sectors_by_momentum")
    def test_as_of_respects_end_date_param(self, mock_rank):
        mock_rank.return_value = _make_rankings(self.ALL_ETFS_ORDER)
        custom_date = date(2025, 11, 15)
        result = get_sector_rotation_signal(end_date=custom_date)
        assert result["as_of"] == "2025-11-15"

    @patch("signals.sector_rotation.rank_sectors_by_momentum")
    def test_risk_off_signal_equals_defensive_rotation(self, mock_rank):
        mock_rank.return_value = _make_rankings(self.ALL_ETFS_ORDER)
        result = get_sector_rotation_signal()
        assert result["risk_off_signal"] == result["defensive_rotation"]

    @patch("signals.sector_rotation.rank_sectors_by_momentum")
    def test_no_rotation_when_all_cyclical_top3(self, mock_rank):
        order = ["XLK", "XLE", "XLB", "XLF", "XLI", "XLU", "XLP", "XLC", "XLRE", "XLV", "XLY"]
        mock_rank.return_value = _make_rankings(order)
        result = get_sector_rotation_signal()
        assert result["defensive_rotation"] is False
        assert result["risk_off_signal"] is False
        assert result["severity"] == "none"

    @patch("signals.sector_rotation.rank_sectors_by_momentum")
    def test_rotation_when_xlu_top3(self, mock_rank):
        order = ["XLE", "XLU", "XLB", "XLK", "XLF", "XLP", "XLI", "XLC", "XLRE", "XLV", "XLY"]
        mock_rank.return_value = _make_rankings(order)
        result = get_sector_rotation_signal()
        assert result["defensive_rotation"] is True
        assert "XLU" in result["defensive_in_top3"]

    @patch("signals.sector_rotation.rank_sectors_by_momentum")
    def test_high_severity_when_both_top3(self, mock_rank):
        order = ["XLU", "XLP", "XLB", "XLK", "XLF", "XLE", "XLI", "XLC", "XLRE", "XLV", "XLY"]
        mock_rank.return_value = _make_rankings(order)
        result = get_sector_rotation_signal()
        assert result["severity"] == "high"

    @patch("signals.sector_rotation.rank_sectors_by_momentum")
    def test_top3_sectors_is_list_of_3(self, mock_rank):
        mock_rank.return_value = _make_rankings(self.ALL_ETFS_ORDER)
        result = get_sector_rotation_signal()
        assert isinstance(result["top3_sectors"], list)
        assert len(result["top3_sectors"]) == 3

    @patch("signals.sector_rotation.rank_sectors_by_momentum")
    def test_bottom3_sectors_is_list_of_3(self, mock_rank):
        mock_rank.return_value = _make_rankings(self.ALL_ETFS_ORDER)
        result = get_sector_rotation_signal()
        assert isinstance(result["bottom3_sectors"], list)
        assert len(result["bottom3_sectors"]) == 3

    @patch("signals.sector_rotation.rank_sectors_by_momentum")
    def test_top3_sectors_match_top_etfs(self, mock_rank):
        order = ["XLK", "XLE", "XLB", "XLF", "XLI", "XLU", "XLP", "XLC", "XLRE", "XLV", "XLY"]
        mock_rank.return_value = _make_rankings(order)
        result = get_sector_rotation_signal()
        assert result["top3_sectors"] == ["Technology", "Energy", "Materials"]

    @patch("signals.sector_rotation.rank_sectors_by_momentum")
    def test_bottom3_sectors_match_bottom_etfs(self, mock_rank):
        order = ["XLK", "XLE", "XLB", "XLF", "XLI", "XLU", "XLP", "XLC", "XLRE", "XLV", "XLY"]
        mock_rank.return_value = _make_rankings(order)
        result = get_sector_rotation_signal()
        assert result["bottom3_sectors"] == ["Real Estate", "Health Care", "Consumer Discretionary"]

    @patch("signals.sector_rotation.rank_sectors_by_momentum")
    def test_rankings_list_passed_through(self, mock_rank):
        rankings = _make_rankings(self.ALL_ETFS_ORDER)
        mock_rank.return_value = rankings
        result = get_sector_rotation_signal()
        assert result["rankings"] is rankings  # same object


# ---------------------------------------------------------------------------
# Integration — uses the real DB (requires ETF data present)
# ---------------------------------------------------------------------------

class TestIntegration:
    """Light integration test against the actual SQLite DB."""

    def test_real_signal_returns_correct_shape(self):
        """Run against the real DB and check the output is well-formed."""
        result = get_sector_rotation_signal()
        # Shape checks
        assert "as_of" in result
        assert isinstance(result["rankings"], list)
        assert isinstance(result["defensive_rotation"], bool)
        assert isinstance(result["risk_off_signal"], bool)
        assert result["severity"] in ("none", "moderate", "high")
        assert len(result["rankings"]) == 11
        assert len(result["top3_sectors"]) == 3
        assert len(result["bottom3_sectors"]) == 3

    def test_real_signal_ranks_are_sequential(self):
        result = get_sector_rotation_signal()
        ranks = [r["rank"] for r in result["rankings"]]
        assert ranks == list(range(1, 12))

    def test_real_signal_sorted_descending(self):
        result = get_sector_rotation_signal()
        rocs = [r["roc_63d"] for r in result["rankings"]]
        assert rocs == sorted(rocs, reverse=True)

    def test_real_signal_end_date_param(self):
        """Passing a fixed historical end_date should still return 11 rankings."""
        past = date(2025, 6, 30)
        result = get_sector_rotation_signal(end_date=past)
        assert result["as_of"] == "2025-06-30"
        assert len(result["rankings"]) == 11
