"""Tests for the track-vs-expectation gate."""
from atlas.execution.track_expectation import Expectation, evaluate


def test_insufficient_obs():
    v = evaluate([0.001] * 5, Expectation(0.001, 0.01), min_obs=20)
    assert v.status == "insufficient" and v.ok


def test_on_track_matches_model():
    model = Expectation(daily_mean=0.0008, daily_std=0.01, sharpe=1.0)
    r = [0.0008 + 0.01, 0.0008 - 0.01] * 30          # mean 0.0008, std ~0.01 -> Sharpe ~1.27
    v = evaluate(r, model)
    assert v.status == "on_track" and v.expectancy_positive and v.ok


def test_halt_on_negative_expectancy():
    v = evaluate([-0.0003] * 60, Expectation(0.0008, 0.01, 1.0))
    assert v.status == "halt" and not v.expectancy_positive and not v.ok


def test_diverging_mean_far_below_model():
    # positive but far below a high-mean model
    v = evaluate([0.0001] * 60, Expectation(daily_mean=0.003, daily_std=0.005, sharpe=2.0))
    assert v.status == "diverging" and v.expectancy_positive and v.mean_z < -3


def test_diverging_daily_anomaly():
    model = Expectation(0.0008, 0.01, 1.0)
    r = [0.0008] * 59 + [0.09]                        # one 9% day = 9 std off model
    v = evaluate(r, model)
    assert v.status == "diverging" and v.worst_daily_z > 4
