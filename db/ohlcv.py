"""db/ohlcv — OHLCV price-data CRUD.

All public functions are re-exported through db.atlas_db for backward compat.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import db.atlas_db as _adb

__all__ = [
    "upsert_ohlcv",
    "get_ohlcv",
    "get_universe_data",
]


def upsert_ohlcv(
    ticker: str,
    date: str,
    o: float,
    h: float,
    l: float,
    c: float,
    adj: Optional[float],
    vol: int,
    universe: str,
    source: str = "tiingo",
) -> None:
    """Insert or replace a single OHLCV row."""
    with _adb.get_db() as db:
        db.execute(
            """
            INSERT OR REPLACE INTO ohlcv
                (ticker, date, open, high, low, close, adj_close, volume, universe, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ticker, date, o, h, l, c, adj, vol, universe, source),
        )


def get_ohlcv(
    ticker: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    """Return OHLCV data for *ticker* as a pandas DataFrame.

    Index is the parsed date column.  Compatible with existing strategy code.
    """
    import pandas as pd

    with _adb.get_db() as db:
        query = "SELECT * FROM ohlcv WHERE ticker=?"
        params: List[Any] = [ticker]
        if start_date:
            query += " AND date>=?"
            params.append(start_date)
        if end_date:
            query += " AND date<=?"
            params.append(end_date)
        query += " ORDER BY date"
        df = pd.read_sql_query(query, db, params=params, parse_dates=["date"])
        if not df.empty:
            df.set_index("date", inplace=True)
        return df


def get_universe_data(
    universe_name: str, start_date: Optional[str] = None
) -> Dict[str, Any]:
    """Return all OHLCV data for a universe as ``{ticker: DataFrame}``.

    For static ETF universes the ticker list is sourced from
    ``universe.definitions.get_universe_tickers()`` so that cross-universe
    tickers (e.g. GLD appears in both commodity_etfs and gold_etfs) are
    always returned for each universe regardless of which universe last
    wrote the SQLite row.
    """
    import pandas as pd

    # Prefer definitions-based ticker list for static universes
    try:
        from universe.definitions import get_universe  # type: ignore
        defn = get_universe(universe_name)
        if defn.get("method") == "static":
            from universe.definitions import get_universe_tickers
            tickers = get_universe_tickers(universe_name)
            if not tickers:
                return {}
            placeholders = ", ".join(["?"] * len(tickers))
            query = f"SELECT * FROM ohlcv WHERE ticker IN ({placeholders})"
            params: List[Any] = list(tickers)
            if start_date:
                query += " AND date >= ?"
                params.append(start_date)
            query += " ORDER BY ticker, date"
            with _adb.get_db() as db:
                df = pd.read_sql_query(query, db, params=params, parse_dates=["date"])
            result: Dict[str, Any] = {}
            if not df.empty:
                for ticker, group in df.groupby("ticker"):
                    result[ticker] = group.set_index("date")
            # Include empty DataFrames for tickers with no data
            for t in tickers:
                if t not in result:
                    result[t] = pd.DataFrame()
            return result
    except (KeyError, ImportError):
        pass

    # Fallback: query by universe column (sp500 and unknown universes)
    query = "SELECT * FROM ohlcv WHERE universe = ?"
    params_fb: List[Any] = [universe_name]
    if start_date:
        query += " AND date >= ?"
        params_fb.append(start_date)
    query += " ORDER BY ticker, date"
    with _adb.get_db() as db:
        df = pd.read_sql_query(query, db, params=params_fb, parse_dates=["date"])
    if df.empty:
        return {}
    result_fb: Dict[str, Any] = {}
    for ticker, group in df.groupby("ticker"):
        result_fb[ticker] = group.set_index("date")
    return result_fb
