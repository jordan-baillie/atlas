"""Test #192: brokers/state/live_*.json equity_history MUST have unique dates."""
import json
from pathlib import Path
import pytest

STATE_FILES = sorted((Path(__file__).resolve().parent.parent / "brokers" / "state").glob("live_*.json"))


@pytest.mark.parametrize("state_file", STATE_FILES, ids=lambda p: p.name)
def test_equity_history_no_duplicate_dates(state_file):
    """Each date should appear at most once in equity_history."""
    with open(state_file) as f:
        data = json.load(f)
    eh = data.get("equity_history", [])
    dates = [e["date"] for e in eh]
    assert len(dates) == len(set(dates)), (
        f"{state_file.name} has duplicate dates in equity_history: "
        f"{[d for d in dates if dates.count(d) > 1]}"
    )
