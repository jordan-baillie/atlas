"""Tests for cross_oos.splitters — leave-one-group-out + regime stratification."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from research.cross_oos import splitters as sp  # noqa: E402


def test_leave_one_group_out_disjoint_and_covering():
    labels = ["A", "A", "B", "B", "C"]
    splits = sp.leave_one_group_out(labels)
    assert [s.held_out for s in splits] == ["A", "B", "C"]
    # each split: train ∪ test = all rows, disjoint
    covered_as_test = []
    for s in splits:
        tr, te = set(s.train_idx.tolist()), set(s.test_idx.tolist())
        assert tr.isdisjoint(te)
        assert tr | te == set(range(len(labels)))
        covered_as_test += s.test_idx.tolist()
    # every row is the test row of exactly one split
    assert sorted(covered_as_test) == list(range(len(labels)))


def test_leave_one_asset_and_venue_aliases():
    assert sp.leave_one_asset_out(["X", "Y"])[0].held_out == "X"
    assert sp.leave_one_venue_out(["bybit", "okx"])[0].held_out == "bybit"


def test_regime_labels_bull_bear_chop():
    flat = [100.0] * 10
    up = list(np.arange(100, 200, 5.0))
    down = list(np.arange(200, 100, -5.0))
    prices = pd.Series(flat + up + down)
    lab = sp.regime_labels(prices, trend_window=5, chop_band=0.05)
    # warmup rows are 'unknown'
    assert (lab.iloc[:4] == "unknown").all()
    # the heart of the up-ramp is bull, the heart of the down-ramp is bear
    up_mid = 10 + len(up) // 2
    down_mid = 10 + len(up) + len(down) // 2
    assert lab.iloc[up_mid] == "bull"
    assert lab.iloc[down_mid] == "bear"
    # the flat plateau (post-warmup) contains chop
    assert (lab.iloc[5:10] == "chop").any()
    # all three regimes present
    assert {"bull", "bear", "chop"}.issubset(set(lab.unique()))


def test_regime_stratify_partitions_and_excludes_unknown():
    labels = ["unknown", "bull", "bull", "bear", "chop", "bear"]
    strat = sp.regime_stratify(labels)
    assert set(strat.keys()) == {"bull", "bear", "chop"}
    assert strat["bull"].tolist() == [1, 2]
    assert strat["bear"].tolist() == [3, 5]
    # union of regime indices excludes the 'unknown' row 0
    allidx = sorted(np.concatenate(list(strat.values())).tolist())
    assert allidx == [1, 2, 3, 4, 5]
    # include_unknown=True surfaces it
    assert "unknown" in sp.regime_stratify(labels, include_unknown=True)
