"""
tests/test_state_order_id_uniqueness.py
-----------------------------------------
Regression tests for the CAT stop_order_id / tp_order_id collision fix.

Bug report: commit aaafb2d9 placed the same UUID into both stop_order_id and
tp_order_id for the CAT position. A subsequent repair correctly set
stop_order_id but cleared tp_order_id to "" instead of the correct TP leg
UUID.  This test suite guards against that class of error returning.

Coverage:
  1. Live state files — all live_*.json positions must have
     stop_order_id != tp_order_id (or one/both empty).
  2. CAT-specific assertion — tp_order_id must match the known retro-TP UUID.
  3. Collision-detector unit test — synthetic fixture with a colliding pair
     must be surfaced by find_collisions().
  4. Clean fixture — non-colliding pairs must not raise a false positive.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_state_order_id_collisions import find_collisions

STATE_DIR = PROJECT_ROOT / "brokers" / "state"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_state_files() -> list[tuple[str, dict]]:
    """Return (market_name, parsed_json) pairs for all live_*.json files."""
    return [
        (f.stem.replace("live_", ""), json.loads(f.read_text()))
        for f in sorted(STATE_DIR.glob("live_*.json"))
    ]


# ---------------------------------------------------------------------------
# 1. Live state files — no stop_order_id == tp_order_id collisions
# ---------------------------------------------------------------------------

class TestLiveStateFiles:
    """All live_*.json positions must have distinct (or empty) order IDs."""

    def test_at_least_one_state_file_exists(self) -> None:
        files = list(STATE_DIR.glob("live_*.json"))
        assert len(files) >= 1, "No live_*.json files found in brokers/state/"

    @pytest.mark.parametrize(
        "market,data",
        _load_state_files(),
    )
    def test_no_stop_tp_collision_in_market(self, market: str, data: dict) -> None:
        """Every position in a market state file must have distinct order IDs."""
        positions = data.get("positions", [])
        collisions = []
        for pos in positions:
            ticker = pos.get("ticker", "?")
            stop_id = pos.get("stop_order_id", "")
            tp_id = pos.get("tp_order_id", "")
            if stop_id and tp_id and stop_id == tp_id:
                collisions.append(
                    f"market={market} ticker={ticker} uuid={stop_id}"
                )
        assert not collisions, (
            f"stop_order_id == tp_order_id collision(s) detected:\n"
            + "\n".join(f"  {c}" for c in collisions)
        )

    @pytest.mark.parametrize(
        "market,data",
        _load_state_files(),
    )
    def test_order_ids_are_strings(self, market: str, data: dict) -> None:
        """order ID fields must be strings (or absent) — not None or int."""
        for pos in data.get("positions", []):
            ticker = pos.get("ticker", "?")
            for field in ("stop_order_id", "tp_order_id", "order_id"):
                val = pos.get(field)
                if val is not None:
                    assert isinstance(val, str), (
                        f"market={market} ticker={ticker} field={field} "
                        f"expected str, got {type(val).__name__!r}: {val!r}"
                    )


# ---------------------------------------------------------------------------
# 2. CAT-specific — known correct UUIDs after 2026-05-12 repair
# ---------------------------------------------------------------------------

class TestCATPositionRepair:
    """Verify the CAT position in live_sp500.json has the repaired UUIDs."""

    _EXPECTED_STOP = "a1021664-51c8-4ac1-aecc-a8d70feb7cf8"
    _EXPECTED_TP = "3d035b5f-3926-4d2d-9506-0c588e691fcb"
    _SP500_STATE = STATE_DIR / "live_sp500.json"

    def _find_cat(self) -> dict | None:
        if not self._SP500_STATE.exists():
            return None
        data = json.loads(self._SP500_STATE.read_text())
        for pos in data.get("positions", []):
            if pos.get("ticker") == "CAT":
                return pos
        return None

    def test_cat_stop_order_id_is_correct(self) -> None:
        pos = self._find_cat()
        if pos is None:
            pytest.skip("CAT not currently in live_sp500.json positions (may have been exited)")
        assert pos["stop_order_id"] == self._EXPECTED_STOP, (
            f"CAT stop_order_id mismatch: got {pos['stop_order_id']!r}"
        )

    def test_cat_tp_order_id_is_correct(self) -> None:
        pos = self._find_cat()
        if pos is None:
            pytest.skip("CAT not currently in live_sp500.json positions")
        assert pos["tp_order_id"] == self._EXPECTED_TP, (
            f"CAT tp_order_id is {pos['tp_order_id']!r}; "
            f"expected {self._EXPECTED_TP!r} (retro-TP OCO limit leg)"
        )

    def test_cat_stop_and_tp_differ(self) -> None:
        pos = self._find_cat()
        if pos is None:
            pytest.skip("CAT not currently in live_sp500.json positions")
        stop = pos.get("stop_order_id", "")
        tp = pos.get("tp_order_id", "")
        assert stop != tp or (not stop and not tp), (
            f"CAT has collision: stop_order_id == tp_order_id == {stop!r}"
        )


# ---------------------------------------------------------------------------
# 3. Collision-detector unit tests (synthetic fixtures)
# ---------------------------------------------------------------------------

class TestCollisionDetector:
    """Test find_collisions() helper from audit_state_order_id_collisions."""

    def _write_state(self, tmp_dir: Path, filename: str, positions: list[dict]) -> Path:
        data = {"market_id": "test", "mode": "live", "positions": positions}
        p = tmp_dir / filename
        p.write_text(json.dumps(data))
        return p

    def test_detects_stop_tp_collision(self, tmp_path: Path) -> None:
        """A position with stop_order_id == tp_order_id must be flagged."""
        self._write_state(
            tmp_path,
            "live_sp500.json",
            [
                {
                    "ticker": "FAKE",
                    "stop_order_id": "aaaa-bbbb-cccc-dddd",
                    "tp_order_id": "aaaa-bbbb-cccc-dddd",  # collision!
                }
            ],
        )
        result = find_collisions(state_dir=tmp_path)
        assert result["collision_count"] == 1
        assert result["collisions"][0]["ticker"] == "FAKE"
        assert result["collisions"][0]["colliding_uuid"] == "aaaa-bbbb-cccc-dddd"

    def test_no_false_positive_when_both_empty(self, tmp_path: Path) -> None:
        """stop_order_id='' and tp_order_id='' must NOT be a collision."""
        self._write_state(
            tmp_path,
            "live_sp500.json",
            [{"ticker": "CLEAN", "stop_order_id": "", "tp_order_id": ""}],
        )
        result = find_collisions(state_dir=tmp_path)
        assert result["collision_count"] == 0

    def test_no_false_positive_distinct_uuids(self, tmp_path: Path) -> None:
        """Different UUIDs for stop and TP must not be reported."""
        self._write_state(
            tmp_path,
            "live_sp500.json",
            [
                {
                    "ticker": "GOOD",
                    "stop_order_id": "aaaa-1111-2222-3333",
                    "tp_order_id": "bbbb-1111-2222-3333",
                }
            ],
        )
        result = find_collisions(state_dir=tmp_path)
        assert result["collision_count"] == 0

    def test_no_false_positive_stop_only(self, tmp_path: Path) -> None:
        """stop set, tp empty — no TP leg yet, must not be a collision."""
        self._write_state(
            tmp_path,
            "live_sp500.json",
            [{"ticker": "STOPONLY", "stop_order_id": "aaaa-0000-0000-0000", "tp_order_id": ""}],
        )
        result = find_collisions(state_dir=tmp_path)
        assert result["collision_count"] == 0

    def test_multi_market_collision_detection(self, tmp_path: Path) -> None:
        """Collisions in multiple markets are all reported."""
        shared_uuid = "dead-beef-dead-beef"
        self._write_state(
            tmp_path,
            "live_sp500.json",
            [{"ticker": "AAAA", "stop_order_id": shared_uuid, "tp_order_id": shared_uuid}],
        )
        self._write_state(
            tmp_path,
            "live_sector_etfs.json",
            [{"ticker": "BBBB", "stop_order_id": shared_uuid, "tp_order_id": shared_uuid}],
        )
        result = find_collisions(state_dir=tmp_path)
        assert result["collision_count"] == 2
        tickers = {c["ticker"] for c in result["collisions"]}
        assert tickers == {"AAAA", "BBBB"}

    def test_files_scanned_count(self, tmp_path: Path) -> None:
        """Scanned file count matches the number of live_*.json files."""
        for name in ("live_sp500.json", "live_sector_etfs.json", "live_commodity_etfs.json"):
            self._write_state(tmp_path, name, [])
        result = find_collisions(state_dir=tmp_path)
        assert result["files_scanned"] == 3

    def test_empty_state_dir_is_clean(self, tmp_path: Path) -> None:
        """An empty directory (no live_*.json) returns zero collisions."""
        result = find_collisions(state_dir=tmp_path)
        assert result["collision_count"] == 0
        assert result["files_scanned"] == 0

    def test_missing_order_id_fields_treated_as_empty(self, tmp_path: Path) -> None:
        """Positions with no stop_order_id/tp_order_id keys at all are fine."""
        self._write_state(
            tmp_path,
            "live_sp500.json",
            [{"ticker": "NOFIELDS"}],  # no stop_order_id or tp_order_id keys
        )
        result = find_collisions(state_dir=tmp_path)
        assert result["collision_count"] == 0


# ---------------------------------------------------------------------------
# 4. Audit-JSON integrity (sanity check that the repair was recorded)
# ---------------------------------------------------------------------------

class TestAuditJSON:
    """Verify that the audit JSON was written and contains expected keys."""

    _AUDIT_PATH = PROJECT_ROOT / "data" / "audit" / "cat_state_repair_2026-05-12.json"

    def test_audit_file_exists(self) -> None:
        assert self._AUDIT_PATH.exists(), (
            f"Audit JSON not found at {self._AUDIT_PATH}"
        )

    def test_audit_has_required_keys(self) -> None:
        if not self._AUDIT_PATH.exists():
            pytest.skip("Audit file not created yet")
        data = json.loads(self._AUDIT_PATH.read_text())
        for key in ("timestamp", "actor", "before_state", "alpaca_state", "diagnosis",
                    "after_state", "collision_audit", "backup_paths"):
            assert key in data, f"Audit JSON missing required key: {key!r}"

    def test_collision_audit_shows_zero_post_repair(self) -> None:
        if not self._AUDIT_PATH.exists():
            pytest.skip("Audit file not created yet")
        data = json.loads(self._AUDIT_PATH.read_text())
        ca = data.get("collision_audit", {})
        assert ca.get("collision_count", -1) == 0, (
            f"collision_audit.collision_count expected 0, got {ca.get('collision_count')}"
        )
