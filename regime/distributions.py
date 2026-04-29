"""
regime/distributions.py — Empirical return distributions conditional on regime state.

For each of the 6 regime states, builds an empirical distribution of SPY daily
log-returns observed when the market was in that regime. These distributions are
the foundation for:
- Monte Carlo path simulation conditional on current regime
- Regime-aware VaR/CVaR risk metrics
- Stress testing during regime transitions

Source data: SPY OHLCV joined against regime_history on date.
Cache: in-memory dict, refreshed if > 7 days old.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
from scipy import stats

from db.atlas_db import get_db
from regime.states import RegimeState

logger = logging.getLogger(__name__)

MIN_OBSERVATIONS = 30  # below this, fall back to unconditional distribution
CACHE_TTL_DAYS = 7


class RegimeDistributions:
    """Empirical return distributions per regime state."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path  # None = use atlas_db default
        self._cache: dict[str, np.ndarray] = {}  # regime_state -> return samples
        self._unconditional: Optional[np.ndarray] = None
        self._fitted_at: Optional[datetime] = None
        self._stats_cache: dict[str, dict] = {}

    def _is_cache_valid(self) -> bool:
        if self._fitted_at is None or not self._cache:
            return False
        return (datetime.now(timezone.utc).replace(tzinfo=None) - self._fitted_at) < timedelta(days=CACHE_TTL_DAYS)

    def fit(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        lookback_years: int = 5,
    ) -> "RegimeDistributions":
        """
        Build empirical return distributions from SPY × regime_history join.

        If start_date is None, uses end_date - lookback_years.
        If end_date is None, uses today.
        Returns self so the call can be chained.
        """
        # Compute date range
        if end_date is None:
            end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if start_date is None:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            start_dt = end_dt - timedelta(days=int(lookback_years * 365.25))
            start_date = start_dt.strftime("%Y-%m-%d")

        query = """
            SELECT o.date, o.close, r.regime_state
            FROM ohlcv o
            JOIN regime_history r ON o.date = r.date
            WHERE o.ticker = 'SPY'
              AND o.date >= ?
              AND o.date <= ?
            ORDER BY o.date
        """
        with get_db(self.db_path) as db:
            rows = db.execute(query, (start_date, end_date)).fetchall()

        if len(rows) < 2:
            raise RuntimeError(
                f"No SPY data found for {start_date}..{end_date}. "
                f"Run data ingestion first."
            )

        # Build aligned arrays
        closes = np.array([r["close"] for r in rows], dtype=np.float64)
        states = [r["regime_state"] for r in rows]
        # log returns; first row has NaN, drop it
        log_returns = np.diff(np.log(closes))
        states_for_returns = states[1:]  # align with returns

        # Group returns by regime state
        self._cache.clear()
        self._stats_cache.clear()
        self._unconditional = log_returns.copy()

        for state in RegimeState:
            mask = np.array([s == state.value for s in states_for_returns], dtype=bool)
            samples = log_returns[mask]
            if len(samples) < MIN_OBSERVATIONS:
                logger.warning(
                    "Regime %s has only %d observations (< %d). "
                    "Falling back to unconditional distribution.",
                    state.value, len(samples), MIN_OBSERVATIONS,
                )
                # Store the actual sparse samples but mark them; sample_returns
                # will use unconditional in the fallback path. We still keep the
                # original samples for stats reporting purposes.
                self._cache[state.value] = samples  # may be empty
            else:
                self._cache[state.value] = samples

        self._fitted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        # Optional: persist stats to DB (best-effort, ignore if table missing)
        try:
            self._persist_stats()
        except (sqlite3.Error, OSError, AttributeError) as e:
            logger.debug("Could not persist regime_distributions: %s", e)

        return self

    def _ensure_fitted(self):
        if not self._is_cache_valid():
            self.fit()

    def sample_returns(
        self,
        regime_state: str,
        n: int,
        seed: Optional[int] = None,
    ) -> np.ndarray:
        """Bootstrap n daily return samples for the given regime state."""
        self._ensure_fitted()
        if regime_state not in self._cache:
            raise ValueError(f"Unknown regime state: {regime_state}")

        samples = self._cache[regime_state]
        rng = np.random.default_rng(seed)

        if len(samples) < MIN_OBSERVATIONS:
            # Fallback: bootstrap from unconditional distribution
            assert self._unconditional is not None
            return rng.choice(self._unconditional, size=n, replace=True)

        return rng.choice(samples, size=n, replace=True)

    def regime_stats(self, regime_state: str) -> dict:
        """Return summary statistics for the given regime state."""
        self._ensure_fitted()
        if regime_state in self._stats_cache:
            return self._stats_cache[regime_state]
        if regime_state not in self._cache:
            raise ValueError(f"Unknown regime state: {regime_state}")

        samples = self._cache[regime_state]
        n_samples = int(len(samples))

        # If sparse, compute stats from the unconditional fallback distribution
        # but report n_samples as the actual observed count.
        if n_samples < MIN_OBSERVATIONS:
            assert self._unconditional is not None
            stat_samples = self._unconditional
            fallback = True
        else:
            stat_samples = samples
            fallback = False

        result = {
            "n_samples": n_samples,
            "fallback":  fallback,
            "mean":  float(np.mean(stat_samples)),
            "vol":   float(np.std(stat_samples, ddof=1)),
            "skew":  float(stats.skew(stat_samples)),
            "kurt":  float(stats.kurtosis(stat_samples)),
            "min":   float(np.min(stat_samples)),
            "max":   float(np.max(stat_samples)),
            "var_5":  float(np.percentile(stat_samples, 5)),
            "var_1":  float(np.percentile(stat_samples, 1)),
            "cvar_5": float(np.mean(stat_samples[stat_samples <= np.percentile(stat_samples, 5)])),
            "cvar_1": float(np.mean(stat_samples[stat_samples <= np.percentile(stat_samples, 1)])),
        }
        self._stats_cache[regime_state] = result
        return result

    def all_regime_stats(self) -> dict[str, dict]:
        """Return stats for every regime state."""
        self._ensure_fitted()
        return {state.value: self.regime_stats(state.value) for state in RegimeState}

    def sample_paths(
        self,
        regime_state: str,
        n_paths: int,
        n_days: int,
        seed: Optional[int] = None,
    ) -> np.ndarray:
        """
        Simulate n_paths × n_days cumulative log-return paths assuming the
        market stays in `regime_state` for the entire horizon.

        Returns: ndarray of shape (n_paths, n_days) where element [i, j] is
        the cumulative log return on day j of path i.
        """
        self._ensure_fitted()
        rng = np.random.default_rng(seed)
        # Sample n_paths * n_days returns at once for speed
        flat_returns = self.sample_returns(regime_state, n_paths * n_days, seed=seed)
        daily_returns = flat_returns.reshape(n_paths, n_days)
        cum_returns = np.cumsum(daily_returns, axis=1)
        return cum_returns

    def _persist_stats(self):
        """Best-effort write of summary stats to regime_distributions table."""
        with get_db(self.db_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS regime_distributions (
                    state      TEXT PRIMARY KEY,
                    mean       REAL,
                    vol        REAL,
                    skew       REAL,
                    kurt       REAL,
                    var_5      REAL,
                    cvar_5     REAL,
                    n_samples  INTEGER,
                    fitted_at  TEXT
                )
            """)
            for state in RegimeState:
                s = self.regime_stats(state.value)
                db.execute(
                    """
                    INSERT OR REPLACE INTO regime_distributions
                        (state, mean, vol, skew, kurt, var_5, cvar_5, n_samples, fitted_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (state.value, s["mean"], s["vol"], s["skew"], s["kurt"],
                     s["var_5"], s["cvar_5"], s["n_samples"],
                     self._fitted_at.isoformat() if self._fitted_at else None),
                )


def _print_summary_table(rd: RegimeDistributions) -> None:
    """Print a fixed-width summary table."""
    header = f"{'Regime':<22} {'N':>6} {'Mean':>10} {'Vol':>8} {'Skew':>8} {'VaR5':>9} {'CVaR5':>9}"
    print(header)
    print("-" * len(header))
    for state in RegimeState:
        s = rd.regime_stats(state.value)
        marker = "*" if s["fallback"] else " "
        print(
            f"{state.value:<22} {s['n_samples']:>6} "
            f"{s['mean']:>10.4f} {s['vol']:>8.4f} {s['skew']:>8.2f} "
            f"{s['var_5']:>9.4f} {s['cvar_5']:>9.4f}{marker}"
        )
    print("\n* = sparse data, stats computed from unconditional fallback")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    rd = RegimeDistributions()
    rd.fit(lookback_years=10)
    _print_summary_table(rd)
