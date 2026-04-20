"""Unit tests for regime.distributions."""
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from regime.distributions import RegimeDistributions, MIN_OBSERVATIONS
from regime.states import RegimeState

# ---------------------------------------------------------------------------
# Isolated seeded DB — copy just the tables this module needs from prod.
# No writes go to production data/atlas.db.
# ---------------------------------------------------------------------------

_PROD_DB = Path(__file__).resolve().parent.parent / "data" / "atlas.db"


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory):
    """Create a module-scoped tmp SQLite DB with SPY OHLCV + regime_history.

    Reads are done via a direct sqlite3 connection to the prod DB (bypassing
    _db_path_override so the isolation fixture does not redirect this open).
    All writes (including _persist_stats) go to the seeded tmp DB.
    """
    tmp = tmp_path_factory.mktemp("regime_dist") / "seeded.db"

    src = sqlite3.connect(f"file:{_PROD_DB}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    dst = sqlite3.connect(str(tmp))
    dst.execute("PRAGMA journal_mode=WAL")
    dst.execute("PRAGMA foreign_keys=ON")
    dst.execute("""
        CREATE TABLE IF NOT EXISTS ohlcv (
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            adj_close REAL,
            volume INTEGER NOT NULL,
            universe TEXT NOT NULL,
            source TEXT DEFAULT 'tiingo',
            PRIMARY KEY (ticker, date)
        )
    """)
    dst.execute("""
        CREATE TABLE IF NOT EXISTS regime_history (
            date TEXT PRIMARY KEY,
            regime_state TEXT NOT NULL,
            trend_score REAL,
            risk_score REAL,
            active_universes TEXT,
            sizing_multiplier REAL DEFAULT 1.0,
            enabled_strategies TEXT,
            reasoning TEXT,
            model_version TEXT
        )
    """)
    dst.execute("""
        CREATE TABLE IF NOT EXISTS regime_distributions (
            state TEXT PRIMARY KEY,
            mean REAL, vol REAL, skew REAL, kurt REAL,
            var_5 REAL, cvar_5 REAL, n_samples INTEGER, fitted_at TEXT
        )
    """)

    # Copy SPY OHLCV rows
    spy_rows = src.execute(
        "SELECT ticker,date,open,high,low,close,adj_close,volume,universe,source "
        "FROM ohlcv WHERE ticker='SPY' ORDER BY date"
    ).fetchall()
    dst.executemany(
        "INSERT OR IGNORE INTO ohlcv "
        "(ticker,date,open,high,low,close,adj_close,volume,universe,source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(r["ticker"],r["date"],r["open"],r["high"],r["low"],r["close"],
          r["adj_close"],r["volume"],r["universe"],r["source"])
         for r in spy_rows],
    )

    # Copy regime_history rows
    rh_rows = src.execute(
        "SELECT date,regime_state,trend_score,risk_score,active_universes,"
        "sizing_multiplier,enabled_strategies,reasoning,model_version "
        "FROM regime_history ORDER BY date"
    ).fetchall()
    dst.executemany(
        "INSERT OR IGNORE INTO regime_history "
        "(date,regime_state,trend_score,risk_score,active_universes,"
        "sizing_multiplier,enabled_strategies,reasoning,model_version) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(r["date"],r["regime_state"],r["trend_score"],r["risk_score"],
          r["active_universes"],r["sizing_multiplier"],r["enabled_strategies"],
          r["reasoning"],r["model_version"])
         for r in rh_rows],
    )

    dst.commit()
    src.close()
    dst.close()
    return str(tmp)


@pytest.fixture(scope="module")
def fitted(seeded_db):
    """Fit RegimeDistributions against the seeded tmp DB (not prod)."""
    rd = RegimeDistributions(db_path=seeded_db)
    rd.fit(lookback_years=10)
    return rd


def test_fit_populates_all_six_states(fitted):
    for state in RegimeState:
        assert state.value in fitted._cache


def test_all_regime_stats_returns_six(fitted):
    stats = fitted.all_regime_stats()
    assert len(stats) == 6
    for state in RegimeState:
        assert state.value in stats


def test_stats_keys_present(fitted):
    s = fitted.regime_stats(RegimeState.BULL_RISK_ON.value)
    required = {"mean", "vol", "skew", "kurt", "var_5", "var_1",
                "cvar_5", "cvar_1", "n_samples", "min", "max"}
    assert required.issubset(set(s.keys()))


def test_vol_positive(fitted):
    for state in RegimeState:
        s = fitted.regime_stats(state.value)
        assert s["vol"] > 0, f"{state.value} has non-positive vol"


def test_mean_close_to_historical_drift(fitted):
    s = fitted.regime_stats(RegimeState.BULL_RISK_ON.value)
    # Daily SPY drift is roughly 0.03%-0.05% in bull regimes
    assert -0.01 < s["mean"] < 0.01, f"bull_risk_on mean implausible: {s['mean']}"


def test_sample_returns_shape(fitted):
    samples = fitted.sample_returns(RegimeState.BULL_RISK_ON.value, n=500, seed=42)
    assert samples.shape == (500,)
    assert samples.dtype == np.float64


def test_sample_paths_shape(fitted):
    paths = fitted.sample_paths(
        RegimeState.BULL_RISK_ON.value, n_paths=100, n_days=20, seed=42
    )
    assert paths.shape == (100, 20)


def test_seed_reproducibility(fitted):
    a = fitted.sample_returns(RegimeState.BULL_RISK_ON.value, n=200, seed=123)
    b = fitted.sample_returns(RegimeState.BULL_RISK_ON.value, n=200, seed=123)
    np.testing.assert_array_equal(a, b)


def test_sparse_regime_falls_back(fitted):
    # bear_capitulation has only ~12 observations in real data
    s = fitted.regime_stats(RegimeState.BEAR_CAPITULATION.value)
    assert s["n_samples"] < MIN_OBSERVATIONS
    assert s["fallback"] is True


def test_unknown_regime_raises(fitted):
    with pytest.raises(ValueError):
        fitted.sample_returns("not_a_regime", n=10)


def test_var_less_than_mean(fitted):
    s = fitted.regime_stats(RegimeState.BULL_RISK_ON.value)
    assert s["var_5"] < s["mean"]


def test_cvar_5_le_var_5(fitted):
    s = fitted.regime_stats(RegimeState.BULL_RISK_ON.value)
    assert s["cvar_5"] <= s["var_5"]
