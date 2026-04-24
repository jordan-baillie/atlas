"""
Regression tests for P1-6 — plan date validation.

Verifies that record_plan() rejects dates that are suspiciously far from
today (>30 days) or have a year mismatch with the current year.
Root cause: test fixture date "2024-03-01" leaked into prod DB via wraps=_save_plan.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, date
from unittest.mock import patch

from db import atlas_db


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Redirect all DB writes to a temp DB for test isolation."""
    db_path = tmp_path / "test_plan_val.db"
    monkeypatch.setattr(atlas_db, "_db_path_override", str(db_path))
    atlas_db.init_db(str(db_path))
    yield


class TestPlanDateValidation:
    """_validate_plan_date rejects bad dates before the INSERT."""

    def test_date_60_days_before_today_raises(self):
        stale_date = (datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%d")
        with pytest.raises(ValueError, match="likely hardcoded test date"):
            atlas_db.record_plan(
                date=stale_date,
                market_id="sp500",
                plan_data={"trade_date": stale_date},
            )

    def test_date_60_days_in_future_raises(self):
        future_date = (datetime.utcnow() + timedelta(days=60)).strftime("%Y-%m-%d")
        with pytest.raises(ValueError, match="likely hardcoded test date"):
            atlas_db.record_plan(
                date=future_date,
                market_id="sp500",
                plan_data={"trade_date": future_date},
            )

    def test_year_mismatch_raises(self):
        """Explicit P1-6 regression — 2024 date while now is 2026."""
        with pytest.raises(ValueError, match="plan year=2024"):
            atlas_db.record_plan(
                date="2024-03-01",
                market_id="sp500",
                plan_data={"trade_date": "2024-03-01"},
            )

    def test_sane_date_today_succeeds(self):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        plan_id = atlas_db.record_plan(
            date=today,
            market_id="sp500",
            plan_data={"trade_date": today, "signals": []},
        )
        assert isinstance(plan_id, int)
        assert plan_id > 0

    def test_sane_date_15_days_ago_succeeds(self):
        recent = (datetime.utcnow() - timedelta(days=15)).strftime("%Y-%m-%d")
        plan_id = atlas_db.record_plan(
            date=recent,
            market_id="sp500",
            plan_data={"trade_date": recent},
        )
        assert isinstance(plan_id, int)

    def test_sane_date_7_days_future_succeeds(self):
        future = (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%d")
        plan_id = atlas_db.record_plan(
            date=future,
            market_id="sp500",
            plan_data={"trade_date": future},
        )
        assert isinstance(plan_id, int)

    def test_empty_date_string_skips_validation(self):
        """Empty date should not raise — some callers may omit it."""
        plan_id = atlas_db.record_plan(
            date="",
            market_id="sp500",
            plan_data={},
        )
        assert isinstance(plan_id, int)

    def test_nonstandard_date_format_skips_validation(self):
        """'2026-04-08-test' style dates skip validation gracefully."""
        # _validate_plan_date returns early on parse failure
        plan_id = atlas_db.record_plan(
            date="2026-04-08-test",
            market_id="sp500",
            plan_data={},
        )
        assert isinstance(plan_id, int)

    def test_exactly_30_days_ago_succeeds(self):
        boundary = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
        plan_id = atlas_db.record_plan(
            date=boundary,
            market_id="sp500",
            plan_data={"trade_date": boundary},
        )
        assert isinstance(plan_id, int)

    def test_31_days_ago_raises(self):
        just_outside = (date.today() - timedelta(days=31)).strftime("%Y-%m-%d")
        with pytest.raises(ValueError, match="likely hardcoded test date"):
            atlas_db.record_plan(
                date=just_outside,
                market_id="sp500",
                plan_data={"trade_date": just_outside},
            )
