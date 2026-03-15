"""Tests for Point-in-Time S&P 500 membership reconstruction."""
import sys
from pathlib import Path
from datetime import date

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from data.sp500_history import (
    load_changes,
    get_members_at_date,
    get_change_count_between,
    _parse_date,
    CHANGES_FILE,
)


class TestLoadChanges:
    """Test CSV loading and parsing."""

    def test_returns_list(self):
        changes = load_changes()
        assert isinstance(changes, list)

    def test_minimum_entry_count(self):
        """CSV must have ≥150 entries."""
        changes = load_changes()
        assert len(changes) >= 150, f"Only {len(changes)} entries, need ≥150"

    def test_entries_have_required_keys(self):
        changes = load_changes()
        required = {"date", "ticker", "action", "replaced", "notes"}
        for c in changes[:10]:
            assert required.issubset(c.keys()), f"Missing keys: {required - c.keys()}"

    def test_dates_are_date_objects(self):
        changes = load_changes()
        for c in changes[:10]:
            assert isinstance(c["date"], date), f"Expected date, got {type(c['date'])}"

    def test_sorted_descending(self):
        changes = load_changes()
        dates = [c["date"] for c in changes]
        assert dates == sorted(dates, reverse=True), "Changes not sorted descending"

    def test_actions_are_valid(self):
        changes = load_changes()
        valid = {"ADD", "REMOVE"}
        for c in changes:
            assert c["action"].upper() in valid, f"Invalid action: {c['action']}"

    def test_covers_2018_to_2025(self):
        changes = load_changes()
        years = {c["date"].year for c in changes}
        for y in range(2018, 2026):
            assert y in years, f"No changes found for year {y}"


class TestGetMembersAtDate:
    """Test PIT membership reconstruction."""

    # Use a known small current set for deterministic testing
    KNOWN_CURRENT = {"AAPL", "MSFT", "TSLA", "UBER", "PLTR", "HOOD", "NTNX", "DELL"}

    def test_returns_set(self):
        result = get_members_at_date("2024-01-01", current_members=self.KNOWN_CURRENT)
        assert isinstance(result, set)

    def test_tsla_not_in_2020_pre_add(self):
        """TSLA added 2020-12-21 — should NOT be in index before that."""
        members = get_members_at_date("2020-12-20", current_members=self.KNOWN_CURRENT)
        assert "TSLA" not in members

    def test_tsla_in_2021(self):
        """TSLA added 2020-12-21 — should be in index after that."""
        members = get_members_at_date("2021-01-01", current_members=self.KNOWN_CURRENT)
        assert "TSLA" in members

    def test_uber_not_in_2023(self):
        """UBER added 2024-01-22 — should NOT be in index before."""
        members = get_members_at_date("2023-12-01", current_members=self.KNOWN_CURRENT)
        assert "UBER" not in members

    def test_uber_in_2024(self):
        """UBER added 2024-01-22 — should be in after."""
        members = get_members_at_date("2024-02-01", current_members=self.KNOWN_CURRENT)
        assert "UBER" in members

    def test_pltr_not_in_early_2024(self):
        """PLTR added 2024-12-23."""
        members = get_members_at_date("2024-06-01", current_members=self.KNOWN_CURRENT)
        assert "PLTR" not in members

    def test_pltr_in_2025(self):
        """PLTR added 2024-12-23."""
        members = get_members_at_date("2025-01-01", current_members=self.KNOWN_CURRENT)
        assert "PLTR" in members

    def test_removed_ticker_restored(self):
        """CELH removed 2025-03-14 — should be present before removal."""
        current = {"AAPL", "HOOD"}  # CELH not in current
        members = get_members_at_date("2025-03-13", current_members=current)
        assert "CELH" in members  # Should be added back

    def test_different_sets_for_different_dates(self):
        """2019 and 2025 should yield different membership."""
        m2019 = get_members_at_date("2019-01-01", current_members=self.KNOWN_CURRENT)
        m2025 = get_members_at_date("2025-03-15", current_members=self.KNOWN_CURRENT)
        assert m2019 != m2025

    def test_accepts_date_object(self):
        result = get_members_at_date(date(2023, 6, 1), current_members=self.KNOWN_CURRENT)
        assert isinstance(result, set)

    def test_empty_current_returns_empty(self):
        result = get_members_at_date("2024-01-01", current_members=set())
        assert result == set()


class TestGetChangeCountBetween:
    """Test change counting."""

    def test_returns_int(self):
        count = get_change_count_between("2020-01-01", "2020-12-31")
        assert isinstance(count, int)

    def test_2020_has_changes(self):
        count = get_change_count_between("2020-01-01", "2020-12-31")
        assert count > 0, "Expected changes in 2020"

    def test_narrow_range_fewer_changes(self):
        narrow = get_change_count_between("2020-06-01", "2020-06-30")
        wide = get_change_count_between("2020-01-01", "2020-12-31")
        assert narrow <= wide

    def test_future_range_zero(self):
        count = get_change_count_between("2030-01-01", "2030-12-31")
        assert count == 0


class TestParseDate:
    """Test date parsing utility."""

    def test_string(self):
        assert _parse_date("2024-06-15") == date(2024, 6, 15)

    def test_date_object(self):
        d = date(2024, 6, 15)
        assert _parse_date(d) == d

    def test_datetime_object(self):
        from datetime import datetime
        dt = datetime(2024, 6, 15, 10, 30)
        assert _parse_date(dt) == date(2024, 6, 15)

    def test_invalid_raises(self):
        with pytest.raises(TypeError):
            _parse_date(12345)


class TestCSVFile:
    """Test CSV file integrity."""

    def test_file_exists(self):
        assert CHANGES_FILE.exists()

    def test_adds_and_removes_balanced(self):
        """Most ADDs should have a corresponding REMOVE (index stays ~500)."""
        changes = load_changes()
        adds = sum(1 for c in changes if c["action"].upper() == "ADD")
        removes = sum(1 for c in changes if c["action"].upper() == "REMOVE")
        # Allow small imbalance (some entries may be unpaired)
        assert abs(adds - removes) <= 5, f"Imbalanced: {adds} adds, {removes} removes"
