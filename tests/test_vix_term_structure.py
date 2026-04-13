"""Tests for signals.vix_term_structure."""
from __future__ import annotations
import pytest
from unittest.mock import patch

from signals.vix_term_structure import (
    classify_term_structure,
    compute_persistence,
    get_current_signal,
)


# ---------------------------------------------------------------------------
# classify_term_structure — value tests
# ---------------------------------------------------------------------------

def test_classify_strong_contango():
    assert classify_term_structure(0.90) == "strong_contango"


def test_classify_contango():
    assert classify_term_structure(0.97) == "contango"


def test_classify_flat():
    assert classify_term_structure(1.02) == "flat"


def test_classify_backwardation():
    assert classify_term_structure(1.10) == "backwardation"


def test_classify_extreme_backwardation():
    assert classify_term_structure(1.25) == "extreme_backwardation"


def test_classify_none():
    assert classify_term_structure(None) == "unknown"


# ---------------------------------------------------------------------------
# classify_term_structure — boundary tests
# ---------------------------------------------------------------------------

def test_boundary_0_95_is_contango():
    """0.95 is NOT strong_contango — it falls into contango (ratio < 1.00)."""
    assert classify_term_structure(0.95) == "contango"


def test_boundary_1_00_is_flat():
    """1.00 is NOT contango — it falls into flat (ratio < 1.05)."""
    assert classify_term_structure(1.00) == "flat"


def test_boundary_1_05_is_backwardation():
    """1.05 is NOT flat — it falls into backwardation (ratio < 1.20)."""
    assert classify_term_structure(1.05) == "backwardation"


def test_boundary_1_20_is_extreme_backwardation():
    """1.20 is NOT backwardation — it falls into extreme_backwardation."""
    assert classify_term_structure(1.20) == "extreme_backwardation"


# ---------------------------------------------------------------------------
# compute_persistence
# ---------------------------------------------------------------------------

def test_persistence_empty():
    assert compute_persistence([]) == 0


def test_persistence_streak():
    """Last 3 entries are backwardation -> persistence = 3."""
    history = [
        {"regime": "contango"},
        {"regime": "contango"},
        {"regime": "backwardation"},
        {"regime": "backwardation"},
        {"regime": "backwardation"},
    ]
    assert compute_persistence(history) == 3


def test_persistence_unique_tail():
    """Last regime appears only once -> persistence = 1."""
    history = [
        {"regime": "contango"},
        {"regime": "contango"},
        {"regime": "flat"},
    ]
    assert compute_persistence(history) == 1


def test_persistence_all_same():
    history = [{"regime": "contango"}] * 7
    assert compute_persistence(history) == 7


def test_persistence_single_entry():
    assert compute_persistence([{"regime": "extreme_backwardation"}]) == 1


# ---------------------------------------------------------------------------
# get_current_signal — live DB (permissive: error key OR full payload)
# ---------------------------------------------------------------------------

def test_get_current_signal_shape():
    """Signal returns either an error dict or a fully-shaped payload."""
    result = get_current_signal()
    assert isinstance(result, dict)

    if "error" in result:
        pytest.skip("No VIX data in DB — skipping shape check")

    required_keys = {
        "as_of", "vix", "vix3m", "ratio", "regime",
        "persistence_days", "action", "severity",
        "ratio_30d_mean", "ratio_30d_max", "ratio_30d_min",
        "history",
    }
    assert required_keys.issubset(result.keys()), (
        f"Missing keys: {required_keys - result.keys()}"
    )
    assert isinstance(result["history"], list)
    assert result["persistence_days"] >= 1


# ---------------------------------------------------------------------------
# get_current_signal — action mapping (monkeypatched, no DB required)
# ---------------------------------------------------------------------------

def _make_backwardation_history(n: int) -> list:
    """Synthetic history: n consecutive days of backwardation."""
    return [
        {
            "date": f"2026-01-{i+1:02d}",
            "vix": 22.0,
            "vix3m": 20.0,
            "ratio": 1.10,
            "regime": "backwardation",
        }
        for i in range(n)
    ]


def test_action_reduce_gross_after_3_days_backwardation(monkeypatch):
    """5 consecutive backwardation days -> REDUCE_GROSS, severity medium."""
    monkeypatch.setattr(
        "signals.vix_term_structure.get_vix_term_structure",
        lambda **kwargs: _make_backwardation_history(5),
    )
    signal = get_current_signal()
    assert signal["action"] == "REDUCE_GROSS"
    assert signal["severity"] in ("medium", "high")


def test_action_watch_for_single_extreme_backwardation(monkeypatch):
    """1 day of extreme_backwardation -> WATCH, severity high (not yet REDUCE_GROSS)."""
    history = [
        {
            "date": "2026-01-01",
            "vix": 26.0,
            "vix3m": 21.0,
            "ratio": 1.238,
            "regime": "extreme_backwardation",
        }
    ]
    monkeypatch.setattr(
        "signals.vix_term_structure.get_vix_term_structure",
        lambda **kwargs: history,
    )
    signal = get_current_signal()
    assert signal["action"] == "WATCH"
    assert signal["severity"] == "high"


def test_action_normal_for_contango(monkeypatch):
    """Contango regime -> NORMAL action."""
    history = [
        {
            "date": f"2026-01-{i+1:02d}",
            "vix": 15.0,
            "vix3m": 17.0,
            "ratio": 0.882,
            "regime": "contango",
        }
        for i in range(5)
    ]
    monkeypatch.setattr(
        "signals.vix_term_structure.get_vix_term_structure",
        lambda **kwargs: history,
    )
    signal = get_current_signal()
    assert signal["action"] == "NORMAL"
    assert signal["severity"] == "low"


def test_action_watch_for_flat(monkeypatch):
    """Flat regime -> WATCH, severity low."""
    history = [
        {
            "date": "2026-01-01",
            "vix": 20.0,
            "vix3m": 19.5,
            "ratio": 1.026,
            "regime": "flat",
        }
    ]
    monkeypatch.setattr(
        "signals.vix_term_structure.get_vix_term_structure",
        lambda **kwargs: history,
    )
    signal = get_current_signal()
    assert signal["action"] == "WATCH"
    assert signal["severity"] == "low"


def test_get_current_signal_empty_db(monkeypatch):
    """Empty history -> error dict."""
    monkeypatch.setattr(
        "signals.vix_term_structure.get_vix_term_structure",
        lambda **kwargs: [],
    )
    signal = get_current_signal()
    assert "error" in signal
