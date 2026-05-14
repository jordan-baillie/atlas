"""Regression tests for run_compute_matrix.py (#216).

Tests the 3-outcome classification logic introduced in the #216 fix:
  - ok            : rc=0, ≥1 keeps
  - no_keeps       : rc=0 (or rc=2 before fix), sweep ran but 0 keeps
  - error          : rc!=0, genuine sweep crash
  - benchmark_unavailable : benchmark data missing, sweep skipped gracefully
  - config_missing : no config/active/*.json, workers crash with assertion
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make sure project root is on path
PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from scripts.run_compute_matrix import (
    _parse_log_for_outcome,
    _SUCCESS_STATUSES,
    main,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_log(tmp_path: Path, content: str) -> Path:
    """Write a fake universe log file and return its path."""
    p = tmp_path / "sp500_20260514_120000.log"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# _parse_log_for_outcome
# ---------------------------------------------------------------------------

class TestParseLogForOutcome:
    """Unit tests for the log-parsing helper."""

    def test_sentinel_completed_no_keeps_returns_no_keeps(self, tmp_path: Path) -> None:
        """ATLAS_NIGHTLY_STATUS sentinel with completed_no_keeps → 'no_keeps'."""
        content = (
            "Some preamble\n"
            'ATLAS_NIGHTLY_STATUS: {"status": "completed_no_keeps", "universe": "sp500", "screened": 50, "kept": 0}\n'
            "Some suffix\n"
        )
        log = _write_log(tmp_path, content)
        assert _parse_log_for_outcome(log, 0) == "no_keeps"

    def test_sentinel_completed_no_keeps_on_rc2(self, tmp_path: Path) -> None:
        """Sentinel no_keeps even if returncode=2 (pre-fix behaviour)."""
        content = 'ATLAS_NIGHTLY_STATUS: {"status": "completed_no_keeps", "screened": 30, "kept": 0}\n'
        log = _write_log(tmp_path, content)
        assert _parse_log_for_outcome(log, 2) == "no_keeps"

    def test_summary_line_kept_gt0_returns_ok(self, tmp_path: Path) -> None:
        """'Total: 50 screened, 8 promoted, 3 kept' → 'ok'."""
        content = "  Total: 50 screened, 8 promoted, 3 kept\n  Runtime: 15.0 min\n"
        log = _write_log(tmp_path, content)
        assert _parse_log_for_outcome(log, 0) == "ok"

    def test_summary_line_kept_eq0_returns_no_keeps(self, tmp_path: Path) -> None:
        """'Total: 50 screened, 11 promoted, 0 kept' → 'no_keeps'."""
        content = "  Total: 50 screened, 11 promoted, 0 kept\n  Runtime: 63.0 min\n"
        log = _write_log(tmp_path, content)
        assert _parse_log_for_outcome(log, 0) == "no_keeps"

    def test_config_missing_pattern_returns_config_missing(self, tmp_path: Path) -> None:
        """'Config file not found' with rc!=0 and no summary → 'config_missing'."""
        content = (
            "14:46:57  WARNING  No active config for market 'defensive_etfs'\n"
            "AssertionError: ResearchSession market mismatch: config.market=None\n"
        )
        log = _write_log(tmp_path, content)
        assert _parse_log_for_outcome(log, 2) == "config_missing"

    def test_could_not_load_active_config_returns_config_missing(self, tmp_path: Path) -> None:
        """'Could not load active config' with no summary → 'config_missing'."""
        content = (
            "[filter] Could not load active config for gold_etfs: Config file not found\n"
            "  Total: 0 screened, 0 promoted, 0 kept\n"  # summary exists but rc!=0
        )
        log = _write_log(tmp_path, content)
        # rc!=0 with all-zero totals AND config missing → config_missing
        assert _parse_log_for_outcome(log, 2) == "config_missing"

    def test_benchmark_unavailable_rc0_returns_benchmark_unavailable(self, tmp_path: Path) -> None:
        """Benchmark-unavailable warning + rc=0 → 'benchmark_unavailable'."""
        content = (
            "WARNING: Benchmark DBC data unavailable, returning empty metrics\n"
            "WARNING: DBC.AX: no data returned from any source\n"
        )
        log = _write_log(tmp_path, content)
        assert _parse_log_for_outcome(log, 0) == "benchmark_unavailable"

    def test_rc0_no_useful_content_returns_ok(self, tmp_path: Path) -> None:
        """No sentinel, no summary, rc=0 → defaults to 'ok'."""
        content = "Sweep completed.\n"
        log = _write_log(tmp_path, content)
        assert _parse_log_for_outcome(log, 0) == "ok"

    def test_rc_nonzero_no_useful_content_returns_error(self, tmp_path: Path) -> None:
        """No sentinel, no summary, rc=1 → defaults to 'error'."""
        content = "Something went wrong\n"
        log = _write_log(tmp_path, content)
        assert _parse_log_for_outcome(log, 1) == "error"


# ---------------------------------------------------------------------------
# main() integration tests (mock run_universe_sweep)
# ---------------------------------------------------------------------------

class TestMainOutcomes:
    """Test main() return code with mocked run_universe_sweep results."""

    def _run_main(self, mock_results: dict[str, dict]) -> int:
        """Call main() with run_universe_sweep mocked to return given results."""
        universes = list(mock_results.keys())

        def fake_sweep(universe: str, hours: float, workers: int, dry_run: bool = False) -> dict:
            return mock_results[universe]

        with patch("scripts.run_compute_matrix.run_universe_sweep", side_effect=fake_sweep):
            return main(["--universes", ",".join(universes)])

    def test_main_returns_0_when_all_universes_have_0_keeps(self) -> None:
        """All universes return status='no_keeps' → main() returns 0.

        Regression: before #216, this would return 1 because rc=2 was treated
        as failure.
        """
        rc = self._run_main({
            "sp500":          {"universe": "sp500", "status": "no_keeps", "returncode": 0},
            "commodity_etfs": {"universe": "commodity_etfs", "status": "no_keeps", "returncode": 0},
        })
        assert rc == 0, f"Expected 0, got {rc}"

    def test_main_returns_1_when_one_universe_fails_genuinely(self) -> None:
        """One 'error' universe → main() returns 1."""
        rc = self._run_main({
            "sp500":          {"universe": "sp500", "status": "ok", "returncode": 0},
            "commodity_etfs": {"universe": "commodity_etfs", "status": "error", "returncode": 2},
        })
        assert rc == 1, f"Expected 1, got {rc}"

    def test_no_keeps_logged_explicitly(self, caplog: pytest.LogCaptureFixture) -> None:
        """A 'no_keeps' result must emit the explicit info message. (#216 spec)"""
        import logging

        def fake_sweep(universe: str, hours: float, workers: int, dry_run: bool = False) -> dict:
            return {"universe": universe, "status": "no_keeps", "returncode": 0}

        with patch("scripts.run_compute_matrix.run_universe_sweep", side_effect=fake_sweep):
            with caplog.at_level(logging.INFO, logger="run_compute_matrix"):
                main(["--universes", "sp500"])

        assert any(
            "0 keeps above silent-failure threshold" in r.message
            for r in caplog.records
        ), "Expected '0 keeps above silent-failure threshold' in logs"

    def test_main_returns_0_when_universe_has_benchmark_unavailable(self) -> None:
        """benchmark_unavailable status → main() returns 0 (graceful no-op)."""
        rc = self._run_main({
            "sp500": {"universe": "sp500", "status": "benchmark_unavailable", "returncode": 0},
        })
        assert rc == 0, f"Expected 0, got {rc}"

    def test_main_returns_0_when_universe_has_config_missing(self) -> None:
        """config_missing status → main() returns 0 (missing config is a known no-op)."""
        rc = self._run_main({
            "gold_etfs": {"universe": "gold_etfs", "status": "config_missing", "returncode": 2},
        })
        assert rc == 0, f"Expected 0, got {rc}"

    def test_main_returns_0_when_all_universes_ok(self) -> None:
        """All universes 'ok' → main() returns 0."""
        rc = self._run_main({
            "sp500":          {"universe": "sp500", "status": "ok", "returncode": 0},
            "commodity_etfs": {"universe": "commodity_etfs", "status": "ok", "returncode": 0},
        })
        assert rc == 0, f"Expected 0, got {rc}"

    def test_main_returns_0_mixed_ok_and_no_keeps(self) -> None:
        """Mix of 'ok' and 'no_keeps' → main() returns 0."""
        rc = self._run_main({
            "sp500":          {"universe": "sp500", "status": "ok", "returncode": 0},
            "commodity_etfs": {"universe": "commodity_etfs", "status": "no_keeps", "returncode": 0},
            "sector_etfs":    {"universe": "sector_etfs", "status": "no_keeps", "returncode": 0},
        })
        assert rc == 0, f"Expected 0, got {rc}"

    def test_success_statuses_constant_coverage(self) -> None:
        """All expected success statuses must be in _SUCCESS_STATUSES."""
        expected = {"ok", "dry_run", "no_keeps", "benchmark_unavailable", "config_missing"}
        assert expected.issubset(_SUCCESS_STATUSES), (
            f"Missing statuses in _SUCCESS_STATUSES: {expected - _SUCCESS_STATUSES}"
        )


# ---------------------------------------------------------------------------
# _count_rows_added date format
# ---------------------------------------------------------------------------

class TestCountRowsAddedDateFormat:
    """Verify the date-format fix in _count_rows_added (#216)."""

    def test_sqlite_format_used_for_cutoff(self) -> None:
        """_count_rows_added must query with SQLite-compatible date format.

        The fix: use strftime('%Y-%m-%d %H:%M:%S') instead of .isoformat()
        so the query string matches DEFAULT (datetime('now')) column format.
        Verifies the format fix is in place by inspecting the source.
        """
        import inspect
        from research import autoresearch_nightly
        src = inspect.getsource(autoresearch_nightly._count_rows_added)
        assert "isoformat()" not in src, (
            "_count_rows_added still uses .isoformat() — breaks SQLite "
            "date comparison (ISO 'T' separator != SQLite space separator)"
        )
        assert "strftime" in src, (
            "_count_rows_added must use strftime('%Y-%m-%d %H:%M:%S') for "
            "SQLite-compatible date cutoff"
        )

    def test_sqlite_rows_counted_correctly(self, tmp_path: Path) -> None:
        """Rows inserted AFTER session_start are correctly counted."""
        import sqlite3, time

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE research_experiments (
                id TEXT PRIMARY KEY,
                strategy TEXT,
                universe TEXT DEFAULT 'sp500',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()

        # session_start is 1 second BEFORE the row insert so that the row's
        # created_at (SQLite datetime('now')) is strictly > cutoff.
        # Same-second inserts would be equal (not >), so subtract 1 second.
        session_start = time.time() - 1
        # Insert a row AFTER session_start (SQLite datetime('now') = UTC now)
        conn.execute(
            "INSERT INTO research_experiments (id, strategy, universe) VALUES (?, ?, ?)",
            ("test-001", "mean_reversion", "sp500"),
        )
        conn.commit()
        conn.close()

        # Patch get_db to use our test DB
        import contextlib

        @contextlib.contextmanager
        def fake_get_db():
            c = sqlite3.connect(str(db_path))
            c.row_factory = sqlite3.Row
            try:
                yield c
            finally:
                c.close()

        with patch("db.atlas_db.get_db", fake_get_db):
            from research.autoresearch_nightly import _count_rows_added
            count = _count_rows_added("sp500", session_start)

        assert count == 1, (
            f"Expected 1 row counted after session_start, got {count}. "
            "This indicates the date-format fix is not applied correctly."
        )
