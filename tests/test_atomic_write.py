"""Unit tests for atomic_json_write utility.

Run with:  python -m pytest tests/test_atomic_write.py -v
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research.models import atomic_json_write


class TestAtomicJsonWrite:
    """Tests for atomic_json_write utility."""

    # ── Correctness ──────────────────────────────────────────────────────────

    def test_writes_valid_json(self, tmp_path):
        """Atomic write produces valid, round-trippable JSON."""
        target = tmp_path / "test.json"
        data = {"key": "value", "numbers": [1, 2, 3], "nested": {"a": 1}}

        atomic_json_write(target, data)

        assert target.exists()
        with open(target) as f:
            loaded = json.load(f)
        assert loaded == data

    def test_writes_list_data(self, tmp_path):
        """Lists (the common case for queue/journal) are written correctly."""
        target = tmp_path / "list.json"
        data = [{"id": 1}, {"id": 2}, {"id": 3}]

        atomic_json_write(target, data)

        with open(target) as f:
            loaded = json.load(f)
        assert loaded == data

    def test_overwrites_existing_file(self, tmp_path):
        """Atomic write correctly replaces an existing file."""
        target = tmp_path / "test.json"
        with open(target, "w") as f:
            json.dump({"original": True}, f)

        atomic_json_write(target, {"updated": True})

        with open(target) as f:
            loaded = json.load(f)
        assert loaded == {"updated": True}

    def test_non_serializable_uses_str_default(self, tmp_path):
        """Non-JSON-serializable objects are coerced to strings (via default=str)."""
        from datetime import datetime

        target = tmp_path / "test.json"
        data = {"timestamp": datetime(2024, 1, 1, 12, 0, 0)}

        atomic_json_write(target, data)

        with open(target) as f:
            loaded = json.load(f)
        assert "timestamp" in loaded
        assert isinstance(loaded["timestamp"], str)

    def test_creates_parent_directories(self, tmp_path):
        """Missing parent directories are created automatically."""
        target = tmp_path / "nested" / "deep" / "test.json"

        atomic_json_write(target, {"nested": True})

        assert target.exists()
        with open(target) as f:
            assert json.load(f) == {"nested": True}

    # ── Crash-safety ─────────────────────────────────────────────────────────

    def test_original_preserved_when_rename_fails(self, tmp_path):
        """If os.replace raises (simulating a crash), the original is untouched."""
        target = tmp_path / "test.json"
        original_data = {"original": "intact", "important": [1, 2, 3]}

        # Write original
        with open(target, "w") as f:
            json.dump(original_data, f)

        # Simulate failure at the rename step (temp is fully written but rename fails)
        with patch("os.replace", side_effect=OSError("simulated rename failure")):
            with pytest.raises(OSError, match="simulated rename failure"):
                atomic_json_write(target, {"new": "data that should not land"})

        # Original must be completely intact
        assert target.exists()
        with open(target) as f:
            loaded = json.load(f)
        assert loaded == original_data

    def test_tmp_file_cleaned_up_on_failure(self, tmp_path):
        """The .tmp sibling is removed after a write failure (no stale artefacts)."""
        target = tmp_path / "test.json"
        tmp_expected = tmp_path / "test.json.tmp"

        with patch("os.replace", side_effect=OSError("simulated failure")):
            with pytest.raises(OSError):
                atomic_json_write(target, {"data": "value"})

        assert not tmp_expected.exists(), ".tmp file should be cleaned up on error"

    def test_no_tmp_file_remains_after_success(self, tmp_path):
        """No .tmp sibling is left after a successful write."""
        target = tmp_path / "queue.json"
        tmp_expected = tmp_path / "queue.json.tmp"

        atomic_json_write(target, [{"id": "exp-1"}])

        assert target.exists()
        assert not tmp_expected.exists(), ".tmp file should be gone after successful rename"

    # ── Edge cases ───────────────────────────────────────────────────────────

    def test_empty_dict(self, tmp_path):
        """Empty dict writes and reads back correctly."""
        target = tmp_path / "empty.json"
        atomic_json_write(target, {})
        with open(target) as f:
            assert json.load(f) == {}

    def test_empty_list(self, tmp_path):
        """Empty list writes and reads back correctly."""
        target = tmp_path / "empty.json"
        atomic_json_write(target, [])
        with open(target) as f:
            assert json.load(f) == []

    def test_large_data(self, tmp_path):
        """Large data (simulating a full journal) is written correctly."""
        target = tmp_path / "journal.json"
        data = [{"experiment_id": f"exp-{i}", "verdict": "pass", "metrics": list(range(50))}
                for i in range(500)]

        atomic_json_write(target, data)

        with open(target) as f:
            loaded = json.load(f)
        assert len(loaded) == 500
        assert loaded[0]["experiment_id"] == "exp-0"
        assert loaded[-1]["experiment_id"] == "exp-499"
