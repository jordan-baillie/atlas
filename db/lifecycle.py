"""db/lifecycle — Strategy lifecycle (promotion stage) CRUD.

All public functions are re-exported through db.atlas_db for backward compat.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

import db.atlas_db as _adb

__all__ = [
    "_VALID_LIFECYCLE_STATES",
    "get_lifecycle_state",
    "set_lifecycle_state",
    "list_lifecycle_states",
]

_log = logging.getLogger(__name__)

_VALID_LIFECYCLE_STATES = {"RESEARCH", "PAPER", "LIVE", "RETIRED"}


def get_lifecycle_state(strategy: str, universe: str) -> Optional[str]:
    """Return current promotion state for (strategy, universe), or None if not tracked."""
    try:
        with _adb.get_db() as db:
            row = db.execute(
                "SELECT state FROM strategy_lifecycle WHERE strategy = ? AND universe = ?",
                (strategy, universe),
            ).fetchone()
            return row["state"] if row else None
    except Exception as exc:
        _log.warning("get_lifecycle_state(%s, %s) failed: %s", strategy, universe, exc)
        return None


def set_lifecycle_state(
    strategy: str,
    universe: str,
    new_state: str,
    reason: str = "",
    auto_promotion_id: Optional[str] = None,
    operator: str = "system",
) -> None:
    """Transition (strategy, universe) to new_state.

    Atomically upserts strategy_lifecycle row and appends a history row.
    """
    if new_state not in _VALID_LIFECYCLE_STATES:
        raise ValueError(
            f"set_lifecycle_state: invalid state {new_state!r}. "
            f"Must be one of {sorted(_VALID_LIFECYCLE_STATES)}"
        )

    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    try:
        with _adb.get_db() as db:
            existing = db.execute(
                """SELECT state, paper_start_date, paper_end_date
                   FROM strategy_lifecycle
                   WHERE strategy = ? AND universe = ?""",
                (strategy, universe),
            ).fetchone()

            prev_state = existing["state"] if existing else None
            paper_start = existing["paper_start_date"] if existing else None
            paper_end = existing["paper_end_date"] if existing else None

            if new_state == "PAPER" and not paper_start:
                paper_start = now_iso
            if prev_state == "PAPER" and new_state != "PAPER":
                paper_end = now_iso

            db.execute(
                """INSERT INTO strategy_lifecycle
                       (strategy, universe, state, entered_state_at, prev_state,
                        transition_reason, paper_start_date, paper_end_date,
                        auto_promotion_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(strategy, universe) DO UPDATE SET
                       state             = excluded.state,
                       entered_state_at  = excluded.entered_state_at,
                       prev_state        = excluded.prev_state,
                       transition_reason = excluded.transition_reason,
                       paper_start_date  = excluded.paper_start_date,
                       paper_end_date    = excluded.paper_end_date,
                       auto_promotion_id = excluded.auto_promotion_id
                """,
                (strategy, universe, new_state, now_iso, prev_state,
                 reason or None, paper_start, paper_end, auto_promotion_id),
            )

            db.execute(
                """INSERT INTO strategy_lifecycle_history
                       (strategy, universe, from_state, to_state, transitioned_at,
                        reason, auto_promotion_id, operator)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (strategy, universe, prev_state, new_state, now_iso,
                 reason or None, auto_promotion_id, operator),
            )

    except Exception as exc:
        _log.error(
            "set_lifecycle_state(%s, %s, %s) failed: %s", strategy, universe, new_state, exc
        )
        raise


def list_lifecycle_states(state: Optional[str] = None) -> List[Dict]:
    """List all tracked (strategy, universe) rows, optionally filtered by state."""
    try:
        with _adb.get_db() as db:
            if state is not None:
                rows = db.execute(
                    "SELECT * FROM strategy_lifecycle WHERE state = ? ORDER BY universe, strategy",
                    (state,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM strategy_lifecycle ORDER BY universe, strategy"
                ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        _log.warning("list_lifecycle_states(state=%s) failed: %s", state, exc)
        return []
