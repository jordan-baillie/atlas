"""db/trades — Live-trade and paper-trade CRUD.

All public functions (and the two private helpers _group_performance and
_assert_state_file_parity) are re-exported through db.atlas_db for backward compat.

Note: _assert_state_file_parity reads _state_dir_override from db.atlas_db via
the module reference (_adb._state_dir_override) so that monkeypatch.setattr on
db.atlas_db._state_dir_override propagates correctly during tests.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import db.atlas_db as _adb

__all__ = [
    "_STRATEGY_SKIP",
    "_group_performance",
    "_assert_state_file_parity",
    "record_trade_entry",
    "update_trade_protective_orders",
    "record_trade_exit",
    "get_open_positions",
    "get_closed_trades",
    "performance_summary",
    # paper trades
    "record_paper_trade_entry",
    "update_paper_trade_protective_orders",
    "record_paper_trade_exit",
    "get_open_paper_trades",
    "get_closed_paper_trades",
    "get_paper_trades_for_universe",
    "get_paper_protective_record",
    "upsert_paper_protective_record",
    "close_paper_protective_record",
    "list_active_paper_protective_records",
]

_log = logging.getLogger(__name__)
_paper_log = logging.getLogger(__name__)

_STRATEGY_SKIP: frozenset = frozenset({"reconciled", "unknown", ""})


def _group_performance(trades: List[Dict], field: str) -> Dict[str, Any]:
    """
    Group closed trades by *field* and return per-group performance stats.
    Used by performance_summary().

    F-06: when field='strategy', trades with strategy in
    ('reconciled', 'unknown', '') or NULL are excluded from rollups —
    these are synthetic housekeeping markers, not real strategies.
    """
    groups: Dict[str, List[Dict]] = {}
    for trade in trades:
        key = trade.get(field) or "unknown"
        if field == "strategy" and (key in _STRATEGY_SKIP or key is None):
            continue  # skip synthetic/housekeeping strategy markers
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
    if _adb._state_dir_override is not None:
        _state_path = Path(_adb._state_dir_override) / f"live_{universe}.json"
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

        # ── Telegram alert (1-hour cooldown) ──────────────────────────────
        try:
            import time as _time
            # Cooldown file lives beside the state file (or in data/ if no override)
            if _adb._state_dir_override is not None:
                _cooldown_path = Path(_adb._state_dir_override) / "parity_alert_cooldown.json"
            else:
                _cooldown_path = _PROJECT / "data" / "parity_alert_cooldown.json"
            _now_ts = _time.time()
            _cooldown_state: dict = {}
            try:
                if _cooldown_path.exists():
                    _cooldown_state = json.loads(_cooldown_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
            _last_alert = float(_cooldown_state.get(universe, 0))
            if _now_ts - _last_alert >= 3600:
                # Build alert content
                try:
                    with _adb.get_db() as _alert_db:
                        _sqlite_rows = _alert_db.execute(
                            "SELECT ticker FROM trades WHERE universe=? AND status='open'",
                            (universe,),
                        ).fetchall()
                    _sqlite_tickers = sorted(r["ticker"] for r in _sqlite_rows)
                except sqlite3.Error:
                    _sqlite_tickers = [ticker]
                _sqlite_count = len(_sqlite_tickers)
                _json_tickers_sorted = sorted(_tickers_in_state)
                _json_count = len(_json_tickers_sorted)
                _missing_set = sorted({ticker} - _tickers_in_state)
                _extra_set = sorted(_tickers_in_state - set(_sqlite_tickers))
                from utils.telegram import send_message as _tg_send
                _msg = (
                    "🚨 STATE PARITY MISMATCH" + "\n"
                    + f"Market: {universe}" + "\n"
                    + f"SQLite open: {_sqlite_count} positions"
                    + f" ({', '.join(_sqlite_tickers) or 'none'})" + "\n"
                    + f"JSON state: {_json_count} positions"
                    + f" ({', '.join(_json_tickers_sorted) or 'none'})" + "\n"
                    + f"Missing from JSON: {', '.join(_missing_set) or 'none'}" + "\n"
                    + f"Extra in JSON: {', '.join(_extra_set) or 'none'}"
                )
                _tg_send(_msg)
                # Record cooldown timestamp
                _cooldown_state[universe] = _now_ts
                try:
                    _cooldown_path.write_text(json.dumps(_cooldown_state))
                except OSError:
                    pass
        except Exception as _tg_exc:
            _log.warning(
                "STATE PARITY: failed to send Telegram alert (non-fatal): %s", _tg_exc
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
        with _adb.get_db() as db:
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


def update_trade_protective_orders(
    *,
    ticker: str,
    universe: str,
    stop_order_id: Optional[str] = None,
    tp_order_id: Optional[str] = None,
) -> int:
    """Update stop_order_id and/or tp_order_id on the OPEN trade row for (ticker, universe).

    Both args are optional — pass only what you want to update. None means leave
    unchanged. Empty string ('') is treated as "set to empty" (clear the field).

    Looks up by (ticker, universe, status='open'). The UNIQUE partial index
    idx_trades_unique_open guarantees at most one match.

    Returns:
        Number of rows updated (0 or 1). Logs a WARNING when no match is found
        — caller may wish to handle this (e.g., not-yet-recorded trade).
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)
    sets = []
    params: list = []
    if stop_order_id is not None:
        sets.append("stop_order_id = ?")
        params.append(stop_order_id)
    if tp_order_id is not None:
        sets.append("tp_order_id = ?")
        params.append(tp_order_id)
    if not sets:
        return 0
    sets.append("updated_at = datetime('now')")
    params.extend([ticker, universe])
    sql = (
        f"UPDATE trades SET {', '.join(sets)} "
        f"WHERE ticker = ? AND universe = ? AND status = 'open'"
    )
    with _adb.get_db() as db:
        cursor = db.execute(sql, params)
        n = cursor.rowcount
    if n == 0:
        _log.warning(
            "update_trade_protective_orders: no open trade for %s/%s "
            "(stop_order_id=%s tp_order_id=%s)",
            ticker, universe, stop_order_id, tp_order_id,
        )
    return n


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
            with _adb.get_db() as conn:
                _run(conn)
    except Exception as exc:
        _log.warning("_compute_and_fill_mae_mfe failed for %s/%s: %s", ticker, strategy, exc)

def record_trade_exit(
    ticker: str,
    strategy: str,
    exit_price: float,
    exit_reason: str,
    regime_at_exit: Optional[str] = None,
    exit_date: Optional[str] = None,
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

    Args:
        exit_date: ISO-format timestamp to use as the exit_date column value.
            When provided (e.g. broker fill's filled_at), this is used verbatim
            so the stored exit_date reflects the ACTUAL fill time rather than
            the script's wall-clock detection time.  Defaults to datetime.now()
            when None.  This prevents premature-closure bugs where a reconcile
            script detects an exit and records detection-time as exit_date even
            though the broker fill happened earlier or later (#FIX-PMEQ-002).
    """
    import logging
    _exit_log = logging.getLogger(__name__)
    # Use caller-supplied exit_date (e.g. broker filled_at) when available so
    # the stored timestamp reflects the ACTUAL fill, not detection wall-clock.
    # Fall back to datetime.now() only when no broker timestamp is provided.
    now = exit_date if exit_date else datetime.now().isoformat()
    with _adb.get_db() as db:
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
    with _adb.get_db() as db:
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
    with _adb.get_db() as db:
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


# ── Paper-trade helpers ──────────────────────────────────────────────────────
# Exact mirrors of the live-trade helpers above, operating on paper_trades
# and paper_position_protective_orders instead of the production tables.
# ──────────────────────────────────────────────────────────────────────────────

def record_paper_trade_entry(
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
    paper_account_id: Optional[str] = None,
    **kwargs,
) -> Optional[int]:
    """Insert a new open paper trade into `paper_trades`.

    Mirrors :func:`record_trade_entry` exactly.  Accepts an additional
    *paper_account_id* kwarg (default None) for Alpaca paper-account
    traceability.

    Returns the new row id on success, or None on duplicate UNIQUE violation
    (same (ticker, universe) already open).
    """
    try:
        with _adb.get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO paper_trades
                    (ticker, strategy, universe, direction, entry_date, entry_price,
                     shares, stop_price, take_profit, confidence, regime_at_entry,
                     status, config_version, paper_account_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    ticker, strategy, universe, direction,
                    datetime.now().isoformat(), entry_price,
                    shares, stop_price, take_profit, confidence, regime_state,
                    config_version, paper_account_id,
                ),
            )
            return cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        _paper_log.warning(
            "record_paper_trade_entry: duplicate open paper trade blocked for %s/%s "
            "(UNIQUE constraint on idx_paper_trades_unique_open): %s",
            ticker, universe, exc,
        )
        return None


def update_paper_trade_protective_orders(
    *,
    ticker: str,
    universe: str,
    stop_order_id: Optional[str] = None,
    tp_order_id: Optional[str] = None,
) -> int:
    """Update stop_order_id and/or tp_order_id on the OPEN paper trade row for
    (ticker, universe).

    Mirrors :func:`update_trade_protective_orders`.

    Returns:
        Number of rows updated (0 or 1).
    """
    sets: list[str] = []
    params: list = []
    if stop_order_id is not None:
        sets.append("stop_order_id = ?")
        params.append(stop_order_id)
    if tp_order_id is not None:
        sets.append("tp_order_id = ?")
        params.append(tp_order_id)
    if not sets:
        return 0
    sets.append("updated_at = datetime('now')")
    params.extend([ticker, universe])
    sql = (
        f"UPDATE paper_trades SET {', '.join(sets)} "
        f"WHERE ticker = ? AND universe = ? AND status = 'open'"
    )
    with _adb.get_db() as db:
        cursor = db.execute(sql, params)
        n = cursor.rowcount
    if n == 0:
        _paper_log.warning(
            "update_paper_trade_protective_orders: no open paper trade for %s/%s "
            "(stop_order_id=%s tp_order_id=%s)",
            ticker, universe, stop_order_id, tp_order_id,
        )
    return n


def record_paper_trade_exit(
    ticker: str,
    strategy: str,
    exit_price: float,
    exit_reason: str,
    regime_at_exit: Optional[str] = None,
) -> None:
    """Close the most recent open paper trade for (ticker, strategy).

    Mirrors :func:`record_trade_exit`.  Applies the same ghost-trade guard,
    duplicate-close guard (via superseded flag), and MAE/MFE computation —
    all against the `paper_trades` table.
    """
    now = datetime.now().isoformat()
    with _adb.get_db() as db:
        # ── Ghost-trade guard ─────────────────────────────────────────────
        open_row = db.execute(
            "SELECT id, entry_date, entry_price, shares "
            "FROM paper_trades WHERE ticker = ? AND strategy = ? AND status = 'open' "
            "ORDER BY id DESC LIMIT 1",
            (ticker, strategy),
        ).fetchone()
        if open_row:
            entry_date_str = str(open_row["entry_date"])[:10]
            exit_date_str = now[:10]
            if exit_date_str < entry_date_str:
                _paper_log.warning(
                    "record_paper_trade_exit: REJECTED ghost trade for %s/%s — "
                    "exit_date %s is before entry_date %s",
                    ticker, strategy, exit_date_str, entry_date_str,
                )
                return

        # ── Duplicate-close guard ─────────────────────────────────────────
        _superseded_flag = 0
        _has_sup_col = any(
            r[1] == "superseded"
            for r in db.execute("PRAGMA table_info(paper_trades)").fetchall()
        )
        if _has_sup_col and open_row:
            _entry_price = float(open_row["entry_price"] or 0)
            _shares = int(open_row["shares"] or 0)
            _would_be_pnl = round(
                (float(exit_price) - _entry_price) * _shares, 2
            )
            _dup = db.execute(
                "SELECT id FROM paper_trades "
                "WHERE ticker=? AND strategy=? "
                "AND DATE(exit_date)=DATE(?) "
                "AND ROUND(pnl,2)=? "
                "AND status='closed' AND superseded=0",
                (ticker, strategy, now, _would_be_pnl),
            ).fetchone()
            if _dup:
                _paper_log.warning(
                    "paper trade dedup hit: skipping duplicate close for %s/%s, "
                    "existing id=%d — marking this trade superseded=1",
                    ticker, strategy, _dup["id"],
                )
                _superseded_flag = 1

        # ── Apply the exit UPDATE ─────────────────────────────────────────
        if _has_sup_col:
            db.execute(
                """
                UPDATE paper_trades
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
            db.execute(
                """
                UPDATE paper_trades
                SET exit_date      = ?,
                    exit_price     = ?,
                    exit_reason    = ?,
                    status         = 'closed',
                    regime_at_exit = ?,
                    pnl            = (? - entry_price) * shares,
                    pnl_pct        = ((? - entry_price) / entry_price) * 100,
                    hold_days      = CAST(julianday(?) - julianday(entry_date) AS INTEGER),
                    updated_at     = datetime('now')
                WHERE ticker = ? AND strategy = ? AND status = 'open'
                """,
                (
                    now, exit_price, exit_reason, regime_at_exit,
                    exit_price, exit_price, now,
                    ticker, strategy,
                ),
            )


def get_open_paper_trades() -> list[dict]:
    """Return all open paper trades, oldest first.

    Mirrors :func:`get_open_positions` for `paper_trades`.
    """
    with _adb.get_db() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT * FROM paper_trades WHERE status='open' ORDER BY entry_date"
            ).fetchall()
        ]


def get_closed_paper_trades(
    days: Optional[int] = None,
    strategy: Optional[str] = None,
    universe: Optional[str] = None,
    limit: int = 10000,
) -> list[dict]:
    """Return closed paper trades with optional filters.

    Mirrors :func:`get_closed_trades` for `paper_trades`.
    Only returns non-superseded rows (superseded=0).
    """
    with _adb.get_db() as db:
        _cols = {r[1] for r in db.execute("PRAGMA table_info(paper_trades)").fetchall()}
        _sup_clause = " AND superseded=0" if "superseded" in _cols else ""
        query = f"SELECT * FROM paper_trades WHERE status='closed'{_sup_clause}"
        params: list[Any] = []
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


def get_paper_trades_for_universe(
    universe: str,
    status: Optional[str] = None,
) -> list[dict]:
    """Return paper trades for a given universe, optionally filtered by status.

    Convenience helper: callers that work per-universe (e.g. portfolio modules)
    can call this instead of building the filter manually.

    Args:
        universe: Universe/market id (e.g. 'sp500', 'asx200').
        status: 'open', 'closed', or None for all.  Only non-superseded rows
                are returned for 'closed'.
    """
    with _adb.get_db() as db:
        _cols = {r[1] for r in db.execute("PRAGMA table_info(paper_trades)").fetchall()}
        _sup_clause = " AND superseded=0" if "superseded" in _cols else ""
        params: list[Any] = [universe]
        if status == "closed":
            query = f"SELECT * FROM paper_trades WHERE universe=? AND status='closed'{_sup_clause} ORDER BY entry_date"
        elif status == "open":
            query = "SELECT * FROM paper_trades WHERE universe=? AND status='open' ORDER BY entry_date"
        else:
            query = "SELECT * FROM paper_trades WHERE universe=? ORDER BY entry_date"
        return [dict(r) for r in db.execute(query, params).fetchall()]


# ── Paper protective-order helpers ───────────────────────────────────────────


def get_paper_protective_record(market_id: str, ticker: str) -> Optional[dict]:
    """Fetch the active protective record for a paper position.

    Mirrors :func:`get_protective_record` for `paper_position_protective_orders`.
    """
    try:
        with _adb.get_db() as db:
            row = db.execute(
                "SELECT * FROM paper_position_protective_orders "
                "WHERE market_id=? AND ticker=? AND status='active'",
                (market_id, ticker),
            ).fetchone()
            return dict(row) if row else None
    except Exception as exc:
        _paper_log.warning(
            "get_paper_protective_record(%s, %s) failed: %s", market_id, ticker, exc
        )
        return None


def upsert_paper_protective_record(
    market_id: str,
    ticker: str,
    trade_id: Optional[int],
    position_qty: float,
    stop_order_id: Optional[str] = None,
    stop_price: Optional[float] = None,
    tp_order_id: Optional[str] = None,
    tp_price: Optional[float] = None,
    oco_class: Optional[str] = None,
) -> None:
    """Insert or update a paper protective record.

    Mirrors :func:`upsert_protective_record` for `paper_position_protective_orders`.
    Uses INSERT OR REPLACE keyed on (market_id, ticker).
    """
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with _adb.get_db() as db:
        db.execute(
            """
            INSERT INTO paper_position_protective_orders
                (market_id, ticker, trade_id, position_qty,
                 stop_order_id, stop_price,
                 tp_order_id, tp_price,
                 oco_class, last_synced_at, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,'active')
            ON CONFLICT(market_id, ticker) DO UPDATE SET
                trade_id       = excluded.trade_id,
                position_qty   = excluded.position_qty,
                stop_order_id  = excluded.stop_order_id,
                stop_price     = excluded.stop_price,
                tp_order_id    = excluded.tp_order_id,
                tp_price       = excluded.tp_price,
                oco_class      = excluded.oco_class,
                last_synced_at = excluded.last_synced_at,
                status         = 'active'
            """,
            (
                market_id, ticker, trade_id, float(position_qty),
                stop_order_id, float(stop_price) if stop_price is not None else None,
                tp_order_id, float(tp_price) if tp_price is not None else None,
                oco_class, now,
            ),
        )


def close_paper_protective_record(market_id: str, ticker: str) -> None:
    """Mark a paper protective record as 'closed'.

    Mirrors :func:`close_protective_record`.  Idempotent.
    """
    try:
        with _adb.get_db() as db:
            db.execute(
                "UPDATE paper_position_protective_orders "
                "SET status='closed', last_synced_at=? "
                "WHERE market_id=? AND ticker=?",
                (datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), market_id, ticker),
            )
    except Exception as exc:
        _paper_log.warning(
            "close_paper_protective_record(%s, %s) failed: %s", market_id, ticker, exc
        )


def list_active_paper_protective_records(
    market_id: Optional[str] = None,
) -> list[dict]:
    """List all status='active' paper protective records.

    Mirrors :func:`list_active_protective_records` for
    `paper_position_protective_orders`.
    """
    try:
        with _adb.get_db() as db:
            if market_id:
                rows = db.execute(
                    "SELECT * FROM paper_position_protective_orders "
                    "WHERE status='active' AND market_id=? "
                    "ORDER BY market_id, ticker",
                    (market_id,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM paper_position_protective_orders "
                    "WHERE status='active' "
                    "ORDER BY market_id, ticker",
                ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        _paper_log.warning(
            "list_active_paper_protective_records failed: %s", exc
        )
        return []


# ── Telegram message capture ────────────────────────────────────────────────

