"""db/research — Research experiments and best-params CRUD.

All public functions are re-exported through db.atlas_db for backward compat.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import db.atlas_db as _adb

__all__ = [
    "record_experiment",
    "get_experiments",
    "update_experiment_status",
    "upsert_research_best",
    "get_research_best",
]

_log = logging.getLogger(__name__)


def record_experiment(
    id: str,
    strategy: str,
    universe: str = "sp500",
    experiment_type: Optional[str] = None,
    params_changed: Optional[Dict] = None,
    description: Optional[str] = None,
    sharpe: Optional[float] = None,
    trades: Optional[int] = None,
    max_dd_pct: Optional[float] = None,
    profit_factor: Optional[float] = None,
    cagr_pct: Optional[float] = None,
    status: str = "running",
    recommendation: Optional[str] = None,
    baseline_sharpe: Optional[float] = None,
    runtime_s: Optional[float] = None,
    agent_id: Optional[str] = None,
    completed_at: Optional[str] = None,
) -> None:
    """Insert a new research experiment."""
    with _adb.get_db() as db:
        db.execute(
            """
            INSERT INTO research_experiments
                (id, strategy, universe, experiment_type, params_changed, description,
                 sharpe, trades, max_dd_pct, profit_factor, cagr_pct, status,
                 recommendation, baseline_sharpe, runtime_s, agent_id, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                id, strategy, universe, experiment_type,
                json.dumps(params_changed) if params_changed is not None else None,
                description, sharpe, trades, max_dd_pct, profit_factor, cagr_pct,
                status, recommendation, baseline_sharpe, runtime_s, agent_id,
                completed_at,
            ),
        )


def get_experiments(
    strategy: Optional[str] = None,
    status: Optional[str] = None,
    universe: Optional[str] = None,
    limit: int = 50,
) -> List[Dict]:
    """Return research experiments, most recent first."""
    with _adb.get_db() as db:
        query = "SELECT * FROM research_experiments WHERE 1=1"
        params: List[Any] = []
        if strategy:
            query += " AND strategy=?"
            params.append(strategy)
        if status:
            query += " AND status=?"
            params.append(status)
        if universe:
            query += " AND universe=?"
            params.append(universe)
        query += f" ORDER BY created_at DESC LIMIT {int(limit)}"
        rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            r = dict(row)
            if r.get("params_changed"):
                try:
                    r["params_changed"] = json.loads(r["params_changed"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(r)
        return result


def update_experiment_status(
    experiment_id: str,
    status: str,
    recommendation: Optional[str] = None,
    sharpe: Optional[float] = None,
    trades: Optional[int] = None,
    max_dd_pct: Optional[float] = None,
    profit_factor: Optional[float] = None,
    cagr_pct: Optional[float] = None,
    runtime_s: Optional[float] = None,
    completed_at: Optional[str] = None,
) -> None:
    """Update the status and results of a research experiment."""
    with _adb.get_db() as db:
        db.execute(
            """
            UPDATE research_experiments
            SET status          = ?,
                recommendation  = COALESCE(?, recommendation),
                sharpe          = COALESCE(?, sharpe),
                trades          = COALESCE(?, trades),
                max_dd_pct      = COALESCE(?, max_dd_pct),
                profit_factor   = COALESCE(?, profit_factor),
                cagr_pct        = COALESCE(?, cagr_pct),
                runtime_s       = COALESCE(?, runtime_s),
                completed_at    = COALESCE(?, completed_at)
            WHERE id = ?
            """,
            (
                status, recommendation, sharpe, trades, max_dd_pct,
                profit_factor, cagr_pct, runtime_s,
                completed_at or datetime.now().isoformat(),
                experiment_id,
            ),
        )


def upsert_research_best(
    strategy: str,
    universe: str,
    params: Dict,
    sharpe: Optional[float] = None,
    trades: Optional[int] = None,
    max_dd_pct: Optional[float] = None,
    solo_sharpe: Optional[float] = None,
    portfolio_sharpe: Optional[float] = None,
    metric_type: Optional[str] = None,
    regime_state: Optional[str] = None,
    oos_sharpe: Optional[float] = None,
    oos_trades: Optional[int] = None,
    oos_cagr: Optional[float] = None,
    oos_max_dd: Optional[float] = None,
) -> None:
    """Insert or replace the best known parameters for (strategy, universe[, regime_state])."""
    # Compute metric_type if not supplied
    if metric_type is None:
        if solo_sharpe is not None and portfolio_sharpe is not None:
            metric_type = "both"
        elif solo_sharpe is not None:
            metric_type = "solo"
        elif portfolio_sharpe is not None:
            metric_type = "portfolio"

    if sharpe is not None and solo_sharpe is None and portfolio_sharpe is None:
        _log.debug(
            "research_best.sharpe is deprecated -- use solo_sharpe / portfolio_sharpe "
            "(strategy=%s universe=%s). Writing legacy-only row.",
            strategy, universe,
        )

    params_json = json.dumps(params)

    with _adb.get_db() as db:
        if regime_state is None:
            # Cross-regime (NULL) row -- SQLite NULL != NULL in a PK, so
            # ON CONFLICT won't fire for NULL.  Use DELETE + INSERT instead.
            db.execute(
                "DELETE FROM research_best "
                "WHERE strategy=? AND universe=? AND regime_state IS NULL",
                (strategy, universe),
            )
            db.execute(
                "INSERT INTO research_best "
                "(strategy, universe, regime_state, params, sharpe, trades, max_dd_pct, "
                " solo_sharpe, portfolio_sharpe, metric_type, "
                " oos_sharpe, oos_trades, oos_cagr, oos_max_dd, updated_at) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, COALESCE(?, 'unknown'), "
                "        ?, ?, ?, ?, datetime('now'))",
                (strategy, universe, params_json, sharpe, trades, max_dd_pct,
                 solo_sharpe, portfolio_sharpe, metric_type,
                 oos_sharpe, oos_trades, oos_cagr, oos_max_dd),
            )
        else:
            # Per-regime row
            db.execute(
                "INSERT OR REPLACE INTO research_best "
                "(strategy, universe, regime_state, params, sharpe, trades, max_dd_pct, "
                " solo_sharpe, portfolio_sharpe, metric_type, "
                " oos_sharpe, oos_trades, oos_cagr, oos_max_dd, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'unknown'), "
                "        ?, ?, ?, ?, datetime('now'))",
                (strategy, universe, regime_state, params_json, sharpe, trades, max_dd_pct,
                 solo_sharpe, portfolio_sharpe, metric_type,
                 oos_sharpe, oos_trades, oos_cagr, oos_max_dd),
            )


def get_research_best(
    strategy: Optional[str] = None,
    universe: Optional[str] = None,
    regime_state: Optional[str] = None,
    fallback_to_cross_regime: bool = True,
) -> List[Dict]:
    """Return research_best rows, optionally filtered."""

    def _fetch(db: Any, extra_clause: str, extra_params: List[Any]) -> List[Dict]:
        query = "SELECT * FROM research_best WHERE 1=1"
        qparams: List[Any] = []
        if strategy:
            query += " AND strategy=?"
            qparams.append(strategy)
        if universe:
            query += " AND universe=?"
            qparams.append(universe)
        query += extra_clause
        qparams.extend(extra_params)
        query += " ORDER BY strategy, universe"
        rows = db.execute(query, qparams).fetchall()
        result = []
        for row in rows:
            r = dict(row)
            if r.get("params"):
                try:
                    r["params"] = json.loads(r["params"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(r)
        return result

    with _adb.get_db() as db:
        if regime_state is None:
            return _fetch(db, " AND regime_state IS NULL", [])
        else:
            rows = _fetch(db, " AND regime_state=?", [regime_state])
            if not rows and fallback_to_cross_regime:
                rows = _fetch(db, " AND regime_state IS NULL", [])
            return rows
