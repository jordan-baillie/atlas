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
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
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
                sum(t.get("pnl") or 0 for t in wins)
                / sum(abs(t.get("pnl") or 0) for t in losses)
                if losses and any((t.get("pnl") or 0) < 0 for t in losses)
                else float("inf")
            ),
        }
    return result


# ── Trades ──────────────────────────────────────────────────────────────────

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
) -> None:
    """Insert a new open trade."""
    with get_db() as db:
        db.execute(
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


def record_trade_exit(
    ticker: str,
    strategy: str,
    exit_price: float,
    exit_reason: str,
    regime_at_exit: Optional[str] = None,
) -> None:
    """Close the most recent open trade for (ticker, strategy)."""
    with get_db() as db:
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
                datetime.now().isoformat(), exit_price, exit_reason, regime_at_exit,
                exit_price, exit_price, datetime.now().isoformat(),
                ticker, strategy,
            ),
        )


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
) -> List[Dict]:
    """Return closed trades with optional filters."""
    with get_db() as db:
        query = "SELECT * FROM trades WHERE status='closed'"
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
        query += " ORDER BY exit_date DESC"
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
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "profit_factor": (
            gross_profit / gross_loss if gross_loss else float("inf")
        ),
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


def get_regime_history(days: Optional[int] = None) -> List[Dict]:
    """Return regime history, optionally limited to recent *days*."""
    with get_db() as db:
        query = "SELECT * FROM regime_history"
        params: List[Any] = []
        if days:
            query += " WHERE date >= date('now', ?)"
            params.append(f"-{days} days")
        query += " ORDER BY date DESC"
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
    """
    with get_db() as db:
        tickers = [
            r[0]
            for r in db.execute(
                "SELECT DISTINCT ticker FROM ohlcv WHERE universe=?", (universe_name,)
            ).fetchall()
        ]
    return {t: get_ohlcv(t, start_date=start_date) for t in tickers}


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
        query += " ORDER BY timestamp DESC"
        rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            r = dict(row)
            if r.get("features"):
                r["features"] = json.loads(r["features"])
            result.append(r)
        return result


# ── Plans ────────────────────────────────────────────────────────────────────

def record_plan(
    date: str,
    market_id: str,
    plan_data: Dict,
    regime_state: Optional[str] = None,
    active_universes: Optional[List[str]] = None,
    sizing_multiplier: Optional[float] = None,
    overlay_applied: bool = False,
    overlay_adjustments: Optional[Dict] = None,
) -> int:
    """Insert a new plan. Returns the new plan id."""
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO plans
                (date, market_id, regime_state, active_universes, sizing_multiplier,
                 overlay_applied, overlay_adjustments, plan_data, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                date, market_id, regime_state,
                json.dumps(active_universes) if active_universes is not None else None,
                sizing_multiplier,
                1 if overlay_applied else 0,
                json.dumps(overlay_adjustments) if overlay_adjustments is not None else None,
                json.dumps(plan_data),
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
) -> None:
    """Upsert an equity curve data point."""
    with get_db() as db:
        db.execute(
            """
            INSERT OR REPLACE INTO equity_curve
                (date, market_id, equity, cash, positions_value, day_pnl, regime_state)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (date, market_id, equity, cash, positions_value, day_pnl, regime_state),
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
) -> None:
    """Insert a portfolio snapshot."""
    with get_db() as db:
        db.execute(
            """
            INSERT INTO portfolio_snapshots
                (timestamp, total_equity, cash, positions, exposure_by_universe,
                 exposure_by_sector, regime_state, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp, total_equity, cash,
                json.dumps(positions) if positions is not None else None,
                json.dumps(exposure_by_universe) if exposure_by_universe is not None else None,
                json.dumps(exposure_by_sector) if exposure_by_sector is not None else None,
                regime_state, source,
            ),
        )


def get_latest_snapshot() -> Optional[Dict]:
    """Return the most recent portfolio snapshot."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if row:
            return _decode_snapshot(dict(row))
        return None


def get_snapshots(days: Optional[int] = None) -> List[Dict]:
    """Return portfolio snapshots, most recent first."""
    with get_db() as db:
        query = "SELECT * FROM portfolio_snapshots"
        params: List[Any] = []
        if days:
            query += " WHERE timestamp >= datetime('now', ?)"
            params.append(f"-{days} days")
        query += " ORDER BY timestamp DESC"
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
) -> int:
    """Insert an overlay decision. Returns the new id."""
    with get_db() as db:
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


def get_overlay_decisions(days: Optional[int] = None) -> List[Dict]:
    """Return overlay decisions, most recent first."""
    with get_db() as db:
        query = "SELECT * FROM overlay_decisions"
        params: List[Any] = []
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
) -> None:
    """Insert or replace the best known parameters for (strategy, universe)."""
    with get_db() as db:
        db.execute(
            """
            INSERT OR REPLACE INTO research_best
                (strategy, universe, params, sharpe, trades, max_dd_pct, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (strategy, universe, json.dumps(params), sharpe, trades, max_dd_pct),
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
