"""Tests for core/orchestrator.py — Phase C.3 shadow scaffold.

Covers:
  - run_cycle(shadow=True) returns complete summary for all markets
  - run_cycle(shadow=False) raises NotImplementedError on first step
  - run_cycle with subset of markets
  - argparse: --once --shadow, --no-shadow, --market accumulation
  - STEPS_PER_MARKET order matches design docs
  - Idempotency: two consecutive calls produce independent summaries
  - Log format contains SHADOW market=X step=Y for grep'ability

Run with: python -m pytest tests/test_orchestrator.py -v
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ATLAS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_ROOT))

from core.orchestrator import (  # noqa: E402
    ACTIVE_MARKETS,
    STEPS_PER_MARKET,
    main,
    run_cycle,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def frozen_now() -> datetime:
    return datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# run_cycle — shadow mode
# ---------------------------------------------------------------------------

class TestRunCycleShadowMode:
    """run_cycle(shadow=True) must return a well-formed summary."""

    def test_returns_summary_dict(self, frozen_now: datetime) -> None:
        result = run_cycle(shadow=True, now=frozen_now)
        assert isinstance(result, dict)

    def test_summary_has_required_keys(self, frozen_now: datetime) -> None:
        result = run_cycle(shadow=True, now=frozen_now)
        assert "started_at" in result
        assert "finished_at" in result
        assert "shadow" in result
        assert "markets" in result

    def test_shadow_flag_is_true(self, frozen_now: datetime) -> None:
        result = run_cycle(shadow=True, now=frozen_now)
        assert result["shadow"] is True

    def test_started_at_matches_now(self, frozen_now: datetime) -> None:
        result = run_cycle(shadow=True, now=frozen_now)
        assert result["started_at"] == frozen_now.isoformat()

    def test_all_active_markets_present(self, frozen_now: datetime) -> None:
        result = run_cycle(shadow=True, now=frozen_now)
        for mkt in ACTIVE_MARKETS:
            assert mkt in result["markets"], f"Market {mkt!r} missing from summary"

    def test_all_steps_present_for_each_market(self, frozen_now: datetime) -> None:
        result = run_cycle(shadow=True, now=frozen_now)
        expected_steps = [name for name, _ in STEPS_PER_MARKET]
        for mkt, mkt_data in result["markets"].items():
            actual_steps = [s["step"] for s in mkt_data["steps"]]
            assert actual_steps == expected_steps, (
                f"Market {mkt!r}: expected steps {expected_steps}, got {actual_steps}"
            )

    def test_all_steps_have_shadow_outcome(self, frozen_now: datetime) -> None:
        result = run_cycle(shadow=True, now=frozen_now)
        for mkt, mkt_data in result["markets"].items():
            for step in mkt_data["steps"]:
                assert step["outcome"] == "shadow", (
                    f"Market {mkt!r} step {step['step']!r}: expected outcome='shadow', "
                    f"got {step['outcome']!r}"
                )

    def test_step_count_matches_steps_per_market(self, frozen_now: datetime) -> None:
        result = run_cycle(shadow=True, now=frozen_now)
        for mkt, mkt_data in result["markets"].items():
            assert len(mkt_data["steps"]) == len(STEPS_PER_MARKET), (
                f"Market {mkt!r}: expected {len(STEPS_PER_MARKET)} steps, "
                f"got {len(mkt_data['steps'])}"
            )


# ---------------------------------------------------------------------------
# run_cycle — real mode (not yet wired)
# ---------------------------------------------------------------------------

class TestRunCycleRealMode:
    """run_cycle(shadow=False) must raise NotImplementedError (not yet wired)."""

    def test_raises_not_implemented(self, frozen_now: datetime) -> None:
        with pytest.raises(NotImplementedError):
            run_cycle(shadow=False, now=frozen_now)

    def test_error_mentions_step_name(self, frozen_now: datetime) -> None:
        """Error message should name the step that failed."""
        with pytest.raises(NotImplementedError, match=STEPS_PER_MARKET[0][0]):
            run_cycle(shadow=False, now=frozen_now)


# ---------------------------------------------------------------------------
# run_cycle — market filtering
# ---------------------------------------------------------------------------

class TestRunCycleMarketFilter:
    """run_cycle with a subset of markets only processes those markets."""

    def test_single_market_sp500(self, frozen_now: datetime) -> None:
        result = run_cycle(markets=("sp500",), shadow=True, now=frozen_now)
        assert set(result["markets"].keys()) == {"sp500"}

    def test_two_markets(self, frozen_now: datetime) -> None:
        result = run_cycle(markets=("sp500", "commodity_etfs"), shadow=True, now=frozen_now)
        assert set(result["markets"].keys()) == {"sp500", "commodity_etfs"}

    def test_empty_markets_tuple_produces_empty_summary(self, frozen_now: datetime) -> None:
        result = run_cycle(markets=(), shadow=True, now=frozen_now)
        assert result["markets"] == {}

    def test_custom_market_name_is_processed(self, frozen_now: datetime) -> None:
        """Orchestrator doesn't validate market names — passes through."""
        result = run_cycle(markets=("test_market",), shadow=True, now=frozen_now)
        assert "test_market" in result["markets"]


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

class TestArgparse:
    """CLI argument parsing."""

    def test_once_and_shadow(self) -> None:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--shadow", action="store_true", default=True)
        parser.add_argument("--no-shadow", dest="shadow", action="store_false")
        parser.add_argument("--market", action="append")
        args = parser.parse_args(["--once", "--shadow"])
        assert args.once is True
        assert args.shadow is True

    def test_no_shadow_parses_correctly(self) -> None:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--shadow", action="store_true", default=True)
        parser.add_argument("--no-shadow", dest="shadow", action="store_false")
        parser.add_argument("--market", action="append")
        args = parser.parse_args(["--no-shadow", "--once"])
        assert args.shadow is False
        assert args.once is True

    def test_multiple_market_flags_accumulate(self) -> None:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--shadow", action="store_true", default=True)
        parser.add_argument("--no-shadow", dest="shadow", action="store_false")
        parser.add_argument("--market", action="append")
        args = parser.parse_args(["--market", "sp500", "--market", "commodity_etfs", "--once"])
        assert args.market == ["sp500", "commodity_etfs"]
        assert args.once is True

    def test_main_shadow_returns_zero(self, frozen_now: datetime) -> None:
        """main() with shadow=True returns exit code 0."""
        rc = main(["--once", "--shadow"])
        assert rc == 0


# ---------------------------------------------------------------------------
# STEPS_PER_MARKET order
# ---------------------------------------------------------------------------

class TestStepsOrder:
    """STEPS_PER_MARKET order must match design doc."""

    def test_first_step_is_sync_broker_orders(self) -> None:
        assert STEPS_PER_MARKET[0][0] == "sync_broker_orders"

    def test_second_step_is_reconcile_fills(self) -> None:
        assert STEPS_PER_MARKET[1][0] == "reconcile_fills"

    def test_third_step_is_sync_protective(self) -> None:
        assert STEPS_PER_MARKET[2][0] == "sync_protective"

    def test_fourth_step_is_healthz(self) -> None:
        assert STEPS_PER_MARKET[3][0] == "healthz"

    def test_exactly_four_steps(self) -> None:
        assert len(STEPS_PER_MARKET) == 4


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    """Two consecutive run_cycle() calls produce independent summaries."""

    def test_two_calls_produce_independent_summaries(self) -> None:
        now1 = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
        now2 = datetime(2026, 4, 29, 12, 15, 0, tzinfo=timezone.utc)
        result1 = run_cycle(shadow=True, now=now1)
        result2 = run_cycle(shadow=True, now=now2)
        # started_at timestamps differ (no global state leak)
        assert result1["started_at"] != result2["started_at"]

    def test_second_call_has_correct_markets(self) -> None:
        now1 = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
        now2 = datetime(2026, 4, 29, 12, 15, 0, tzinfo=timezone.utc)
        run_cycle(shadow=True, now=now1)  # first call — mutate anything?
        result2 = run_cycle(shadow=True, now=now2)
        assert set(result2["markets"].keys()) == set(ACTIVE_MARKETS)

    def test_second_call_step_count_unchanged(self) -> None:
        now1 = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
        now2 = datetime(2026, 4, 29, 12, 15, 0, tzinfo=timezone.utc)
        run_cycle(shadow=True, now=now1)
        result2 = run_cycle(shadow=True, now=now2)
        for mkt_data in result2["markets"].values():
            assert len(mkt_data["steps"]) == len(STEPS_PER_MARKET)


# ---------------------------------------------------------------------------
# Log format
# ---------------------------------------------------------------------------

class TestLogFormat:
    """Log messages must include SHADOW market=X step=Y for grep'ability."""

    def test_shadow_log_format(self, caplog: pytest.LogCaptureFixture, frozen_now: datetime) -> None:
        with caplog.at_level(logging.INFO, logger="atlas.orchestrator"):
            run_cycle(markets=("sp500",), shadow=True, now=frozen_now)
        # At least one log line must match the grep pattern
        shadow_lines = [r.message for r in caplog.records if "SHADOW" in r.message]
        assert shadow_lines, "No log lines with 'SHADOW' found — log format broken"

    def test_shadow_log_contains_market_name(
        self, caplog: pytest.LogCaptureFixture, frozen_now: datetime
    ) -> None:
        with caplog.at_level(logging.INFO, logger="atlas.orchestrator"):
            run_cycle(markets=("sp500",), shadow=True, now=frozen_now)
        matching = [r.message for r in caplog.records if "market=sp500" in r.message]
        assert matching, "No log line with 'market=sp500' found"

    def test_shadow_log_contains_step_name(
        self, caplog: pytest.LogCaptureFixture, frozen_now: datetime
    ) -> None:
        with caplog.at_level(logging.INFO, logger="atlas.orchestrator"):
            run_cycle(markets=("sp500",), shadow=True, now=frozen_now)
        for step_name, _ in STEPS_PER_MARKET:
            matching = [
                r.message for r in caplog.records
                if f"step={step_name}" in r.message
            ]
            assert matching, f"No log line with 'step={step_name}' found"
