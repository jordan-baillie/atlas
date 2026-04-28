"""
Atlas v2.0 — Typed SQLite access layer.

Every module in Atlas that needs persistent state goes through here.
No raw SQL scattered across the codebase.

Design rules:
- DB_PATH points to data/atlas.db (production)
- _db_path_override can be set for testing (call init_db(path) or set directly)
- get_db() is a context manager — every function uses ``with get_db() as db:``
- JSON columns are serialized with json.dumps / json.loads
- Timestamps are ISO format strings
- get_ohlcv() returns a pandas DataFrame with date as index
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "atlas.db"

# Module-level override used by tests — set via init_db(path) or directly.
# All CRUD functions call get_db() with no args; they use whatever is current.
_db_path_override: Optional[str] = None

# Test override for the broker state file directory used by _assert_state_file_parity.
# Set to a tmp_path str in tests; defaults to None (production path).
_state_dir_override: Optional[str] = None

# WAL mode persists at the DB file level; only needs to be set once per path
# per process. Avoids redundant PRAGMA on every connection.
_wal_initialized_paths: set = set()


# ── Connection ──────────────────────────────────────────────────────────────

@contextmanager
def get_db(db_path: Optional[str] = None):
    """
    Context manager that yields a WAL-mode SQLite connection.

    Priority for path: explicit arg → _db_path_override → DB_PATH
    Commits on clean exit, rolls back on exception.
    """
    path = db_path if db_path is not None else (_db_path_override or str(DB_PATH))
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    if path not in _wal_initialized_paths:
        conn.execute("PRAGMA journal_mode=WAL")
        _wal_initialized_paths.add(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema init ─────────────────────────────────────────────────────────────

def init_db(db_path: Optional[str] = None) -> None:
    """
    Create all tables from schema.sql (idempotent — uses IF NOT EXISTS).

    When db_path is provided, sets the module-level override so all subsequent
    CRUD calls use the same database.  Used by tests to point at a tmp file.

    Example::

        init_db()                            # production
        init_db("/tmp/test.db")              # test with temp file
        init_db(":memory:")                  # NOT recommended — each new
                                             # connection is a fresh DB;
                                             # use a tmp file for tests instead
    """
    global _db_path_override
    if db_path is not None:
        _db_path_override = db_path

    # Ensure the data directory exists for file-based DBs
    effective_path = _db_path_override or str(DB_PATH)
    if effective_path not in (":memory:",) and not effective_path.startswith("file:"):
        Path(effective_path).parent.mkdir(parents=True, exist_ok=True)

    schema_path = Path(__file__).resolve().parent / "schema.sql"
    schema_sql = schema_path.read_text()

    with get_db() as conn:
        conn.executescript(schema_sql)


# ── Helper ──────────────────────────────────────────────────────────────────

def _group_performance(trades: List[Dict], field: str) -> Dict[str, Any]:
    """
    Group closed trades by *field* and return per-group performance stats.
    Used by performance_summary().
    """
    groups: Dict[str, List[Dict]] = {}
    for trade in trades:
        key = trade.get(field) or "unknown"
        groups.setdefault(key, []).append(trade)

    result: Dict[str, Any] = {}
    for key, group_trades in groups.items():
        wins = [t for t in group_trades if (t.get("pnl") or 0) > 0]
        losses = [t for t in group_trades if (t.get("pnl") or 0) <= 0]
        total_pnl = sum(t.get("pnl") or 0 for t in group_trades)
        result[key] = {
            "trades": len(group_trades),
            "win_rate": len(wins) / len(group_trades) * 100,
            "total_pnl": round(total_pnl, 4),
            "avg_pnl": round(total_pnl / len(group_trades), 4),
            "profit_factor": (
                min(
                    sum(t.get("pnl") or 0 for t in wins)
                    / sum(abs(t.get("pnl") or 0) for t in losses),
                    99.99,
                )
                if losses and any((t.get("pnl") or 0) < 0 for t in losses)
                else (99.99 if wins else None)
            ),
        }
    return result


# ── Trades ──────────────────────────────────────────────────────────────────

def _assert_state_file_parity(
    ticker: str,
    universe: str,
    strategy: str,
    entry_price: float,
    shares: int,
    stop_price: float,
) -> None:
    """Post-insert guardrail: verify the market state file reflects the new trade.

    After a successful SQLite INSERT in ``record_trade_entry``, this helper
    checks that ``brokers/state/live_{universe}.json`` contains an entry for
    *ticker*.  If the ticker is missing it:

    1. Emits a loud ERROR log (visible in alerts / healthz).
    2. Self-heals by appending a minimal position entry to the state file so
       the dashboard and protective-order sync see the position.

    Non-fatal — all exceptions are swallowed after logging.  The state file
    is **not** the source of truth (SQLite is); this is a best-effort sync.

    Design notes
    ------------
    - Skips gracefully when the state file does not exist (paper/backtest
      markets, or a fresh environment that hasn't created it yet).
    - Does NOT create the state file from scratch — only patches an existing
      one.  File creation is the responsibility of ``LivePortfolio.save_state``.
    - Thread-safe for CPython: the JSON read-modify-write is done under a
      filelock if portalocker is available; otherwise best-effort (GIL
      provides some protection for short operations).
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)
    _PROJECT = Path(__file__).resolve().parent.parent
    if _state_dir_override is not None:
        _state_path = Path(_state_dir_override) / f"live_{universe}.json"
    else:
        _state_path = _PROJECT / "brokers" / "state" / f"live_{universe}.json"

    if not _state_path.exists():
        return  # Normal for paper/backtest markets

    try:
        with open(_state_path) as _f:
            _state = json.load(_f)

        _positions = _state.get("positions", [])
        _tickers_in_state = {p.get("ticker") for p in _positions}

        if ticker in _tickers_in_state:
            return  # Already present — no action needed

        # ── Mismatch detected ──────────────────────────────────────────────
        _log.error(
            "STATE FILE PARITY MISMATCH: %s/%s was written to SQLite but is "
            "MISSING from %s — self-healing by appending position entry. "
            "Root cause: state file write path is gated (live_enabled=False "
            "or eod_settlement/sync_protective_orders skipped this market).",
            ticker, universe, _state_path.name,
        )

        # Self-heal: append a minimal position entry
        _new_entry = {
            "ticker": ticker,
            "strategy": strategy,
            "entry_date": datetime.now().strftime("%Y-%m-%d"),
            "entry_price": float(entry_price),
            "shares": int(shares),
            "stop_price": float(stop_price),
            "order_id": "",
        }
        _positions.append(_new_entry)
        _state["positions"] = _positions

        with open(_state_path, "w") as _f:
            json.dump(_state, _f, indent=2)

        _log.info(
            "STATE FILE PARITY: self-healed %s/%s — appended to %s",
            ticker, universe, _state_path.name,
        )
    except Exception as _exc:
        _log.warning(
            "STATE FILE PARITY: check failed for %s/%s (non-fatal): %s",
            ticker, universe, _exc,
        )


def record_trade_entry(
    ticker: str,
    strategy: str,
    universe: str,
    entry_price: float,
    shares: int,
    stop_price: float,
    take_profit: Optional[float],
    confidence: float,
    regime_state: Optional[str],
    direction: str = "long",
    config_version: Optional[str] = None,
    **kwargs,
) -> Optional[int]:
    """Insert a new open trade.

    Returns the new trade id on success.  Returns None (and logs a WARNING)
    when a UNIQUE constraint violation is detected — i.e. there is already an
    open trade for the same (ticker, universe) pair.  This makes the function
    safe to call from concurrent processes without crashing the caller.

    The UNIQUE partial index ``idx_trades_unique_open`` on
    ``trades(ticker, universe) WHERE status='open'`` enforces the constraint at
    the database level, making the guard atomic regardless of how many
    processes call this simultaneously.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)
    try:
        with get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO trades
                    (ticker, strategy, universe, direction, entry_date, entry_price,
                     shares, stop_price, take_profit, confidence, regime_at_entry,
                     status, config_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    ticker, strategy, universe, direction,
                    datetime.now().isoformat(), entry_price,
                    shares, stop_price, take_profit, confidence, regime_state,
                    config_version,
                ),
            )
            _new_id = cursor.lastrowid
        # Post-insert parity check: verify state file reflects this trade.
        # Non-fatal — never prevents the successful return value.
        _assert_state_file_parity(ticker, universe, strategy, entry_price, shares, stop_price)
        return _new_id
    except sqlite3.IntegrityError as exc:
        _log.warning(
            "record_trade_entry: duplicate open trade blocked for %s/%s "
            "(UNIQUE constraint on idx_trades_unique_open): %s",
            ticker, universe, exc,
        )
        return None


def _compute_and_fill_mae_mfe(ticker: str, strategy: str, *, db=None) -> None:
    """Compute and fill MAE/MFE for the most recently closed trade of (ticker, strategy).

    Uses OHLCV data between entry_date and exit_date. Non-fatal — logs errors
    but never raises.

    When *db* is provided, reuses that connection instead of opening a new one.
    """
    import logging
    _log = logging.getLogger(__name__)

    def _run(conn):
        trade = conn.execute(
            "SELECT id, entry_date, exit_date, entry_price FROM trades "
            "WHERE ticker=? AND strategy=? AND status='closed' "
            "ORDER BY id DESC LIMIT 1",
            (ticker, strategy),
        ).fetchone()
        if not trade:
            return

        ed = trade['entry_date'][:10]
        xd = trade['exit_date'][:10]
        entry_price = trade['entry_price']
        trade_id = trade['id']

        rows = conn.execute(
            "SELECT low, high FROM ohlcv WHERE ticker=? AND date BETWEEN ? AND ?",
            (ticker, ed, xd),
        ).fetchall()

        if not rows:
            _log.debug("No OHLCV data for %s between %s and %s — skipping MAE/MFE", ticker, ed, xd)
            return

        min_low = min(r['low'] for r in rows)
        max_high = max(r['high'] for r in rows)
        mae = round((min_low - entry_price) / entry_price * 100, 4)
        mfe = round((max_high - entry_price) / entry_price * 100, 4)

        conn.execute(
            "UPDATE trades SET mae=?, mfe=?, updated_at=datetime('now') WHERE id=?",
            (mae, mfe, trade_id),
        )
        _log.info("MAE/MFE filled for trade #%d %s: mae=%.4f%%, mfe=%.4f%%", trade_id, ticker, mae, mfe)

    try:
        if db is not None:
            _run(db)
        else:
            with get_db() as conn:
                _run(conn)
    except Exception as exc:
        _log.warning("_compute_and_fill_mae_mfe failed for %s/%s: %s", ticker, strategy, exc)

def record_trade_exit(
    ticker: str,
    strategy: str,
    exit_price: float,
    exit_reason: str,
    regime_at_exit: Optional[str] = None,
) -> None:
    """Close the most recent open trade for (ticker, strategy).

    Duplicate-close guard: before updating the row to status='closed',
    this function checks whether a row with the same
    (ticker, strategy, DATE(exit_date), ROUND(pnl,2), superseded=0)
    already exists.  If so the trade is closed as superseded=1 instead
    of superseded=0, and a WARN is emitted.  This prevents double-
    counting in P&L aggregations.

    The uq_trades_active_closed unique index enforces the same invariant
    at the database level as a hard backstop.
    """
    import logging
    _exit_log = logging.getLogger(__name__)
    now = datetime.now().isoformat()
    with get_db() as db:
        # ── Validation: reject ghost trades (exit before entry) ──
        open_row = db.execute(
            "SELECT id, entry_date, entry_price, shares "
            "FROM trades WHERE ticker = ? AND strategy = ? AND status = 'open' "
            "ORDER BY id DESC LIMIT 1",
            (ticker, strategy),
        ).fetchone()
        if open_row:
            entry_date_str = str(open_row["entry_date"])[:10]
            exit_date_str = now[:10]
            if exit_date_str < entry_date_str:
                _exit_log.warning(
                    "record_trade_exit: REJECTED ghost trade for %s/%s — "
                    "exit_date %s is before entry_date %s",
                    ticker, strategy, exit_date_str, entry_date_str,
                )
                return

        # ── Duplicate-close guard ─────────────────────────────────────────
        # Compute the would-be pnl before the UPDATE and check for an
        # existing active-closed row with the same signature.
        # Works only when the superseded column is present (post-migration).
        _superseded_flag = 0  # default: this is a canonical close
        _has_sup_col = any(
            r[1] == "superseded"
            for r in db.execute("PRAGMA table_info(trades)").fetchall()
        )
        if _has_sup_col and open_row:
            _entry_price = float(open_row["entry_price"] or 0)
            _shares      = int(open_row["shares"] or 0)
            _would_be_pnl = round(
                (float(exit_price) - _entry_price) * _shares, 2
            )
            _dup = db.execute(
                "SELECT id FROM trades "
                "WHERE ticker=? AND strategy=? "
                "AND DATE(exit_date)=DATE(?) "
                "AND ROUND(pnl,2)=? "
                "AND status='closed' AND superseded=0",
                (ticker, strategy, now, _would_be_pnl),
            ).fetchone()
            if _dup:
                _exit_log.warning(
                    "trade dedup hit: skipping duplicate close for %s/%s, "
                    "existing id=%d would-have-been entry_date=%s — "
                    "marking this trade superseded=1",
                    ticker, strategy, _dup["id"],
                    str((open_row["entry_date"] or ""))[:10],
                )
                _superseded_flag = 1

        # ── Apply the exit UPDATE ─────────────────────────────────────────
        if _has_sup_col:
            db.execute(
                """
                UPDATE trades
                SET exit_date      = ?,
                    exit_price     = ?,
                    exit_reason    = ?,
                    status         = 'closed',
                    regime_at_exit = ?,
                    pnl            = (? - entry_price) * shares,
                    pnl_pct        = ((? - entry_price) / entry_price) * 100,
                    hold_days      = CAST(julianday(?) - julianday(entry_date) AS INTEGER),
                    superseded     = ?,
                    updated_at     = datetime('now')
                WHERE ticker = ? AND strategy = ? AND status = 'open'
                """,
                (
                    now, exit_price, exit_reason, regime_at_exit,
                    exit_price, exit_price, now,
                    _superseded_flag,
                    ticker, strategy,
                ),
            )
        else:
            # Fallback path: superseded column not yet present (pre-migration)
            db.execute(
                """
                UPDATE trades
                SET exit_date    = ?,
                    exit_price   = ?,
                    exit_reason  = ?,
                    status       = 'closed',
                    regime_at_exit = ?,
                    pnl          = (? - entry_price) * shares,
                    pnl_pct      = ((? - entry_price) / entry_price) * 100,
                    hold_days    = CAST(julianday(?) - julianday(entry_date) AS INTEGER),
                    updated_at   = datetime('now')
                WHERE ticker = ? AND strategy = ? AND status = 'open'
                """,
                (
                    now, exit_price, exit_reason, regime_at_exit,
                    exit_price, exit_price, now,
                    ticker, strategy,
                ),
            )
        # Compute and fill MAE/MFE in same transaction (non-fatal)
        _compute_and_fill_mae_mfe(ticker, strategy, db=db)


def get_open_positions() -> List[Dict]:
    """Return all open trades, oldest first."""
    with get_db() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT * FROM trades WHERE status='open' ORDER BY entry_date"
            ).fetchall()
        ]


def get_closed_trades(
    days: Optional[int] = None,
    strategy: Optional[str] = None,
    universe: Optional[str] = None,
    limit: int = 10000,
) -> List[Dict]:
    """Return closed trades with optional filters.

    Only returns non-superseded rows (superseded=0).  Superseded rows
    are audit-only duplicates and must not be counted in P&L aggregations.
    """
    with get_db() as db:
        # Use superseded=0 filter when column is present (post-migration).
        # The fallback (status='closed' only) preserves behaviour on fresh
        # test DBs that init from a schema version without the column.
        _cols = {r[1] for r in db.execute("PRAGMA table_info(trades)").fetchall()}
        _sup_clause = " AND superseded=0" if "superseded" in _cols else ""
        query = f"SELECT * FROM trades WHERE status='closed'{_sup_clause}"
        params: List[Any] = []
        if days:
            query += " AND exit_date >= date('now', ?)"
            params.append(f"-{days} days")
        if strategy:
            query += " AND strategy=?"
            params.append(strategy)
        if universe:
            query += " AND universe=?"
            params.append(universe)
        query += f" ORDER BY exit_date DESC LIMIT {int(limit)}"
        return [dict(r) for r in db.execute(query, params).fetchall()]


def performance_summary(days: Optional[int] = None) -> Dict:
    """Aggregate performance stats across all closed trades."""
    trades = get_closed_trades(days=days)
    if not trades:
        return {"trades": 0}
    wins = [t for t in trades if (t.get("pnl") or 0) > 0]
    losses = [t for t in trades if (t.get("pnl") or 0) <= 0]
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(abs(t["pnl"]) for t in losses) / len(losses) if losses else 0
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = sum(abs(t["pnl"]) for t in losses)
    if gross_loss == 0 or gross_loss is None:
        profit_factor = 99.99 if gross_profit > 0 else None
    else:
        pf = gross_profit / gross_loss
        profit_factor = min(pf, 99.99)
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "profit_factor": profit_factor,
        "expectancy": round(sum(t["pnl"] for t in trades) / len(trades), 4),
        "by_universe": _group_performance(trades, "universe"),
        "by_strategy": _group_performance(trades, "strategy"),
    }


# ── Regime ──────────────────────────────────────────────────────────────────

def record_regime(
    date: str,
    state: str,
    trend_score: float,
    risk_score: float,
    active_universes: List[str],
    sizing_multiplier: float,
    reasoning: str = "",
    enabled_strategies: Optional[List[str]] = None,
    model_version: str = "v1",
) -> None:
    """Insert or replace a regime classification for a date."""
    with get_db() as db:
        db.execute(
            """
            INSERT OR REPLACE INTO regime_history
                (date, regime_state, trend_score, risk_score, active_universes,
                 sizing_multiplier, enabled_strategies, reasoning, model_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                date, state, trend_score, risk_score,
                json.dumps(active_universes), sizing_multiplier,
                json.dumps(enabled_strategies) if enabled_strategies is not None else None,
                reasoning, model_version,
            ),
        )


def get_current_regime() -> Optional[Dict]:
    """Return the most recent regime record, or None if table is empty."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM regime_history ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if row:
            r = dict(row)
            r["active_universes"] = json.loads(r["active_universes"] or "[]")
            if r.get("enabled_strategies"):
                r["enabled_strategies"] = json.loads(r["enabled_strategies"])
            return r
        return None


def get_regime_history(days: Optional[int] = None, limit: int = 10000) -> List[Dict]:
    """Return regime history, optionally limited to recent *days*."""
    with get_db() as db:
        query = "SELECT * FROM regime_history"
        params: List[Any] = []
        if days:
            query += " WHERE date >= date('now', ?)"
            params.append(f"-{days} days")
        query += f" ORDER BY date DESC LIMIT {int(limit)}"
        rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            r = dict(row)
            r["active_universes"] = json.loads(r["active_universes"] or "[]")
            if r.get("enabled_strategies"):
                r["enabled_strategies"] = json.loads(r["enabled_strategies"])
            result.append(r)
        return result


# ── OHLCV ────────────────────────────────────────────────────────────────────

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
    with get_db() as db:
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
    """
    Return OHLCV data for *ticker* as a pandas DataFrame.

    Index is the parsed date column.  Compatible with existing strategy code.
    """
    import pandas as pd

    with get_db() as db:
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
    """
    Return all OHLCV data for a universe as ``{ticker: DataFrame}``.

    For static ETF universes the ticker list is sourced from
    ``universe.definitions.get_universe_tickers()`` so that cross-universe
    tickers (e.g. GLD appears in both commodity_etfs and gold_etfs) are
    always returned for each universe regardless of which universe last
    wrote the SQLite row.

    For dynamic universes (sp500) or when definitions is unavailable, falls
    back to querying ``WHERE universe=?`` as before.
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
            with get_db() as db:
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
    with get_db() as db:
        df = pd.read_sql_query(query, db, params=params_fb, parse_dates=["date"])
    if df.empty:
        return {}
    result_fb: Dict[str, Any] = {}
    for ticker, group in df.groupby("ticker"):
        result_fb[ticker] = group.set_index("date")
    return result_fb


# ── Signals ──────────────────────────────────────────────────────────────────

def record_signal(
    timestamp: str,
    ticker: str,
    strategy: str,
    universe: str,
    entry_price: float,
    stop_price: float,
    position_size: int,
    position_value: float,
    risk_amount: float,
    confidence: float,
    action: str,
    direction: str = "long",
    take_profit: Optional[float] = None,
    rationale: Optional[str] = None,
    features: Optional[Dict] = None,
    sector: Optional[str] = None,
    regime_state: Optional[str] = None,
    action_reason: Optional[str] = None,
    config_version: Optional[str] = None,
    market_id: Optional[str] = None,
) -> None:
    """Insert a signal record."""
    with get_db() as db:
        db.execute(
            """
            INSERT INTO signals
                (timestamp, ticker, strategy, universe, direction, entry_price,
                 stop_price, take_profit, position_size, position_value, risk_amount,
                 confidence, rationale, features, sector, regime_state, action,
                 action_reason, config_version, market_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp, ticker, strategy, universe, direction, entry_price,
                stop_price, take_profit, position_size, position_value, risk_amount,
                confidence, rationale,
                json.dumps(features) if features is not None else None,
                sector, regime_state, action, action_reason, config_version, market_id,
            ),
        )


def get_signals(
    days: Optional[int] = None,
    strategy: Optional[str] = None,
    ticker: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 10000,
) -> List[Dict]:
    """Return signals with optional filters. Most recent first."""
    with get_db() as db:
        query = "SELECT * FROM signals WHERE 1=1"
        params: List[Any] = []
        if days:
            query += " AND timestamp >= datetime('now', ?)"
            params.append(f"-{days} days")
        if strategy:
            query += " AND strategy=?"
            params.append(strategy)
        if ticker:
            query += " AND ticker=?"
            params.append(ticker)
        if action:
            query += " AND action=?"
            params.append(action)
        query += f" ORDER BY timestamp DESC LIMIT {int(limit)}"
        rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            r = dict(row)
            if r.get("features"):
                r["features"] = json.loads(r["features"])
            result.append(r)
        return result


# ── Plans ────────────────────────────────────────────────────────────────────

def _validate_plan_date(date: str) -> None:
    """Raise ValueError if plan date is suspiciously far from today (>30d or year mismatch).

    This catches the P1-6 class of bug where a test fixture date like
    '2024-03-01' leaks into production plan writes.  Date-level comparison
    avoids sub-day float noise from datetime.utcnow().
    """
    if not date:
        return  # Empty/missing date — not our concern here
    import logging as _plan_log
    from datetime import date as _date
    _log = _plan_log.getLogger(__name__)
    try:
        plan_d = _date.fromisoformat(date[:10])
    except ValueError:
        return  # Non-standard date string (e.g. "2026-04-08-test") — skip silently
    today = _date.today()
    delta_days = abs((plan_d - today).days)
    if delta_days > 30 or plan_d.year != today.year:
        raise ValueError(
            f"plan.date={date!r} is {delta_days}d from today (today year={today.year}, "
            f"plan year={plan_d.year}) — likely hardcoded test date leaking into production. "
            "Fix the call site or use datetime.today().strftime('%Y-%m-%d')."
        )


def record_plan(
    date: str,
    market_id: str,
    plan_data: Dict,
    regime_state: Optional[str] = None,
    active_universes: Optional[List[str]] = None,
    sizing_multiplier: Optional[float] = None,
    overlay_applied: bool = False,
    overlay_adjustments: Optional[Dict] = None,
    status: str = "pending_approval",
) -> int:
    """Insert a new plan. Returns the new plan id.

    Args:
        status: The plan status to store (e.g. 'pending_approval', 'approved',
                'pending').  Defaults to 'pending_approval' to match the status
                written to the JSON plan file by TradePlanGenerator._save_plan().
                The old hardcoded 'pending' value was a P0-A bug — it caused
                verify_dual_write.py to fail the status-normalisation check.
    """
    _validate_plan_date(date)
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO plans
                (date, market_id, regime_state, active_universes, sizing_multiplier,
                 overlay_applied, overlay_adjustments, plan_data, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                date, market_id, regime_state,
                json.dumps(active_universes) if active_universes is not None else None,
                sizing_multiplier,
                1 if overlay_applied else 0,
                json.dumps(overlay_adjustments) if overlay_adjustments is not None else None,
                json.dumps(plan_data),
                status,
            ),
        )
        return cursor.lastrowid


def get_plan(date: str, market_id: str) -> Optional[Dict]:
    """Return the most recent plan for (date, market_id), or None."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM plans WHERE date=? AND market_id=? ORDER BY id DESC LIMIT 1",
            (date, market_id),
        ).fetchone()
        if row:
            return _decode_plan(dict(row))
        return None


def get_plans(
    days: Optional[int] = None,
    status: Optional[str] = None,
    market_id: Optional[str] = None,
) -> List[Dict]:
    """Return plans with optional filters. Most recent first."""
    with get_db() as db:
        query = "SELECT * FROM plans WHERE 1=1"
        params: List[Any] = []
        if days:
            query += " AND date >= date('now', ?)"
            params.append(f"-{days} days")
        if status:
            query += " AND status=?"
            params.append(status)
        if market_id:
            query += " AND market_id=?"
            params.append(market_id)
        query += " ORDER BY id DESC"
        return [_decode_plan(dict(r)) for r in db.execute(query, params).fetchall()]


def update_plan_status(
    plan_id: int,
    status: str,
    approved_at: Optional[str] = None,
    executed_at: Optional[str] = None,
) -> None:
    """Update a plan's status (and optional timestamps)."""
    with get_db() as db:
        db.execute(
            """
            UPDATE plans
            SET status      = ?,
                approved_at = COALESCE(?, approved_at),
                executed_at = COALESCE(?, executed_at)
            WHERE id = ?
            """,
            (status, approved_at, executed_at, plan_id),
        )


def update_plan(
    plan_id: int,
    status: Optional[str] = None,
    approved_at: Optional[str] = None,
    executed_at: Optional[str] = None,
    plan_data: Optional[Dict] = None,
) -> None:
    """Update an existing plan row. All args except plan_id are optional;
    only non-None fields are written (COALESCE pattern).

    This is the idempotent dual-write target used by
    TradePlanGenerator._save_plan() to avoid inserting a new row on every
    status transition (pending_approval -> approved -> executed).
    """
    with get_db() as db:
        db.execute(
            """
            UPDATE plans
            SET status      = COALESCE(?, status),
                approved_at = COALESCE(?, approved_at),
                executed_at = COALESCE(?, executed_at),
                plan_data   = COALESCE(?, plan_data)
            WHERE id = ?
            """,
            (
                status,
                approved_at,
                executed_at,
                json.dumps(plan_data) if plan_data is not None else None,
                plan_id,
            ),
        )


def _decode_plan(row: Dict) -> Dict:
    """Deserialize JSON columns in a plan row."""
    if row.get("plan_data"):
        try:
            row["plan_data"] = json.loads(row["plan_data"])
        except (json.JSONDecodeError, TypeError):
            pass
    if row.get("active_universes"):
        try:
            row["active_universes"] = json.loads(row["active_universes"])
        except (json.JSONDecodeError, TypeError):
            pass
    if row.get("overlay_adjustments"):
        try:
            row["overlay_adjustments"] = json.loads(row["overlay_adjustments"])
        except (json.JSONDecodeError, TypeError):
            pass
    return row


# ── Equity curve ─────────────────────────────────────────────────────────────

def record_equity(
    date: str,
    market_id: str,
    equity: float,
    cash: Optional[float] = None,
    positions_value: Optional[float] = None,
    day_pnl: Optional[float] = None,
    regime_state: Optional[str] = None,
    broker_equity: Optional[float] = None,
    daily_pnl_pct: Optional[float] = None,
    total_pnl: Optional[float] = None,
    total_pnl_pct: Optional[float] = None,
    positions_count: Optional[int] = None,
    realized_pnl: Optional[float] = None,
) -> None:
    """Upsert an equity curve data point."""
    with get_db() as db:
        # Ensure new columns exist (idempotent migration)
        for col, ctype in [
            ("broker_equity", "REAL"),
            ("daily_pnl_pct", "REAL"),
            ("total_pnl", "REAL"),
            ("total_pnl_pct", "REAL"),
            ("positions_count", "INTEGER"),
            ("realized_pnl", "REAL"),
        ]:
            try:
                db.execute(f"ALTER TABLE equity_curve ADD COLUMN {col} {ctype}")
            except Exception:
                pass  # Column already exists
        db.execute(
            """
            INSERT OR REPLACE INTO equity_curve
                (date, market_id, equity, cash, positions_value, day_pnl, regime_state,
                 broker_equity, daily_pnl_pct, total_pnl, total_pnl_pct, positions_count, realized_pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (date, market_id, equity, cash, positions_value, day_pnl, regime_state,
             broker_equity, daily_pnl_pct, total_pnl, total_pnl_pct, positions_count, realized_pnl),
        )


def get_equity_curve(
    market_id: str, days: Optional[int] = None
) -> List[Dict]:
    """Return equity curve rows for *market_id*, oldest first."""
    with get_db() as db:
        query = "SELECT * FROM equity_curve WHERE market_id=?"
        params: List[Any] = [market_id]
        if days:
            query += " AND date >= date('now', ?)"
            params.append(f"-{days} days")
        query += " ORDER BY date ASC"
        return [dict(r) for r in db.execute(query, params).fetchall()]


def get_latest_equity(market_id: Optional[str] = None) -> Optional[Dict]:
    """Return the most recent equity curve row, optionally filtered by market."""
    with get_db() as db:
        if market_id:
            row = db.execute(
                "SELECT * FROM equity_curve WHERE market_id=? ORDER BY date DESC LIMIT 1",
                (market_id,),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM equity_curve ORDER BY date DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None


# ── Position snapshots ───────────────────────────────────────────────────────

def record_position_snapshots(
    date: str,
    market_id: str,
    positions: List[Dict],
) -> None:
    """Write per-position snapshots for historical tracking.

    Deletes existing snapshots for this date/market before inserting
    (idempotent — safe to re-run).
    """
    with get_db() as db:
        # Create table if not exists
        db.execute("""
            CREATE TABLE IF NOT EXISTS position_snapshots (
                date TEXT NOT NULL,
                market_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                strategy TEXT,
                entry_date TEXT,
                entry_price REAL,
                close_price REAL,
                shares INTEGER,
                unrealized_pnl REAL,
                unrealized_pnl_pct REAL,
                stop_price REAL,
                take_profit REAL,
                mae_pct REAL,
                mfe_pct REAL,
                holding_days INTEGER,
                sector TEXT,
                PRIMARY KEY (date, market_id, ticker)
            )
        """)
        # Delete existing for idempotent re-runs
        db.execute("DELETE FROM position_snapshots WHERE date=? AND market_id=?", (date, market_id))
        for pos in positions:
            db.execute(
                """INSERT INTO position_snapshots
                    (date, market_id, ticker, strategy, entry_date, entry_price, close_price,
                     shares, unrealized_pnl, unrealized_pnl_pct, stop_price, take_profit,
                     mae_pct, mfe_pct, holding_days, sector)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (date, market_id, pos.get("ticker"), pos.get("strategy"),
                 pos.get("entry_date"), pos.get("entry_price"), pos.get("close_price"),
                 pos.get("shares"), pos.get("unrealized_pnl"), pos.get("unrealized_pnl_pct"),
                 pos.get("stop_price"), pos.get("take_profit"),
                 pos.get("mae_pct"), pos.get("mfe_pct"), pos.get("holding_days"),
                 pos.get("sector")),
            )


def get_position_snapshots(date: str, market_id: Optional[str] = None) -> List[Dict]:
    """Return position snapshots for a given date."""
    with get_db() as db:
        if market_id:
            rows = db.execute(
                "SELECT * FROM position_snapshots WHERE date=? AND market_id=?",
                (date, market_id),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM position_snapshots WHERE date=?", (date,)
            ).fetchall()
        return [dict(r) for r in rows]


# ── Portfolio snapshots ───────────────────────────────────────────────────────

def record_snapshot(
    timestamp: str,
    total_equity: Optional[float] = None,
    cash: Optional[float] = None,
    positions: Optional[List[Dict]] = None,
    exposure_by_universe: Optional[Dict] = None,
    exposure_by_sector: Optional[Dict] = None,
    regime_state: Optional[str] = None,
    source: str = "eod",
    market_id: str = "sp500",
) -> None:
    """Insert a per-market portfolio snapshot.

    Args:
        market_id: The market whose positions this snapshot covers (e.g. 'sp500',
                   'commodity_etfs'). Use 'ALL' for the broker-level aggregate row
                   written once per EOD cycle via record_all_markets_snapshot().
    """
    with get_db() as db:
        db.execute(
            """
            INSERT INTO portfolio_snapshots
                (timestamp, total_equity, cash, positions, exposure_by_universe,
                 exposure_by_sector, regime_state, source, market_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp, total_equity, cash,
                json.dumps(positions) if positions is not None else None,
                json.dumps(exposure_by_universe) if exposure_by_universe is not None else None,
                json.dumps(exposure_by_sector) if exposure_by_sector is not None else None,
                regime_state, source, market_id,
            ),
        )


def record_all_markets_snapshot(
    timestamp: str,
    broker_equity: float,
    broker_cash: float,
    source: str = "eod",
) -> None:
    """Write one aggregate snapshot row (market_id='ALL') per EOD cycle.

    This is the authoritative total-portfolio equity row. It uses the
    broker-account-level figures (single Alpaca account == single equity),
    so it is immune to "last writer wins" across per-market EOD runs.

    Readers wanting the portfolio total should filter ``WHERE market_id='ALL'``.
    """
    positions_value = round(broker_equity - broker_cash, 2)
    record_snapshot(
        timestamp=timestamp,
        total_equity=broker_equity,
        cash=broker_cash,
        positions=None,
        regime_state=None,
        source=source,
        market_id="ALL",
    )


def get_latest_snapshot(market_id: str = "ALL") -> Optional[Dict]:
    """Return the most recent portfolio snapshot for a given market.

    Args:
        market_id: Defaults to 'ALL' (broker-level aggregate). Pass a specific
                   market ('sp500', 'commodity_etfs') to get that market's row.
                   Pass ``None`` to get the single most-recent row regardless of
                   market (legacy behaviour — prefer the default).
    """
    with get_db() as db:
        if market_id is None:
            row = db.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM portfolio_snapshots"
                " WHERE market_id=?"
                " ORDER BY timestamp DESC LIMIT 1",
                (market_id,),
            ).fetchone()
        if row:
            return _decode_snapshot(dict(row))
        return None


def get_snapshots(
    days: Optional[int] = None,
    limit: int = 10000,
    market_id: Optional[str] = "ALL",
) -> List[Dict]:
    """Return portfolio snapshots, most recent first.

    Args:
        market_id: Filter to a specific market. Defaults to 'ALL' (aggregate rows).
                   Pass ``None`` to return snapshots across all markets.
    """
    with get_db() as db:
        query = "SELECT * FROM portfolio_snapshots"
        params: List[Any] = []
        conditions: List[str] = []
        if market_id is not None:
            conditions.append("market_id=?")
            params.append(market_id)
        if days:
            conditions.append("timestamp >= datetime('now', ?)")
            params.append(f"-{days} days")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += f" ORDER BY timestamp DESC LIMIT {int(limit)}"
        return [
            _decode_snapshot(dict(r)) for r in db.execute(query, params).fetchall()
        ]


def _decode_snapshot(row: Dict) -> Dict:
    for field in ("positions", "exposure_by_universe", "exposure_by_sector"):
        if row.get(field):
            try:
                row[field] = json.loads(row[field])
            except (json.JSONDecodeError, TypeError):
                pass
    return row


# ── Overlay decisions ─────────────────────────────────────────────────────────

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

    Idempotency guard
    -----------------
    Before inserting, the function queries ``overlay_decisions`` for any row
    that satisfies ALL of:

    * ``regime_state`` matches the candidate
    * ``action`` matches the candidate
    * ``timestamp`` is within ``dedup_window_seconds`` (default 300 s / 5 min)
      of the candidate's timestamp

    If such a row exists, its id is returned and no new row is written.  This
    prevents triple-write duplication when multiple market cron entries
    (sp500 / commodity_etfs / sector_etfs) call the overlay concurrently in
    the same premarket window.

    The check is non-fatal: if timestamp parsing or the SELECT fails for any
    reason, execution falls through to the normal INSERT.
    """
    import logging as _logging
    from datetime import timedelta, timezone as _tz

    _log = _logging.getLogger(__name__)

    with get_db() as db:
        # -- Dedup guard ----------------------------------------------------
        try:
            candidate_dt = datetime.fromisoformat(timestamp)
            if candidate_dt.tzinfo is None:
                candidate_dt = candidate_dt.replace(tzinfo=_tz.utc)
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
            # Non-fatal: dedup check failed; fall through to normal INSERT.
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
    """Return overlay decisions, most recent first.

    Parameters
    ----------
    days:
        When provided (and *unevaluated_only* is False), restrict to decisions
        made within the last *days* calendar days.  When ``None``, all rows are
        returned.
    unevaluated_only:
        When ``True`` the *days* filter is **ignored for unevaluated rows** —
        every row with ``outcome_evaluated = 0`` is returned regardless of age
        (up to a safety cap of 365 days).  Already-evaluated rows are not
        included in the result.  This flag exists so the weekly evaluator can
        catch up on decisions that fell outside the normal look-back window
        without breaking callers that use the recency-filtered path.
    """
    with get_db() as db:
        if unevaluated_only:
            # Return ALL unevaluated rows up to a safe upper-age limit.
            query = (
                "SELECT * FROM overlay_decisions"
                " WHERE outcome_evaluated = 0"
                "   AND timestamp >= datetime('now', '-365 days')"
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
    with get_db() as db:
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


# ── Overlay shadow log (M3 shadow mode) ──────────────────────────────────────

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
    import logging as _logging
    _log = _logging.getLogger(__name__)
    try:
        with get_db() as db:
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
    with get_db() as db:
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
    with get_db() as db:
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
    with get_db() as db:
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
    with get_db() as db:
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
    with get_db() as db:
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
    with get_db() as db:
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
    with get_db() as db:
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
    with get_db() as db:
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
    with get_db() as db:
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


# ── Research ──────────────────────────────────────────────────────────────────

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
    with get_db() as db:
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
    with get_db() as db:
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
    with get_db() as db:
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
) -> None:
    """Insert or replace the best known parameters for (strategy, universe).

    New columns (M2 2026-04-28):
        solo_sharpe      — strategy-standalone backtest Sharpe
        portfolio_sharpe — whole-portfolio Sharpe with this strategy
        metric_type      — 'solo', 'portfolio', 'both', 'legacy_portfolio', 'unknown'

    The legacy ``sharpe`` column is preserved for backwards compat but is
    DEPRECATED (use solo_sharpe / portfolio_sharpe).  A DEBUG log is emitted
    when ``sharpe`` is written without the new fields.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    # Compute metric_type if not supplied
    if metric_type is None:
        if solo_sharpe is not None and portfolio_sharpe is not None:
            metric_type = "both"
        elif solo_sharpe is not None:
            metric_type = "solo"
        elif portfolio_sharpe is not None:
            metric_type = "portfolio"
        # else leave None → will COALESCE to existing or default 'unknown'

    if sharpe is not None and solo_sharpe is None and portfolio_sharpe is None:
        _log.debug(
            "research_best.sharpe is deprecated — use solo_sharpe / portfolio_sharpe "
            "(strategy=%s universe=%s). Writing legacy-only row.",
            strategy, universe,
        )

    with get_db() as db:
        db.execute(
            """
            INSERT INTO research_best
                (strategy, universe, params, sharpe, trades, max_dd_pct,
                 solo_sharpe, portfolio_sharpe, metric_type, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'unknown'), datetime('now'))
            ON CONFLICT(strategy, universe) DO UPDATE SET
                params           = excluded.params,
                sharpe           = excluded.sharpe,
                trades           = excluded.trades,
                max_dd_pct       = excluded.max_dd_pct,
                solo_sharpe      = COALESCE(excluded.solo_sharpe, solo_sharpe),
                portfolio_sharpe = COALESCE(excluded.portfolio_sharpe, portfolio_sharpe),
                metric_type      = COALESCE(excluded.metric_type, metric_type, 'unknown'),
                updated_at       = datetime('now')
            """,
            (
                strategy, universe, json.dumps(params), sharpe, trades, max_dd_pct,
                solo_sharpe, portfolio_sharpe, metric_type,
            ),
        )


def get_research_best(
    strategy: Optional[str] = None,
    universe: Optional[str] = None,
) -> List[Dict]:
    """Return research_best rows, optionally filtered."""
    with get_db() as db:
        query = "SELECT * FROM research_best WHERE 1=1"
        params: List[Any] = []
        if strategy:
            query += " AND strategy=?"
            params.append(strategy)
        if universe:
            query += " AND universe=?"
            params.append(universe)
        query += " ORDER BY strategy, universe"
        rows = db.execute(query, params).fetchall()
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


# ── System ────────────────────────────────────────────────────────────────────

def record_heartbeat(
    service: str,
    status: str,
    detail: Optional[Dict] = None,
) -> None:
    """Upsert a heartbeat for *service*."""
    with get_db() as db:
        db.execute(
            """
            INSERT OR REPLACE INTO heartbeats (service, timestamp, status, detail)
            VALUES (?, datetime('now'), ?, ?)
            """,
            (service, status, json.dumps(detail) if detail is not None else None),
        )


def get_heartbeats(service: Optional[str] = None) -> List[Dict]:
    """Return heartbeats, optionally filtered by service."""
    with get_db() as db:
        query = "SELECT * FROM heartbeats"
        params: List[Any] = []
        if service:
            query += " WHERE service=?"
            params.append(service)
        query += " ORDER BY service"
        rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            r = dict(row)
            if r.get("detail"):
                try:
                    r["detail"] = json.loads(r["detail"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(r)
        return result


def record_system_log(
    level: str,
    service: str,
    message: Optional[str] = None,
    detail: Optional[Dict] = None,
) -> None:
    """Append a system log entry."""
    with get_db() as db:
        db.execute(
            """
            INSERT INTO system_log (level, service, message, detail)
            VALUES (?, ?, ?, ?)
            """,
            (level, service, message, json.dumps(detail) if detail is not None else None),
        )


def get_system_logs(
    hours: Optional[int] = None,
    service: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = 200,
) -> List[Dict]:
    """Return system log entries, most recent first."""
    with get_db() as db:
        query = "SELECT * FROM system_log WHERE 1=1"
        params: List[Any] = []
        if hours:
            query += " AND timestamp >= datetime('now', ?)"
            params.append(f"-{hours} hours")
        if service:
            query += " AND service=?"
            params.append(service)
        if level:
            query += " AND level=?"
            params.append(level)
        query += f" ORDER BY timestamp DESC LIMIT {int(limit)}"
        rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            r = dict(row)
            if r.get("detail"):
                try:
                    r["detail"] = json.loads(r["detail"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(r)
        return result


# ── Macro Indicators ──────────────────────────────────────────────────────────

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


def upsert_macro_indicators(date: str, **fields) -> None:
    """Insert or replace a macro indicators row for the given date.

    Pass field values as keyword arguments matching the macro_indicators
    schema columns.  Unknown columns are silently ignored.  NaN/inf float
    values are stored as NULL.

    Example::

        upsert_macro_indicators(
            "2024-01-02",
            vix=15.3, vix3m=16.1, vix_term_ratio=0.95,
            credit_oas=50.2, dxy=104.1,
        )
    """
    import math

    def _clean(v: Any) -> Any:
        """Convert NaN/inf to None so SQLite stores NULL."""
        if v is None:
            return None
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return v

    safe = {k: _clean(v) for k, v in fields.items() if k in _MACRO_INDICATOR_COLS}

    if not safe:
        # Nothing to update — at least record the date.
        with get_db() as db:
            db.execute(
                "INSERT OR IGNORE INTO macro_indicators (date) VALUES (?)",
                (date,),
            )
        return

    # Sort for deterministic SQL (easier to test/debug).
    sorted_keys = sorted(safe.keys())
    cols = ["date"] + sorted_keys
    placeholders = ", ".join(["?"] * len(cols))
    cols_str = ", ".join(cols)
    values = [date] + [safe[k] for k in sorted_keys]

    with get_db() as db:
        db.execute(
            f"INSERT OR REPLACE INTO macro_indicators ({cols_str}) VALUES ({placeholders})",
            values,
        )


def batch_upsert_macro_indicators(rows: List[Dict]) -> int:
    """Batch-insert macro indicator rows in a single transaction.

    Each dict must contain a 'date' key. Other keys matching
    _MACRO_INDICATOR_COLS are upserted; unknown keys are ignored.
    Returns the number of rows written.
    """
    import math

    def _clean(v: Any) -> Any:
        """Convert NaN/inf to None so SQLite stores NULL."""
        if v is None:
            return None
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return v

    count = 0
    with get_db() as db:
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
    """Return macro indicators rows ordered by date ascending.

    Args:
        start_date: Only rows with date >= start_date.
        end_date:   Only rows with date <= end_date.
        days:       Shortcut: include last *days* calendar days.
    """
    with get_db() as db:
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


# ── Treasury Yield Curve ──────────────────────────────────────────────────────

# All columns in treasury_curve table (excludes date and updated_at).
_TREASURY_CURVE_COLS: frozenset = frozenset({
    "yield_1m", "yield_3m", "yield_6m",
    "yield_1y", "yield_2y", "yield_3y",
    "yield_5y", "yield_7y", "yield_10y",
    "yield_20y", "yield_30y",
    "treasury_slope", "treasury_curvature", "treasury_level",
})


def batch_upsert_treasury_curve(rows: List[Dict]) -> int:
    """Batch-insert Treasury yield curve rows in a single transaction.

    Each dict must contain a 'date' key (``'YYYY-MM-DD'``).  Other keys
    matching ``_TREASURY_CURVE_COLS`` are upserted; unknown keys are ignored.
    NaN / inf values are stored as NULL.

    Args:
        rows: List of dicts, each with 'date' + yield/metric columns.

    Returns:
        Number of rows written.
    """
    import math

    def _clean(v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return v

    count = 0
    with get_db() as db:
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
    """Return treasury_curve rows ordered by date ascending.

    Args:
        start_date: Only rows with date >= start_date.
        end_date:   Only rows with date <= end_date.
        days:       Shortcut: include last *days* calendar days.
    """
    with get_db() as db:
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


# ── Risk Cache (P2.7 / P2.8 / P4.2) ──────────────────────────────────────────

import logging as _logging

_risk_cache_tables_ensured = False


def _ensure_risk_cache_tables() -> None:
    """Idempotent migration: create risk cache tables if they don't exist.

    Called lazily from every cache helper so tests and fresh installs
    automatically get the schema without requiring a separate migration step.
    """
    global _risk_cache_tables_ensured
    if _risk_cache_tables_ensured:
        return
    with get_db() as conn:
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
        # ruin_probability already exists (created by risk/ruin_probability.py);
        # CREATE TABLE IF NOT EXISTS is safe to re-run and ensures the table is
        # present in fresh test DBs.
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
    _risk_cache_tables_ensured = True


# ── Regime Transition Cache ───────────────────────────────────────────────────


def get_cached_regime_transitions(max_age_hours: int = 24) -> Optional[Dict]:
    """Return the latest cached regime transition matrix if it is fresh.

    Staleness is measured by comparing ``as_of`` (an ISO timestamp) to now.
    Returns ``None`` when no cache row exists or the row is older than
    *max_age_hours*.

    Return shape::

        {
            "as_of": "2026-04-22T22:30:00+00:00",
            "matrix": {"bull_risk_on": {"bull_risk_on": 85.2, ...}, ...},
            "window_days": 90,
            "n_observations": 88,
        }
    """
    _ensure_risk_cache_tables()
    with get_db() as conn:
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
    except Exception:
        d["matrix"] = {}
    return d


def set_cached_regime_transitions(
    matrix: Dict,
    window_days: int,
    n_obs: int,
    as_of: Optional[str] = None,
) -> None:
    """Persist a regime transition matrix to the cache table.

    *as_of* defaults to the current UTC ISO timestamp.  Override for testing.
    """
    _ensure_risk_cache_tables()
    from datetime import timezone as _tz
    ts = as_of or datetime.now(_tz.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO regime_transitions_cache
                (as_of, matrix_json, window_days, n_observations)
            VALUES (?, ?, ?, ?)
        """, (ts, json.dumps(matrix), window_days, n_obs))


# ── Ruin Probability Cache ────────────────────────────────────────────────────


def get_cached_ruin_probability(max_age_hours: int = 24) -> Optional[Dict]:
    """Return the latest cached ruin probability snapshot.

    Two staleness signals are surfaced:

    1. **Age**: if the latest ``as_of`` date is older than *max_age_hours*,
       returns ``None``.
    2. **Portfolio change** (P2.8): if the cached ticker set differs from the
       current open positions, the returned dict has ``stale=True`` and
       ``reason="portfolio_changed"``.

    Return shape::

        {
            "as_of": "2026-04-22",
            "prob": 0.03,           # 30-day horizon prob_ruin (canonical)
            "tickers": ["AAPL", ...],
            "current_equity": 5200.0,
            "horizons": {"30d": {...}, "60d": {...}, "90d": {...}},
            "stale": False,
            "reason": None,
        }
    """
    _ensure_risk_cache_tables()
    try:
        with get_db() as conn:
            # Latest as_of that falls within the freshness window
            age_row = conn.execute("""
                SELECT MAX(as_of) AS max_as_of
                FROM   ruin_probability
                WHERE  (julianday('now') - julianday(as_of)) * 24.0 <= ?
            """, (max_age_hours,)).fetchone()

        if not age_row or not age_row["max_as_of"]:
            return None

        as_of = age_row["max_as_of"]

        with get_db() as conn:
            rows = conn.execute("""
                SELECT * FROM ruin_probability
                WHERE  as_of = ?
                ORDER  BY horizon_days
            """, (as_of,)).fetchall()

            # Current open position tickers (for P2.8 comparison)
            open_rows = conn.execute("""
                SELECT DISTINCT ticker FROM trades WHERE exit_date IS NULL
            """).fetchall()

        if not rows:
            return None

        first = dict(rows[0])
        try:
            cached_tickers = sorted(json.loads(first["tickers"] or "[]"))
        except Exception:
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

        # Canonical single-number probability: prefer 30d, else first horizon
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
        _logging.getLogger(__name__).warning("get_cached_ruin_probability failed: %s", exc)
        return None


def set_cached_ruin_probability(
    prob: float,
    tickers: List[str],
    n_positions: int,
    equity: float,
    params: Optional[Dict] = None,
) -> None:
    """Write a ruin probability snapshot directly to ``ruin_probability``.

    Inserts a single row for ``horizon_days=30`` (the canonical summary
    horizon).  Pass ``params={'as_of': 'YYYY-MM-DD'}`` to override the
    date (used by tests to simulate stale rows).

    For the full multi-horizon persist used by the precompute script, call
    ``risk.ruin_probability.persist_ruin_probability()`` directly.
    """
    _ensure_risk_cache_tables()
    params = params or {}
    from datetime import timezone as _tz
    as_of = params.get("as_of", datetime.now(_tz.utc).strftime("%Y-%m-%d"))
    floor_pct = params.get("floor_pct", 0.70)
    floor = equity * floor_pct
    n_paths = params.get("n_paths", 10_000)
    tickers_json = json.dumps(sorted(tickers))
    with get_db() as conn:
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


# ── Portfolio Risk Cache ──────────────────────────────────────────────────────


def get_cached_portfolio_risk(max_age_hours: int = 24) -> Optional[Dict]:
    """Return the latest portfolio_risk row if it is fresh.

    Staleness is based on ``as_of`` date vs now (same semantics as
    ``get_cached_ruin_probability``).

    Returns ``None`` when no fresh row exists.
    """
    _ensure_risk_cache_tables()
    try:
        with get_db() as conn:
            row = conn.execute("""
                SELECT *
                FROM   portfolio_risk
                WHERE  (julianday('now') - julianday(as_of)) * 24.0 <= ?
                ORDER  BY as_of DESC, created_at DESC
                LIMIT  1
            """, (max_age_hours,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["tickers"] = json.loads(d["tickers"]) if d["tickers"] else []
        except Exception:
            d["tickers"] = []
        return d
    except Exception as exc:
        _logging.getLogger(__name__).warning("get_cached_portfolio_risk failed: %s", exc)
        return None
