"""core/reconcile.py — Canonical reconciliation module for Atlas.

Two primary functions:

  reconcile_fills(market_id, broker, db, dry_run=True) -> ReconcileReport
      Syncs broker order history → broker_orders table → SQLite trades.
      Direction: broker → broker_orders → SQLite (one direction only).
      Idempotent. dry_run=True makes no DB changes.

  reconcile_positions(market_id, broker, db, dry_run=True) -> ReconcileReport
      Compares live broker positions vs SQLite open trades for a market.
      REPORTS ONLY in this version — no auto-fix (Phase B.3+).
      Idempotent. Safe to run repeatedly.

Shadow mode: scripts/reconcile_shadow.py runs both functions in dry_run mode
alongside existing scripts and compares results via Telegram alert.

Cutover plan: after 7-day shadow validation with zero divergence, the existing
reconcile scripts (reconcile_ledger.py, reconcile_positions.py,
reconcile_sqlite_to_broker.py) will be deleted and this module becomes the
sole reconcile path.  See docs/reconcile.md for the full cutover procedure.

Phase B.2 — 2026-04-29
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Project root ──────────────────────────────────────────────────────────────
_PROJECT = Path(__file__).resolve().parent.parent

# Markets Atlas currently tracks
_ALL_MARKETS: tuple[str, ...] = ("sp500", "commodity_etfs", "sector_etfs", "asx")

# Days of broker order history to pull per reconcile_fills call
_HISTORY_DAYS: int = 30


# ═══════════════════════════════════════════════════════════════════════════════
# Report dataclass
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ReconcileReport:
    """Structured result from a single reconcile call.

    fields:
        fills_added     — broker_orders rows that are new to our local DB
        fills_updated   — broker_orders rows whose fill data changed (e.g. newly FILLED)
        trades_opened   — trade IDs opened from new BUY fills (0 = dry_run placeholder)
        trades_closed   — trade IDs closed from new SELL fills (0 = dry_run placeholder)
        drift_detected  — position-level discrepancies (type/ticker/details dicts)
        errors          — non-fatal error strings collected during the run
        dry_run         — True if no DB writes were made
    """
    market_id: str
    timestamp: str  # ISO UTC
    fills_added: List[Dict[str, Any]] = field(default_factory=list)
    fills_updated: List[Dict[str, Any]] = field(default_factory=list)
    trades_opened: List[int] = field(default_factory=list)
    trades_closed: List[int] = field(default_factory=list)
    drift_detected: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    dry_run: bool = True

    @property
    def changed(self) -> bool:
        """True if any fills or trade records would change (or did change)."""
        return bool(
            self.fills_added or self.fills_updated
            or self.trades_opened or self.trades_closed
        )

    def summary(self) -> dict[str, Any]:
        """Compact summary dict for logging and shadow persistence."""
        return {
            "market_id": self.market_id,
            "timestamp": self.timestamp,
            "fills_added": len(self.fills_added),
            "fills_updated": len(self.fills_updated),
            "trades_opened": len(self.trades_opened),
            "trades_closed": len(self.trades_closed),
            "drift_detected": len(self.drift_detected),
            "errors": len(self.errors),
            "dry_run": self.dry_run,
            "changed": self.changed,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_market_tickers(market_id: str) -> set[str]:
    """Return all tickers in scope for *market_id*.

    Union of:
      1. Universe definition tickers (builder first, definitions fallback)
      2. Tickers in brokers/state/live_{market_id}.json

    Returns empty set on complete failure; caller should warn and skip.
    """
    tickers: set[str] = set()

    # 1a. Universe builder (handles dynamic sp500 + all static ETF universes)
    try:
        from universe.builder import get_universe_tickers
        tickers |= set(get_universe_tickers(market_id))
    except Exception as exc:
        logger.debug("_get_market_tickers: builder failed for %s: %s", market_id, exc)

    # 1b. Static definitions fallback (for ETF universes not in builder)
    if not tickers:
        try:
            from universe.definitions import get_universe_tickers as _def
            tickers |= set(_def(market_id))
        except Exception as exc:
            logger.debug("_get_market_tickers: definitions failed for %s: %s", market_id, exc)

    # 2. Always augment from state file — catches positions outside the current universe
    state_path = _PROJECT / "brokers" / "state" / f"live_{market_id}.json"
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text())
            for pos in data.get("positions", []) or []:
                if pos.get("ticker"):
                    tickers.add(pos["ticker"])
        except Exception as exc:
            logger.warning("_get_market_tickers: could not read state for %s: %s", market_id, exc)

    if not tickers:
        logger.warning("_get_market_tickers: no tickers found for %s", market_id)

    return tickers


def _get_other_market_tickers(market_id: str) -> set[str]:
    """Return tickers actively managed by markets OTHER than *market_id*.

    Used to exclude cross-market positions from this market's position scope
    so we don't flag GLD as a BROKER_ORPHAN when running sp500 reconcile.
    """
    tickers: set[str] = set()
    for m in _ALL_MARKETS:
        if m == market_id:
            continue
        state_path = _PROJECT / "brokers" / "state" / f"live_{m}.json"
        if not state_path.exists():
            continue
        try:
            data = json.loads(state_path.read_text())
            for pos in data.get("positions", []) or []:
                if pos.get("ticker"):
                    tickers.add(pos["ticker"])
        except Exception as exc:
            logger.debug("_get_other_market_tickers: could not read %s: %s", m, exc)
    return tickers


def _order_status_str(order) -> str:
    s = order.status
    return str(s.value).lower() if hasattr(s, "value") else str(s).lower()


def _order_side_str(order) -> str:
    s = order.side
    return str(s.value).lower() if hasattr(s, "value") else str(s).lower()


def _is_filled(order) -> bool:
    return _order_status_str(order) == "filled"


def _is_buy(order) -> bool:
    return _order_side_str(order) == "buy"


def _is_sell(order) -> bool:
    return _order_side_str(order) == "sell"


def _derive_exit_reason(order) -> str:
    """Infer exit reason from order type (stop/limit/market)."""
    raw = getattr(order, "raw", {}) or {}
    order_type = str(raw.get("order_type") or raw.get("type") or "").lower()
    if "stop" in order_type:
        return "stop_fill"
    if "limit" in order_type:
        return "take_profit"
    if "market" in order_type:
        return "market_exit"
    return "reconcile_fill"


def _order_to_broker_row(order, synced_at: str) -> dict[str, Any]:
    """Convert an OrderResult to a broker_orders INSERT/UPSERT row dict."""
    raw = getattr(order, "raw", {}) or {}
    status_str = _order_status_str(order)
    side_str = _order_side_str(order)

    def _clean(v: Any) -> Optional[str]:
        """Return None for null-like values from Alpaca SDK (None strings, etc.)."""
        if v is None:
            return None
        sv = str(v)
        return None if sv.lower() in ("none", "null", "") else sv

    raw_json = raw if isinstance(raw, str) else json.dumps(raw)
    fill_price_raw = order.fill_price
    filled_qty_raw = order.filled_qty

    return {
        "order_id": order.order_id,
        "symbol": order.ticker,
        "side": side_str,
        "qty": float(order.requested_qty or 0),
        "filled_qty": float(filled_qty_raw) if filled_qty_raw else None,
        "fill_price": float(fill_price_raw) if fill_price_raw else None,
        "status": status_str,
        "submitted_at": _clean(raw.get("submitted_at")) or synced_at,
        "filled_at": _clean(raw.get("filled_at")),
        "order_class": _clean(raw.get("order_class")),
        "parent_id": _clean(raw.get("parent_id")),
        "raw_alpaca_json": raw_json,
        "last_synced_at": synced_at,
    }


def _upsert_broker_order_row(db_module, row: dict[str, Any]) -> None:
    """Upsert a single broker_orders row.  Non-fatal — logs on error."""
    try:
        with db_module.get_db() as conn:
            conn.execute(
                """INSERT INTO broker_orders
                   (order_id, symbol, side, qty, filled_qty, fill_price, status,
                    submitted_at, filled_at, order_class, parent_id,
                    raw_alpaca_json, last_synced_at)
                   VALUES (:order_id, :symbol, :side, :qty, :filled_qty, :fill_price,
                    :status, :submitted_at, :filled_at, :order_class, :parent_id,
                    :raw_alpaca_json, :last_synced_at)
                   ON CONFLICT(order_id) DO UPDATE SET
                     filled_qty      = excluded.filled_qty,
                     fill_price      = excluded.fill_price,
                     status          = excluded.status,
                     filled_at       = excluded.filled_at,
                     raw_alpaca_json = excluded.raw_alpaca_json,
                     last_synced_at  = excluded.last_synced_at
                """,
                row,
            )
    except Exception as exc:
        logger.warning(
            "_upsert_broker_order_row failed for order_id=%s: %s",
            row.get("order_id"), exc,
        )


def _load_existing_broker_orders(db_module, tickers: set[str]) -> dict[str, dict[str, Any]]:
    """Batch-load broker_orders rows for *tickers*. Returns order_id → row dict.

    Returns empty dict if broker_orders table is missing or on any error.
    """
    if not tickers:
        return {}
    try:
        ph = ",".join("?" * len(tickers))
        with db_module.get_db() as conn:
            rows = conn.execute(
                f"SELECT * FROM broker_orders WHERE symbol IN ({ph})",
                tuple(tickers),
            ).fetchall()
        return {row["order_id"]: dict(row) for row in rows}
    except Exception as exc:
        logger.debug("_load_existing_broker_orders: %s (broker_orders may not exist)", exc)
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════

def reconcile_fills(
    market_id: str,
    broker,
    db,
    dry_run: bool = True,
) -> ReconcileReport:
    """Sync broker fill history → broker_orders → SQLite trades.

    Algorithm:
      1. Pull broker order history for the last 30 days
      2. Filter to market_id tickers (universe ∪ state file)
      3. Determine fills_added (new to local DB) and fills_updated (fill data changed)
      4. For newly FILLED BUY orders with no open SQLite trade: record_trade_entry
         (strategy='reconcile_fill'; stop/TP null — requires follow-up by sync_protective)
      5. For newly FILLED SELL orders with an open SQLite trade: record_trade_exit

    OCO bracket children: only FILLED orders trigger trade actions; CANCELLED legs
    are ignored automatically (status != 'filled').

    Partial fills: handled via upsert — same order_id updated from partial → full fill;
    fills_updated captures the status transition; trade is created on full fill only.

    Cross-market tickers: filtered by market_tickers (universe + state file).
    Tickers from OTHER markets' state files can still appear in fills but will be
    naturally excluded if they're not in this market's universe/state.

    Args:
        market_id: Market to reconcile ('sp500', 'commodity_etfs', etc.)
        broker:    Connected broker; must support get_history_orders(days: int)
        db:        atlas_db module providing get_db(), get_open_positions(),
                   record_trade_entry(), record_trade_exit()
        dry_run:   True → report what would change; make NO DB writes

    Returns:
        ReconcileReport with fills_added, fills_updated, trades_opened, trades_closed
    """
    ts = _now_utc()
    report = ReconcileReport(market_id=market_id, timestamp=ts, dry_run=dry_run)

    # ── 1. Market tickers ─────────────────────────────────────────────────────
    market_tickers = _get_market_tickers(market_id)
    if not market_tickers:
        msg = f"No tickers found for market {market_id!r} — cannot scope fills"
        report.errors.append(msg)
        logger.error("reconcile_fills: %s", msg)
        return report

    # ── 2. Broker order history ───────────────────────────────────────────────
    try:
        broker_orders_raw = broker.get_history_orders(days=_HISTORY_DAYS)
    except Exception as exc:
        report.errors.append(f"broker.get_history_orders failed: {exc}")
        logger.error("reconcile_fills [%s]: broker error: %s", market_id, exc)
        return report

    market_orders = [o for o in (broker_orders_raw or []) if o.ticker in market_tickers]
    logger.info(
        "reconcile_fills [%s]: broker returned %d orders, %d in market scope (dry_run=%s)",
        market_id, len(broker_orders_raw or []), len(market_orders), dry_run,
    )

    if not market_orders:
        logger.info("reconcile_fills [%s]: no in-scope orders — clean", market_id)
        return report

    # ── 3. Load existing broker_orders from local DB ──────────────────────────
    in_scope_tickers = {o.ticker for o in market_orders}
    existing_db: dict[str, dict] = _load_existing_broker_orders(db, in_scope_tickers)
    synced_at = ts

    newly_filled_buys: list = []
    newly_filled_sells: list = []

    for order in market_orders:
        if not order.order_id:
            continue
        existing = existing_db.get(order.order_id)
        row = _order_to_broker_row(order, synced_at)
        status = _order_status_str(order)
        side = _order_side_str(order)

        if existing is None:
            # Brand-new order — not yet in our local broker_orders table
            report.fills_added.append({
                "order_id": order.order_id,
                "ticker": order.ticker,
                "side": side,
                "status": status,
                "fill_price": row.get("fill_price"),
                "filled_qty": row.get("filled_qty"),
            })
            if _is_filled(order):
                if _is_buy(order):
                    newly_filled_buys.append(order)
                elif _is_sell(order):
                    newly_filled_sells.append(order)
            if not dry_run:
                _upsert_broker_order_row(db, row)

        else:
            # Order already in DB — check if fill data is new (partial → full fill)
            was_filled = (
                existing.get("fill_price") is not None
                and float(existing.get("fill_price", 0) or 0) > 0
            )
            now_filled = row.get("fill_price") is not None and float(row.get("fill_price") or 0) > 0
            status_changed = existing.get("status") != row.get("status")

            if (not was_filled and now_filled) or status_changed:
                report.fills_updated.append({
                    "order_id": order.order_id,
                    "ticker": order.ticker,
                    "side": side,
                    "old_status": existing.get("status"),
                    "new_status": status,
                    "fill_price": row.get("fill_price"),
                })
                # Only act on orders that just became FILLED
                if _is_filled(order) and not was_filled:
                    if _is_buy(order):
                        newly_filled_buys.append(order)
                    elif _is_sell(order):
                        newly_filled_sells.append(order)
                if not dry_run:
                    _upsert_broker_order_row(db, row)

    # ── 4. Load open SQLite trades for this market ────────────────────────────
    try:
        all_open = db.get_open_positions()
    except Exception as exc:
        report.errors.append(f"db.get_open_positions failed: {exc}")
        logger.error("reconcile_fills [%s]: DB error: %s", market_id, exc)
        return report

    open_by_ticker: dict[str, dict] = {
        t["ticker"]: t
        for t in all_open
        if t.get("universe") == market_id
    }

    # ── 5. trades_opened — newly FILLED BUY orders with no open SQLite trade ─
    for order in newly_filled_buys:
        ticker = order.ticker

        if ticker in open_by_ticker:
            logger.debug(
                "reconcile_fills: BUY fill %s — open trade already exists (id=%s), skip",
                ticker, open_by_ticker[ticker].get("id"),
            )
            continue

        fill_price = float(order.fill_price or 0)
        filled_qty = int(order.filled_qty or order.requested_qty or 0)

        try:
            from universe.membership import derive_universe
            universe = derive_universe(ticker, market_id) or market_id
        except Exception:
            universe = market_id

        if dry_run:
            report.trades_opened.append(0)
            logger.info(
                "reconcile_fills [dry_run] would open trade: %s %d@%.4f universe=%s",
                ticker, filled_qty, fill_price, universe,
            )
        else:
            try:
                trade_id = db.record_trade_entry(
                    ticker=ticker,
                    strategy="reconcile_fill",
                    universe=universe,
                    entry_price=fill_price,
                    shares=filled_qty,
                    stop_price=None,
                    take_profit=None,
                    confidence=0.0,
                    regime_state=None,
                    direction="long",
                )
                if trade_id is not None:
                    report.trades_opened.append(trade_id)
                    logger.info(
                        "reconcile_fills: opened trade #%d: %s %d@%.4f universe=%s",
                        trade_id, ticker, filled_qty, fill_price, universe,
                    )
                else:
                    logger.warning(
                        "reconcile_fills: record_trade_entry returned None for %s "
                        "(UNIQUE constraint — open trade already exists from concurrent run)",
                        ticker,
                    )
            except Exception as exc:
                report.errors.append(f"record_trade_entry {ticker}: {exc}")
                logger.error("reconcile_fills: record_trade_entry failed for %s: %s", ticker, exc)

    # ── 6. trades_closed — newly FILLED SELL orders with an open SQLite trade ─
    for order in newly_filled_sells:
        ticker = order.ticker

        if ticker not in open_by_ticker:
            logger.debug(
                "reconcile_fills: SELL fill %s — no open trade for market=%s, skip",
                ticker, market_id,
            )
            continue

        existing_trade = open_by_ticker[ticker]
        exit_reason = _derive_exit_reason(order)
        fill_price = float(order.fill_price or 0)

        if dry_run:
            report.trades_closed.append(0)
            logger.info(
                "reconcile_fills [dry_run] would close trade #%d: %s @%.4f reason=%s",
                existing_trade.get("id", -1), ticker, fill_price, exit_reason,
            )
        else:
            try:
                db.record_trade_exit(
                    ticker=ticker,
                    strategy=existing_trade.get("strategy", "reconcile_fill"),
                    exit_price=fill_price,
                    exit_reason=exit_reason,
                    regime_at_exit=None,
                )
                report.trades_closed.append(existing_trade.get("id", 0))
                logger.info(
                    "reconcile_fills: closed trade #%d: %s @%.4f reason=%s",
                    existing_trade.get("id", -1), ticker, fill_price, exit_reason,
                )
            except Exception as exc:
                report.errors.append(f"record_trade_exit {ticker}: {exc}")
                logger.error("reconcile_fills: record_trade_exit failed for %s: %s", ticker, exc)

    logger.info(
        "reconcile_fills [%s] complete: fills_added=%d fills_updated=%d "
        "trades_opened=%d trades_closed=%d errors=%d",
        market_id,
        len(report.fills_added), len(report.fills_updated),
        len(report.trades_opened), len(report.trades_closed),
        len(report.errors),
    )
    return report


def reconcile_positions(
    market_id: str,
    broker,
    db,
    dry_run: bool = True,
) -> ReconcileReport:
    """Compare live broker positions vs SQLite open trades for *market_id*.

    Detects three drift types:
      BROKER_ORPHAN  — broker holds a position, SQLite has no open trade for it
      SQLITE_ORPHAN  — SQLite has an open trade, broker holds no matching position
                       (the "MU class" — position disappeared from broker without
                        a recorded exit; most commonly caused by stop/TP fills that
                        were not picked up by live_executor)
      QTY_DRIFT      — both have the position but share quantities differ

    Phase B.3+ will add auto-fix capability once 7-day shadow mode confirms
    detection accuracy.  For now this function is REPORT-ONLY.

    Cross-market exclusion: tickers actively managed by other markets are excluded
    from BROKER_ORPHAN detection (prevents false alarms for cross-held tickers).

    Args:
        market_id: Market to reconcile
        broker:    Connected broker; must support get_positions()
        db:        atlas_db module providing get_db(), get_open_positions()
        dry_run:   Reserved for Phase B.3+ auto-fix; currently always report-only

    Returns:
        ReconcileReport with drift_detected populated; fills_* and trades_* are empty
    """
    ts = _now_utc()
    report = ReconcileReport(market_id=market_id, timestamp=ts, dry_run=dry_run)

    # ── 1. Market tickers + cross-market exclusion ────────────────────────────
    market_tickers = _get_market_tickers(market_id)
    other_market_tickers = _get_other_market_tickers(market_id)
    in_scope = market_tickers - other_market_tickers

    if not market_tickers:
        logger.warning(
            "reconcile_positions [%s]: no tickers found — scope will be empty, "
            "BROKER_ORPHAN detection disabled",
            market_id,
        )
        # Fall through: SQLite orphan detection still works with empty broker_map

    # ── 2. Broker positions filtered to in-scope tickers ─────────────────────
    try:
        all_broker_positions = broker.get_positions()
    except Exception as exc:
        report.errors.append(f"broker.get_positions failed: {exc}")
        logger.error("reconcile_positions [%s]: broker error: %s", market_id, exc)
        return report

    broker_map: dict[str, Any] = {}
    if in_scope:
        broker_map = {p.ticker: p for p in all_broker_positions if p.ticker in in_scope}
        skipped = len(all_broker_positions) - len(broker_map)
        logger.info(
            "reconcile_positions [%s]: %d broker positions in scope "
            "(%d total, %d excluded — other markets or out-of-universe)",
            market_id, len(broker_map), len(all_broker_positions), skipped,
        )
    else:
        # No universe data: fall back to state-file tickers only
        state_tickers = market_tickers  # already loaded above (empty if none)
        broker_map = {p.ticker: p for p in all_broker_positions if p.ticker in state_tickers}
        logger.warning(
            "reconcile_positions [%s]: using state-file-only scope (%d tickers)",
            market_id, len(broker_map),
        )

    # ── 3. Open SQLite trades for this market ─────────────────────────────────
    try:
        all_open = db.get_open_positions()
    except Exception as exc:
        report.errors.append(f"db.get_open_positions failed: {exc}")
        logger.error("reconcile_positions [%s]: DB error: %s", market_id, exc)
        return report

    sqlite_map: dict[str, dict] = {
        t["ticker"]: t
        for t in all_open
        if t.get("universe") == market_id
    }
    logger.info(
        "reconcile_positions [%s]: broker=%d in-scope, sqlite=%d open trades",
        market_id, len(broker_map), len(sqlite_map),
    )

    # ── 4a. BROKER_ORPHAN — broker has position, SQLite has no open trade ─────
    for ticker, bp in broker_map.items():
        if ticker not in sqlite_map:
            report.drift_detected.append({
                "type": "BROKER_ORPHAN",
                "ticker": ticker,
                "details": (
                    f"Broker: {bp.shares} shares @ ${bp.entry_price:.2f}; "
                    f"SQLite: no open trade (universe={market_id!r})"
                ),
            })
            logger.warning(
                "reconcile_positions [%s]: BROKER_ORPHAN — %s "
                "(%.0f shares @ $%.2f)",
                market_id, ticker, bp.shares, bp.entry_price,
            )

    # ── 4b. SQLITE_ORPHAN — SQLite has open trade, broker has no position ─────
    for ticker, trade in sqlite_map.items():
        if ticker not in broker_map:
            report.drift_detected.append({
                "type": "SQLITE_ORPHAN",
                "ticker": ticker,
                "details": (
                    f"SQLite: trade #{trade.get('id')} — "
                    f"{trade.get('shares')} shares @ ${float(trade.get('entry_price', 0)):.2f} "
                    f"(entry {str(trade.get('entry_date', '?'))[:10]}); "
                    f"Broker: no position"
                ),
            })
            logger.warning(
                "reconcile_positions [%s]: SQLITE_ORPHAN (MU class) — "
                "%s (trade #%d, entry %s)",
                market_id, ticker,
                trade.get("id", -1),
                str(trade.get("entry_date", "?"))[:10],
            )

    # ── 4c. QTY_DRIFT — both present but quantities differ ────────────────────
    for ticker in set(broker_map) & set(sqlite_map):
        bp = broker_map[ticker]
        trade = sqlite_map[ticker]
        broker_qty = int(bp.shares)
        sqlite_qty = int(trade.get("shares", 0))
        if broker_qty != sqlite_qty:
            report.drift_detected.append({
                "type": "QTY_DRIFT",
                "ticker": ticker,
                "details": (
                    f"Qty mismatch: broker={broker_qty}, "
                    f"SQLite={sqlite_qty} (trade #{trade.get('id')})"
                ),
            })
            logger.warning(
                "reconcile_positions [%s]: QTY_DRIFT — %s "
                "broker=%d SQLite=%d",
                market_id, ticker, broker_qty, sqlite_qty,
            )

    logger.info(
        "reconcile_positions [%s] complete: drift=%d errors=%d",
        market_id, len(report.drift_detected), len(report.errors),
    )
    return report
