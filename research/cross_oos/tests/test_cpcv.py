"""Tests for cross_oos.cpcv — combinatorial purged CV with purge + embargo."""
import math
import sys
from itertools import combinations
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from research.cross_oos import cpcv  # noqa: E402


def test_group_edges_cover_and_partition():
    edges = cpcv.group_edges(100, 6)
    assert edges[0][0] == 0 and edges[-1][1] == 100
    # contiguous, non-overlapping
    for i in range(1, len(edges)):
        assert edges[i][0] == edges[i - 1][1]


def test_combo_count_and_path_count():
    splits = cpcv.cpcv_splits(600, n_groups=6, k_test=2, embargo_pct=0.0, purge=0)
    assert len(splits) == math.comb(6, 2) == 15
    # phi = k/N * C(N,k) = 2/6 * 15 = 5
    assert cpcv.n_backtest_paths(6, 2) == 5


def test_no_train_test_overlap_and_full_coverage():
    n = 600
    for sp in cpcv.cpcv_splits(n, n_groups=6, k_test=2, embargo_pct=0.0, purge=0):
        tr, te = set(sp.train_idx.tolist()), set(sp.test_idx.tolist())
        assert tr.isdisjoint(te)
        # with no purge/embargo, train ∪ test covers everything
        assert tr | te == set(range(n))


def test_purge_embargo_removes_adjacent_train_rows():
    n = 600
    purge, embargo_pct = 3, 0.02
    embargo = math.ceil(embargo_pct * n)
    splits = cpcv.cpcv_splits(n, n_groups=6, k_test=2, embargo_pct=embargo_pct, purge=purge)
    for sp in splits:
        # leakage checker must report clean for the parameters we purged with
        assert not cpcv.has_leakage(sp, purge=purge, embargo=embargo), \
            f"leakage near test blocks {sp.test_groups}"
        # train should be strictly smaller than the no-purge case
    no_purge = cpcv.cpcv_splits(n, n_groups=6, k_test=2, embargo_pct=0.0, purge=0)
    assert sum(len(s.train_idx) for s in splits) < sum(len(s.train_idx) for s in no_purge)


def test_purge_actually_detected_when_absent():
    # Build a split with NO purge and assert the leakage checker flags adjacency
    n = 600
    splits = cpcv.cpcv_splits(n, n_groups=6, k_test=2, embargo_pct=0.0, purge=0)
    # at least one split has an interior test block with train neighbours → leakage at purge>=1
    assert any(cpcv.has_leakage(sp, purge=1, embargo=0) for sp in splits)


def test_adjacent_test_groups_merge_into_one_block():
    # groups (0,1) are adjacent → a single contiguous test block, so only the left edge
    # and right edge get purged (not an interior seam).
    n = 600
    sp = next(s for s in cpcv.cpcv_splits(n, 6, 2, embargo_pct=0.0, purge=2)
              if s.test_groups == (0, 1))
    # test indices must be one contiguous run
    te = np.sort(sp.test_idx)
    assert np.all(np.diff(te) == 1)


def test_invalid_params():
    with pytest.raises(ValueError):
        cpcv.cpcv_splits(100, n_groups=6, k_test=6)   # k must be < N
    with pytest.raises(ValueError):
        cpcv.group_edges(0, 6)
