"""
Tests for universe/builder.py — build_from_definition() and build_multi_universe().

Uses the real production SQLite database (already populated with ETF OHLCV data).
"""

import pytest
import pandas as pd

from universe.builder import build_from_definition, build_multi_universe
from universe.definitions import list_universes, get_universe_tickers

# ── helpers ────────────────────────────────────────────────────────────────

STATIC_UNIVERSES = [u for u in list_universes() if u != "sp500"]
ALL_UNIVERSES = list_universes()

# ── build_from_definition ───────────────────────────────────────────────────

class TestBuildFromDefinition:

    def test_static_universe_returns_dict_of_dataframes(self):
        """Basic smoke test: returns a non-empty dict of DataFrames."""
        data = build_from_definition("sector_etfs", start_date="2022-01-01")
        assert isinstance(data, dict)
        assert len(data) > 0
        for ticker, df in data.items():
            assert isinstance(ticker, str)
            assert isinstance(df, pd.DataFrame)
            assert not df.empty

    def test_column_names_match_expected_format(self):
        """DataFrames must have lowercase OHLCV columns expected by the backtest engine."""
        required_cols = {"open", "high", "low", "close", "volume"}
        data = build_from_definition("gold_etfs", start_date="2022-01-01")
        assert len(data) > 0, "Expected at least one ticker"
        for ticker, df in data.items():
            missing = required_cols - set(df.columns)
            assert not missing, (
                f"{ticker}: missing columns {missing}. Got: {list(df.columns)}"
            )

    def test_index_is_datetime(self):
        """DataFrame index must be a DatetimeIndex named 'date'."""
        data = build_from_definition("treasury_etfs", start_date="2022-01-01")
        assert len(data) > 0
        for ticker, df in data.items():
            assert isinstance(df.index, pd.DatetimeIndex), (
                f"{ticker}: index is {type(df.index)}, expected DatetimeIndex"
            )
            assert df.index.name == "date", (
                f"{ticker}: index name is {df.index.name!r}, expected 'date'"
            )

    def test_min_history_days_filtering(self):
        """Tickers with fewer rows than min_history_days must be excluded."""
        # Request a very high min so we only keep tickers with long history
        data_high = build_from_definition(
            "sector_etfs", start_date="2022-01-01", min_history_days=500
        )
        data_low = build_from_definition(
            "sector_etfs", start_date="2022-01-01", min_history_days=1
        )
        # With low threshold we get at least as many tickers
        assert len(data_low) >= len(data_high), (
            "Low min_history_days should yield >= tickers compared to high threshold"
        )
        # With a very high threshold (more days than available), might drop some
        data_extreme = build_from_definition(
            "sector_etfs", start_date="2022-01-01", min_history_days=99999
        )
        assert len(data_extreme) == 0, (
            "No ticker should survive a min_history_days=99999 filter"
        )

    def test_all_returned_tickers_meet_min_history(self):
        """Every returned DataFrame must have >= min_history_days rows."""
        min_h = 100
        data = build_from_definition(
            "sector_etfs", start_date="2020-01-01", min_history_days=min_h
        )
        for ticker, df in data.items():
            assert len(df) >= min_h, (
                f"{ticker}: only {len(df)} rows, expected >= {min_h}"
            )

    def test_start_date_filtering(self):
        """Data returned must not pre-date start_date."""
        start = "2023-06-01"
        data = build_from_definition("gold_etfs", start_date=start)
        for ticker, df in data.items():
            min_date = df.index.min()
            assert str(min_date.date()) >= start, (
                f"{ticker}: min date {min_date.date()} < start_date {start}"
            )

    def test_end_date_filtering(self):
        """Data returned must not go beyond end_date."""
        end = "2023-12-31"
        data = build_from_definition(
            "treasury_etfs", start_date="2022-01-01", end_date=end
        )
        for ticker, df in data.items():
            max_date = df.index.max()
            assert str(max_date.date()) <= end, (
                f"{ticker}: max date {max_date.date()} > end_date {end}"
            )

    def test_sp500_delegates_to_db(self):
        """SP500 universe should return DataFrames from SQLite."""
        data = build_from_definition("sp500", start_date="2023-01-01")
        # SP500 has a large number of tickers — just verify we get a meaningful result
        assert isinstance(data, dict)
        if len(data) > 0:  # may be empty if DB not populated with sp500 data
            sample_df = next(iter(data.values()))
            required_cols = {"open", "high", "low", "close", "volume"}
            missing = required_cols - set(sample_df.columns)
            assert not missing, f"SP500 DataFrame missing columns: {missing}"

    @pytest.mark.parametrize("universe", STATIC_UNIVERSES)
    def test_all_static_universes_return_data(self, universe):
        """Each static universe should return at least 1 ticker."""
        data = build_from_definition(universe, start_date="2022-01-01")
        assert isinstance(data, dict)
        assert len(data) >= 1, f"{universe}: expected at least 1 ticker, got 0"

    def test_tickers_match_definitions_for_static_universes(self):
        """Returned tickers must be a subset of the universe definition."""
        universe = "commodity_etfs"
        expected_tickers = set(get_universe_tickers(universe))
        data = build_from_definition(universe, start_date="2022-01-01", min_history_days=1)
        for ticker in data:
            assert ticker in expected_tickers, (
                f"{ticker} not in {universe} definition. Expected: {expected_tickers}"
            )

    def test_unknown_universe_raises_value_error(self):
        """Requesting an unknown universe name must raise ValueError."""
        with pytest.raises(ValueError, match="Unknown universe"):
            build_from_definition("this_does_not_exist")

    def test_default_start_date_applied(self):
        """When no start_date given, should still return data (7-year default)."""
        data = build_from_definition("gold_etfs")
        assert isinstance(data, dict)
        assert len(data) > 0

    def test_no_empty_dataframes_in_result(self):
        """All DataFrames in the result must be non-empty."""
        data = build_from_definition("defensive_etfs", start_date="2022-01-01")
        for ticker, df in data.items():
            assert not df.empty, f"{ticker}: returned an empty DataFrame"


# ── build_multi_universe ────────────────────────────────────────────────────

class TestBuildMultiUniverse:

    def test_returns_nested_dict(self):
        """Should return dict[str, dict[str, DataFrame]]."""
        multi = build_multi_universe(
            ["treasury_etfs", "gold_etfs"], start_date="2022-01-01"
        )
        assert isinstance(multi, dict)
        assert set(multi.keys()) == {"treasury_etfs", "gold_etfs"}
        for uni_name, tickers in multi.items():
            assert isinstance(tickers, dict), (
                f"{uni_name}: expected dict, got {type(tickers)}"
            )
            for ticker, df in tickers.items():
                assert isinstance(df, pd.DataFrame)

    def test_each_universe_has_data(self):
        """Each universe in the result should contain at least one ticker."""
        multi = build_multi_universe(
            ["sector_etfs", "commodity_etfs"], start_date="2022-01-01"
        )
        for uni_name, tickers in multi.items():
            assert len(tickers) >= 1, f"{uni_name}: expected >=1 ticker, got 0"

    def test_kwargs_forwarded(self):
        """end_date and min_history_days kwargs must be forwarded."""
        end = "2023-06-30"
        multi = build_multi_universe(
            ["gold_etfs"], start_date="2022-01-01", end_date=end, min_history_days=50
        )
        for ticker, df in multi["gold_etfs"].items():
            assert str(df.index.max().date()) <= end

    def test_unknown_universe_raises_value_error(self):
        """Any unknown universe in the list must raise ValueError."""
        with pytest.raises(ValueError, match="Unknown universe"):
            build_multi_universe(["gold_etfs", "unicorn_universe"])

    def test_empty_list_returns_empty_dict(self):
        """Passing an empty list of universes returns an empty dict."""
        multi = build_multi_universe([])
        assert multi == {}

    def test_single_universe_matches_build_from_definition(self):
        """build_multi_universe([u]) must match build_from_definition(u)."""
        universe = "treasury_etfs"
        start = "2022-01-01"
        single = build_from_definition(universe, start_date=start)
        multi = build_multi_universe([universe], start_date=start)
        assert set(single.keys()) == set(multi[universe].keys()), (
            "Ticker sets differ between build_from_definition and build_multi_universe"
        )
