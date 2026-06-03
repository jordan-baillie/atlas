"""Tests for the research search-history multiple-testing burden."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from research.cross_oos import search_history as sh  # noqa: E402


def _write_tsv(d: Path, name: str, sharpes, params):
    df = pd.DataFrame({
        "timestamp": [f"2026-01-{i+1:02d}T00:00:00" for i in range(len(sharpes))],
        "sharpe": sharpes,
        "params_changed": params,
        "status": ["keep"] * len(sharpes),
    })
    df.to_csv(d / f"{name}.tsv", sep="\t", index=False)


def test_search_burden_counts_distinct_and_variance(tmp_path):
    # 6 rows but only 3 distinct param signatures
    _write_tsv(tmp_path, "stratA",
               sharpes=[0.2, 0.4, 0.4, 0.6, 0.6, 0.8],
               params=["", "p=1", "p=1", "p=2", "p=2", "p=3"])
    out = sh.search_burden(["stratA"], results_dir=tmp_path)
    assert out is not None
    assert out["n_trials"] == 4          # distinct of {"", p=1, p=2, p=3}
    assert out["n_experiments"] == 6
    expected_var_ann = float(np.var([0.2, 0.4, 0.4, 0.6, 0.6, 0.8], ddof=1))
    assert abs(out["sr_variance_ann"] - expected_var_ann) < 1e-9
    assert abs(out["sr_variance_pp"] - expected_var_ann / 252) < 1e-12
    assert out["strategies_found"] == ["stratA"]


def test_search_burden_aggregates_multiple_strategies(tmp_path):
    _write_tsv(tmp_path, "a", [0.1, 0.3, 0.5], ["", "x=1", "x=2"])
    _write_tsv(tmp_path, "b", [0.2, 0.4], ["", "y=1"])
    out = sh.search_burden(["a", "b"], results_dir=tmp_path)
    assert out["n_trials"] == 3 + 2
    assert out["n_experiments"] == 5
    assert set(out["strategies_found"]) == {"a", "b"}


def test_search_burden_none_when_missing(tmp_path):
    assert sh.search_burden(["does_not_exist"], results_dir=tmp_path) is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
