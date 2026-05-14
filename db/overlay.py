"""db/overlay — Overlay decisions, shadow log, ceasefire, and news intel CRUD.

All public functions are re-exported through db.atlas_db for backward compat.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import db.atlas_db as _adb

__all__ = [
    "record_overlay_decision",
    "get_overlay_decisions",
    "update_overlay_outcome",
    "insert_overlay_shadow_event",
    "get_unevaluated_shadow_events",
    "update_shadow_outcome",
    "get_shadow_events",
    "upsert_ceasefire_factor",
    "get_ceasefire_factors",
    "record_ceasefire_history",
    "get_ceasefire_history",
    "record_news",
    "get_news",
]

_log = logging.getLogger(__name__)


def record_overlay_decision(
    timestamp: str,
    regime_state: str,
    action: str,
    sizing_override: Optional[float] = None,
    universes_deactivated: Optional[List[str]] = None,
    tickers_avoided: Optional[List[str]] = None,
    reasoning: Optional[str] = None,
    confidence: Optional[float] = None,
    data_sources: Optional[Dict] = None,
    dedup_window_seconds: int = 300,
) -> int:
    """Insert an overlay decision and return its id.

    Idempotency guard: if a matching row exists within dedup_window_seconds,
    returns its id without inserting a new row.
    """
    with _adb.get_db() as db:
        # -- Dedup guard ----------------------------------------------------
        try:
            candidate_dt = datetime.fromisoformat(timestamp)
            if candidate_dt.tzinfo is None:
                candidate_dt = candidate_dt.replace(tzinfo=timezone.utc)
            window_start = (candidate_dt - timedelta(seconds=dedup_window_seconds)).isoformat()
            window_end = (candidate_dt + timedelta(seconds=dedup_window_seconds)).isoformat()
            existing = db.execute(
                """
                SELECT id, timestamp
                FROM overlay_decisions
                WHERE regime_state = ?
                  AND action = ?
                  AND timestamp >= ?
                  AND timestamp <= ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (regime_state, action, window_start, window_end),
            ).fetchone()
            if existing is not None:
                _log.info(
                    "overlay: dedup hit -- returning existing id=%s from %s, "
                    "suppressed duplicate action=%s regime=%s",
                    existing["id"], existing["timestamp"], action, regime_state,
                )
                return existing["id"]
        except Exception as _dedup_err:
            _log.warning(
                "overlay: dedup check failed (%s) -- proceeding with insert",
                _dedup_err,
            )
        # -- End dedup guard ------------------------------------------------

        cursor = db.execute(
            """
            INSERT INTO overlay_decisions
                (timestamp, regime_state, action, sizing_override,
                 universes_deactivated, tickers_avoided, reasoning,
                 confidence, data_sources)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp, regime_state, action, sizing_override,
                json.dumps(universes_deactivated) if universes_deactivated is not None else None,
                json.dumps(tickers_avoided) if tickers_avoided is not None else None,
                reasoning, confidence,
                json.dumps(data_sources) if data_sources is not None else None,
            ),
        )
        return cursor.lastrowid


def get_overlay_decisions(
    days: Optional[int] = None,
    unevaluated_only: bool = False,
) -> List[Dict]:
    """Return overlay decisions, most recent first."""
    with _adb.get_db() as db:
        if unevaluated_only:
            query = (
                "SELECT * FROM overlay_decisions"
                " WHERE outcome_evaluated = 0"
                "   AND timestamp >= datetime('now', \'-365 days\')"
                " ORDER BY timestamp DESC"
            )
            params: List[Any] = []
        else:
            query = "SELECT * FROM overlay_decisions"
            params = []
            if days:
                query += " WHERE timestamp >= datetime('now', ?)"
                params.append(f"-{days} days")
            query += " ORDER BY timestamp DESC"
        rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            r = dict(row)
            for field in ("universes_deactivated", "tickers_avoided", "data_sources"):
                if r.get(field):
                    try:
                        r[field] = json.loads(r[field])
                    except (json.JSONDecodeError, TypeError):
                        pass
            result.append(r)
        return result


def update_overlay_outcome(
    decision_id: int,
    outcome_correct: int,
    outcome_notes: str,
    evaluated_at: Optional[str] = None,
) -> None:
    """Mark an overlay decision as evaluated."""
    with _adb.get_db() as db:
        db.execute(
            """
            UPDATE overlay_decisions
            SET outcome_evaluated = 1,
                outcome_correct   = ?,
                outcome_notes     = ?,
                evaluated_at      = ?
            WHERE id = ?
            """,
            (
                outcome_correct,
                outcome_notes,
                evaluated_at or datetime.now().isoformat(),
                decision_id,
            ),
        )


def insert_overlay_shadow_event(
    plan_id: str,
    ticker: str,
    market_id: str,
    original_size: float,
    overlay_size: float,
    sizing_multiplier: float,
    would_be_dollar_diff: Optional[float] = None,
    overlay_decision_id: Optional[int] = None,
    overlay_action: Optional[str] = None,
    overlay_reasoning: Optional[str] = None,
) -> int:
    """Insert a shadow event row. Returns row id. Non-fatal on failure (logs + returns -1)."""
    try:
        with _adb.get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO overlay_shadow_log
                    (plan_id, ticker, market_id, original_size, overlay_size,
                     sizing_multiplier, would_be_dollar_diff,
                     overlay_decision_id, overlay_action, overlay_reasoning)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (plan_id, ticker, market_id, original_size, overlay_size,
                 sizing_multiplier, would_be_dollar_diff,
                 overlay_decision_id, overlay_action, overlay_reasoning),
            )
            return cursor.lastrowid
    except Exception as exc:
        _log.warning("overlay_shadow: insert failed for %s/%s: %s", market_id, ticker, exc)
        return -1


def get_unevaluated_shadow_events(limit: int = 1000) -> List[Dict]:
    """Return all shadow events with actual_outcome_evaluated=0, oldest first."""
    with _adb.get_db() as db:
        rows = db.execute(
            """
            SELECT * FROM overlay_shadow_log
            WHERE actual_outcome_evaluated = 0
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_shadow_outcome(
    shadow_id: int,
    actual_outcome_pnl: float,
    evaluated_at: Optional[str] = None,
) -> None:
    """Mark a shadow event as evaluated."""
    with _adb.get_db() as db:
        db.execute(
            """
            UPDATE overlay_shadow_log
            SET actual_outcome_pnl = ?,
                actual_outcome_evaluated = 1,
                evaluated_at = ?
            WHERE id = ?
            """,
            (actual_outcome_pnl, evaluated_at or datetime.now().isoformat(), shadow_id),
        )


def get_shadow_events(
    days: Optional[int] = None,
    market_id: Optional[str] = None,
    ticker: Optional[str] = None,
) -> List[Dict]:
    """Return shadow events, most recent first, with optional filters."""
    with _adb.get_db() as db:
        query = "SELECT * FROM overlay_shadow_log WHERE 1=1"
        params: List[Any] = []
        if days:
            query += " AND created_at >= datetime('now', ?)"
            params.append(f"-{days} days")
        if market_id:
            query += " AND market_id = ?"
            params.append(market_id)
        if ticker:
            query += " AND ticker = ?"
            params.append(ticker)
        query += " ORDER BY created_at DESC"
        rows = db.execute(query, params).fetchall()
        return [dict(r) for r in rows]


# ── Ceasefire ────────────────────────────────────────────────────────────────

def upsert_ceasefire_factor(
    id: str,
    category: str,
    description: str,
    weight: float,
    active: int = 0,
    confidence: str = "medium",
    source: Optional[str] = None,
    last_updated: Optional[str] = None,
) -> None:
    """Insert or replace a ceasefire factor."""
    with _adb.get_db() as db:
        db.execute(
            """
            INSERT OR REPLACE INTO ceasefire_factors
                (id, category, description, weight, active, confidence, source, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                id, category, description, weight, active, confidence,
                source, last_updated or datetime.now().isoformat(),
            ),
        )


def get_ceasefire_factors(category: Optional[str] = None) -> List[Dict]:
    """Return ceasefire factors, optionally filtered by category."""
    with _adb.get_db() as db:
        query = "SELECT * FROM ceasefire_factors"
        params: List[Any] = []
        if category:
            query += " WHERE category=?"
            params.append(category)
        query += " ORDER BY id"
        return [dict(r) for r in db.execute(query, params).fetchall()]


def record_ceasefire_history(
    timestamp: str,
    probability: float,
    active_factors: Optional[List[str]] = None,
    change_log: Optional[str] = None,
) -> None:
    """Insert or replace a ceasefire probability snapshot."""
    with _adb.get_db() as db:
        db.execute(
            """
            INSERT OR REPLACE INTO ceasefire_history
                (timestamp, probability, active_factors, change_log)
            VALUES (?, ?, ?, ?)
            """,
            (
                timestamp, probability,
                json.dumps(active_factors) if active_factors is not None else None,
                change_log,
            ),
        )


def get_ceasefire_history(days: Optional[int] = None) -> List[Dict]:
    """Return ceasefire history, most recent first."""
    with _adb.get_db() as db:
        query = "SELECT * FROM ceasefire_history"
        params: List[Any] = []
        if days:
            query += " WHERE timestamp >= datetime('now', ?)"
            params.append(f"-{days} days")
        query += " ORDER BY timestamp DESC"
        rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            r = dict(row)
            if r.get("active_factors"):
                try:
                    r["active_factors"] = json.loads(r["active_factors"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(r)
        return result


# ── News intel ────────────────────────────────────────────────────────────────

def record_news(
    timestamp: str,
    source: Optional[str] = None,
    headline: Optional[str] = None,
    url: Optional[str] = None,
    relevance_score: Optional[float] = None,
    category: Optional[str] = None,
    summary: Optional[str] = None,
) -> int:
    """Insert a news item. Returns the new id."""
    with _adb.get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO news_intel
                (timestamp, source, headline, url, relevance_score, category, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (timestamp, source, headline, url, relevance_score, category, summary),
        )
        return cursor.lastrowid


def get_news(
    days: Optional[int] = None,
    category: Optional[str] = None,
    limit: int = 100,
) -> List[Dict]:
    """Return news items, most recent first."""
    with _adb.get_db() as db:
        query = "SELECT * FROM news_intel WHERE 1=1"
        params: List[Any] = []
        if days:
            query += " AND timestamp >= datetime('now', ?)"
            params.append(f"-{days} days")
        if category:
            query += " AND category=?"
            params.append(category)
        query += f" ORDER BY timestamp DESC LIMIT {int(limit)}"
        return [dict(r) for r in db.execute(query, params).fetchall()]
