"""db/macro — Macro indicators and treasury yield curve CRUD.

All public functions are re-exported through db.atlas_db for backward compat.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import db.atlas_db as _adb

__all__ = [
    "_MACRO_INDICATOR_COLS",
    "_TREASURY_CURVE_COLS",
    "upsert_macro_indicators",
    "batch_upsert_macro_indicators",
    "get_macro_indicators",
    "batch_upsert_treasury_curve",
    "get_treasury_curve",
]

# Columns allowed in the macro_indicators table (excludes date and updated_at).
_MACRO_INDICATOR_COLS: frozenset = frozenset({
    "vix", "vix3m", "vix_term_ratio",
    "yield_10y", "yield_2y", "yield_3m",
    "yield_curve_10y2y", "yield_curve_10y3m",
    "credit_oas", "dxy",
    "gold", "copper", "gold_copper_ratio",
    "fed_funds", "unemployment_claims",
    "spy_close", "spy_200dma", "spy_above_200dma", "spy_200dma_slope",
    "put_call_ratio",
    "skew_index", "breadth_rsp_spy",
    # Treasury curve derived metrics (Phase 3.1)
    "treasury_slope", "treasury_curvature", "treasury_level",
})

# All columns in treasury_curve table (excludes date and updated_at).
_TREASURY_CURVE_COLS: frozenset = frozenset({
    "yield_1m", "yield_3m", "yield_6m",
    "yield_1y", "yield_2y", "yield_3y",
    "yield_5y", "yield_7y", "yield_10y",
    "yield_20y", "yield_30y",
    "treasury_slope", "treasury_curvature", "treasury_level",
})


def _clean(v: Any) -> Any:
    """Convert NaN/inf to None so SQLite stores NULL."""
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def upsert_macro_indicators(date: str, **fields) -> None:
    """Insert or replace a macro indicators row for the given date."""
    safe = {k: _clean(v) for k, v in fields.items() if k in _MACRO_INDICATOR_COLS}

    if not safe:
        with _adb.get_db() as db:
            db.execute(
                "INSERT OR IGNORE INTO macro_indicators (date) VALUES (?)",
                (date,),
            )
        return

    sorted_keys = sorted(safe.keys())
    cols = ["date"] + sorted_keys
    placeholders = ", ".join(["?"] * len(cols))
    cols_str = ", ".join(cols)
    values = [date] + [safe[k] for k in sorted_keys]

    with _adb.get_db() as db:
        db.execute(
            f"INSERT OR REPLACE INTO macro_indicators ({cols_str}) VALUES ({placeholders})",
            values,
        )


def batch_upsert_macro_indicators(rows: List[Dict]) -> int:
    """Batch-insert macro indicator rows in a single transaction.

    Returns the number of rows written.
    """
    count = 0
    with _adb.get_db() as db:
        for row in rows:
            date = row.get("date")
            if not date:
                continue
            safe = {k: _clean(v) for k, v in row.items() if k in _MACRO_INDICATOR_COLS}
            if not safe:
                db.execute(
                    "INSERT OR IGNORE INTO macro_indicators (date) VALUES (?)",
                    (date,),
                )
            else:
                sorted_keys = sorted(safe.keys())
                cols = ["date"] + sorted_keys
                placeholders = ", ".join(["?"] * len(cols))
                cols_str = ", ".join(cols)
                values = [date] + [safe[k] for k in sorted_keys]
                db.execute(
                    f"INSERT OR REPLACE INTO macro_indicators ({cols_str}) VALUES ({placeholders})",
                    values,
                )
            count += 1
    return count


def get_macro_indicators(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    days: Optional[int] = None,
) -> List[Dict]:
    """Return macro indicators rows ordered by date ascending."""
    with _adb.get_db() as db:
        query = "SELECT * FROM macro_indicators WHERE 1=1"
        params: List[Any] = []
        if days:
            query += " AND date >= date('now', ?)"
            params.append(f"-{days} days")
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date ASC"
        return [dict(r) for r in db.execute(query, params).fetchall()]


def batch_upsert_treasury_curve(rows: List[Dict]) -> int:
    """Batch-insert Treasury yield curve rows in a single transaction.

    Returns the number of rows written.
    """
    count = 0
    with _adb.get_db() as db:
        for row in rows:
            date = row.get("date")
            if not date:
                continue
            safe = {k: _clean(v) for k, v in row.items() if k in _TREASURY_CURVE_COLS}
            sorted_keys = sorted(safe.keys())
            cols = ["date"] + sorted_keys
            placeholders = ", ".join(["?"] * len(cols))
            cols_str = ", ".join(cols)
            values = [date] + [safe[k] for k in sorted_keys]
            db.execute(
                f"INSERT OR REPLACE INTO treasury_curve ({cols_str}) VALUES ({placeholders})",
                values,
            )
            count += 1
    return count


def get_treasury_curve(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    days: Optional[int] = None,
) -> List[Dict]:
    """Return treasury_curve rows ordered by date ascending."""
    with _adb.get_db() as db:
        query = "SELECT * FROM treasury_curve WHERE 1=1"
        params: List[Any] = []
        if days:
            query += " AND date >= date('now', ?)"
            params.append(f"-{days} days")
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date ASC"
        return [dict(r) for r in db.execute(query, params).fetchall()]
