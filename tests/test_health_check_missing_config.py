"""Regression test: health_check handles decommissioned universe configs gracefully."""
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def test_missing_config_exits_zero_with_skipped_report(tmp_path):
    from scripts import health_check

    missing_cfg = tmp_path / "decommissioned.json"  # does not exist
    report = tmp_path / "report.json"

    with pytest.raises(SystemExit) as exc_info:
        health_check.main([
            "--config-path", str(missing_cfg),
            "--report-path", str(report),
        ])

    assert exc_info.value.code == 0, "missing config should exit cleanly"
    assert report.exists(), "SKIPPED report must be written"
    data = json.loads(report.read_text())
    assert data["status"] == "SKIPPED"
    assert "decommissioned" in data["message"].lower()
    assert data["config_path"] == str(missing_cfg)


def test_archived_asx_does_not_crash(tmp_path):
    """Smoke: pointing at the real archived path (now absent) exits 0."""
    from scripts import health_check

    cfg_path = PROJECT_ROOT / "config" / "active" / "asx.json"
    assert not cfg_path.exists(), "Precondition: asx.json must be archived"
    report = tmp_path / "asx_report.json"
    with pytest.raises(SystemExit) as exc_info:
        health_check.main([
            "--config-path", str(cfg_path),
            "--report-path", str(report),
        ])
    assert exc_info.value.code == 0
