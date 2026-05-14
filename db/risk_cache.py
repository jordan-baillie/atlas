"""db/risk_cache — Risk cache tables (regime transitions, ruin probability, portfolio risk).

All public functions are re-exported through db.atlas_db for backward compat.

Note: _risk_cache_tables_ensured is kept in db.atlas_db so that tests can patch
it there.  This module reads/writes it via the module object to ensure test
patches propagate correctly.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import db.atlas_db as _adb

__all__ = [
    "_ensure_risk_cache_tables",
    "get_cached_regime_transitions",
    "set_cached_regime_transitions",
    "get_cached_ruin_probability",
    "set_cached_ruin_probability",
    "get_cached_portfolio_risk",
]

_log = logging.getLogger(__name__)


def _ensure_risk_cache_tables() -> None:
    """Idempotent migration: create risk cache tables if they don't exist.

    Reads and writes _risk_cache_tables_ensured on the db.atlas_db module so
    that monkeypatch.setattr(_adb, "_risk_cache_tables_ensured", False) in
    tests correctly resets the guard.
    """
    if getattr(_adb, "_risk_cache_tables_ensured", False):
        return
    with _adb.get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS regime_transitions_cache (
                as_of          TEXT PRIMARY KEY,
                matrix_json    TEXT NOT NULL,
                window_days    INTEGER NOT NULL,
                n_observations INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rtc_as_of
            ON regime_transitions_cache(as_of DESC)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ruin_probability (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                as_of TEXT NOT NULL,
                current_equity REAL,
                floor REAL,
                floor_pct REAL,
                n_paths INTEGER,
                horizon_days INTEGER NOT NULL,
                prob_ruin REAL,
                worst_case_equity REAL,
                worst_5pct_equity REAL,
                median_end_equity REAL,
                tickers TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(as_of, horizon_days, floor_pct)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_risk (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                as_of TEXT NOT NULL,
                equity REAL NOT NULL,
                positions_value REAL NOT NULL,
                positions_count INTEGER NOT NULL,
                tickers TEXT NOT NULL,
                correlation_avg REAL,
                correlation_max REAL,
                effective_bets REAL,
                var_1d_95 REAL,
                var_1d_99 REAL,
                cvar_1d_95 REAL,
                cvar_1d_99 REAL,
                var_5d_95 REAL,
                var_5d_99 REAL,
                cvar_5d_95 REAL,
                cvar_5d_99 REAL,
                var_1d_95_pct REAL,
                cvar_1d_95_pct REAL,
                method TEXT,
                n_paths INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(as_of, method)
            )
        """)
    _adb._risk_cache_tables_ensured = True


def get_cached_regime_transitions(max_age_hours: int = 24) -> Optional[Dict]:
    """Return the latest cached regime transition matrix if it is fresh."""
    _ensure_risk_cache_tables()
    with _adb.get_db() as conn:
        row = conn.execute("""
            SELECT *
            FROM   regime_transitions_cache
            WHERE  as_of = (SELECT MAX(as_of) FROM regime_transitions_cache)
              AND  (julianday('now') - julianday(as_of)) * 24.0 <= ?
        """, (max_age_hours,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["matrix"] = json.loads(d["matrix_json"])
    except (json.JSONDecodeError, KeyError, TypeError):
        d["matrix"] = {}
    return d


def set_cached_regime_transitions(
    matrix: Dict,
    window_days: int,
    n_obs: int,
    as_of: Optional[str] = None,
) -> None:
    """Persist a regime transition matrix to the cache table."""
    _ensure_risk_cache_tables()
    ts = as_of or datetime.now(timezone.utc).isoformat()
    with _adb.get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO regime_transitions_cache
                (as_of, matrix_json, window_days, n_observations)
            VALUES (?, ?, ?, ?)
        """, (ts, json.dumps(matrix), window_days, n_obs))


def get_cached_ruin_probability(max_age_hours: int = 24) -> Optional[Dict]:
    """Return the latest cached ruin probability snapshot."""
    _ensure_risk_cache_tables()
    try:
        with _adb.get_db() as conn:
            age_row = conn.execute("""
                SELECT MAX(as_of) AS max_as_of
                FROM   ruin_probability
                WHERE  (julianday('now') - julianday(as_of)) * 24.0 <= ?
            """, (max_age_hours,)).fetchone()

        if not age_row or not age_row["max_as_of"]:
            return None

        as_of = age_row["max_as_of"]

        with _adb.get_db() as conn:
            rows = conn.execute("""
                SELECT * FROM ruin_probability
                WHERE  as_of = ?
                ORDER  BY horizon_days
            """, (as_of,)).fetchall()

            open_rows = conn.execute("""
                SELECT DISTINCT ticker FROM trades WHERE exit_date IS NULL
            """).fetchall()

        if not rows:
            return None

        first = dict(rows[0])
        try:
            cached_tickers = sorted(json.loads(first["tickers"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            cached_tickers = []

        current_tickers = sorted(r["ticker"] for r in open_rows)

        horizons: Dict[str, Any] = {}
        for r in rows:
            rd = dict(r)
            horizons[f"{rd['horizon_days']}d"] = {
                "days": rd["horizon_days"],
                "prob_ruin": rd["prob_ruin"],
                "worst_case_equity": rd["worst_case_equity"],
                "worst_5pct_equity": rd["worst_5pct_equity"],
                "median_end_equity": rd["median_end_equity"],
            }

        prob = (
            horizons.get("30d", list(horizons.values())[0] if horizons else {})
            .get("prob_ruin", 0.0)
        )

        stale = cached_tickers != current_tickers
        result: Dict[str, Any] = {
            "as_of": as_of,
            "prob": prob,
            "tickers": cached_tickers,
            "current_equity": first.get("current_equity"),
            "floor": first.get("floor"),
            "floor_pct": first.get("floor_pct"),
            "n_paths": first.get("n_paths"),
            "horizons": horizons,
            "stale": stale,
            "reason": "portfolio_changed" if stale else None,
            "source": "cache",
        }
        return result
    except Exception as exc:
        _log.warning("get_cached_ruin_probability failed: %s", exc)
        return None


def set_cached_ruin_probability(
    prob: float,
    tickers: List[str],
    n_positions: int,
    equity: float,
    params: Optional[Dict] = None,
) -> None:
    """Write a ruin probability snapshot directly to ``ruin_probability``."""
    _ensure_risk_cache_tables()
    params = params or {}
    as_of = params.get("as_of", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    floor_pct = params.get("floor_pct", 0.70)
    floor = equity * floor_pct
    n_paths = params.get("n_paths", 10_000)
    tickers_json = json.dumps(sorted(tickers))
    with _adb.get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO ruin_probability
                (as_of, current_equity, floor, floor_pct, n_paths,
                 horizon_days, prob_ruin,
                 worst_case_equity, worst_5pct_equity, median_end_equity,
                 tickers)
            VALUES (?, ?, ?, ?, ?, 30, ?, ?, ?, ?, ?)
        """, (
            as_of, equity, floor, floor_pct, n_paths,
            prob,
            equity * 0.70, equity * 0.75, equity * 1.05,
            tickers_json,
        ))


def get_cached_portfolio_risk(max_age_hours: int = 24) -> Optional[Dict]:
    """Return the latest portfolio_risk row if it is fresh."""
    _ensure_risk_cache_tables()
    try:
        with _adb.get_db() as conn:
            row = conn.execute("""
                SELECT *
                FROM   portfolio_risk
                WHERE  (julianday('now') - julianday(created_at)) * 24.0 <= ?
                ORDER  BY as_of DESC, created_at DESC
                LIMIT  1
            """, (max_age_hours,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["tickers"] = json.loads(d["tickers"]) if d["tickers"] else []
        except (json.JSONDecodeError, TypeError):
            d["tickers"] = []
        return d
    except Exception as exc:
        _log.warning("get_cached_portfolio_risk failed: %s", exc)
        return None
