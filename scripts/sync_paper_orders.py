#!/usr/bin/env python3
"""Sync Alpaca PAPER order history into local SQLite paper_broker_orders table.

Mirrors scripts/sync_broker_orders.py for the Alpaca paper trading account.
Also writes back newly filled BUY/SELL orders to the paper_trades table so
that PAPER-state strategies accumulate a proper trade record for validation.

Root cause it fixes: brokers/live_executor.py:1188-1232 only records to
paper_trades when order_result.status == FILLED.  LIMIT orders return
SUBMITTED and never fill in-process.  This poller catches fills at Alpaca
and writes them into paper_trades retrospectively.

Idempotent — safe to run multiple times per day.  Run every 5 min during
US RTH via cron.  Unblocks paper→live promotion for mean_reversion,
short_term_mr, connors_rsi2 (all in PAPER lifecycle state as of 2026-05-19).

Usage:
    python3 scripts/sync_paper_orders.py [--days N] [--dry-run]
    python3 scripts/sync_paper_orders.py --backfill-ids d46a49ae,9f4b7e6b,667b4469
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ── Project bootstrap ────────────────────────────────────────────────────────
ATLAS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ATLAS_ROOT))

from atlas_bootstrap import PROJECT_ROOT as PROJECT      # noqa: E402
from utils.logging_config import setup_logging           # noqa: E402

log = setup_logging("sync_paper_orders", extra_log_file="sync_paper_orders")

# ── Constants ────────────────────────────────────────────────────────────────
_SERVICE_NAME = "sync_paper_orders"
_DEFAULT_DAYS = 7
_SUCCESS_STAMP_PATH = PROJECT / "data" / ".sync_paper_orders_last_ok"
_STALE_THRESHOLD_HOURS = 6   # alert after 6h gap (RTH-aware)


# ── Value helpers (mirrors sync_broker_orders.py) ────────────────────────────

def _safe_float(val: Any) -> float | None:
    """Convert value to float; return None if invalid/missing."""
    if val is None:
        return None
    try:
        f = float(val)
        return f if f == f else None   # NaN check
    except (TypeError, ValueError):
        return None


def _safe_str(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val)
    return None if s.lower() == "none" else s


def _enum_str(v: Any) -> str | None:
    """Normalise Alpaca enums/strings to lowercase plain string."""
    if v is None:
        return None
    s = str(v)
    if "." in s:
        s = s.split(".", 1)[-1]
    return s.lower()


def _ts(v: Any) -> str | None:
    """Convert datetime or string to ISO str; return None if absent."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    s = str(v)
    return None if s.lower() == "none" else s


# ── Order conversion ─────────────────────────────────────────────────────────

def _order_to_row(order: Any, now_iso: str) -> dict:
    """Convert raw Alpaca order object → paper_broker_orders row dict."""
    d = order.model_dump()

    order_id    = str(d.get("id") or "")
    symbol      = str(d.get("symbol") or "")
    side        = _enum_str(d.get("side")) or "unknown"
    status      = _enum_str(d.get("status")) or "unknown"
    order_class = _enum_str(d.get("order_class"))

    submitted_at = _ts(d.get("submitted_at")) or now_iso
    filled_at    = _ts(d.get("filled_at"))
    qty          = _safe_float(d.get("qty")) or 0.0
    filled_qty   = _safe_float(d.get("filled_qty"))
    fill_price   = _safe_float(d.get("filled_avg_price"))
    replaces     = _safe_str(d.get("replaces"))

    # Serialise full order to JSON for forensic inspection
    raw_json = json.dumps(
        {
            k: str(v) if isinstance(v, datetime) else (
                v.model_dump() if hasattr(v, "model_dump") else (
                    [
                        leg.model_dump() if hasattr(leg, "model_dump") else str(leg)
                        for leg in v
                    ] if isinstance(v, list) else str(v)
                )
            )
            for k, v in d.items()
        },
        default=str,
    )

    return {
        "order_id":        order_id,
        "symbol":          symbol,
        "side":            side,
        "qty":             qty,
        "filled_qty":      filled_qty,
        "fill_price":      fill_price,
        "status":          status,
        "submitted_at":    submitted_at,
        "filled_at":       filled_at,
        "order_class":     order_class,
        "parent_id":       replaces,
        "raw_alpaca_json": raw_json,
        "last_synced_at":  now_iso,
    }


# ── DB upsert ────────────────────────────────────────────────────────────────

def _upsert_rows(db_conn: Any, rows: list[dict], dry_run: bool) -> int:
    """Upsert rows into paper_broker_orders. Returns count upserted."""
    if not rows:
        return 0

    sql = """
        INSERT INTO paper_broker_orders (
            order_id, symbol, side, qty, filled_qty, fill_price,
            status, submitted_at, filled_at, order_class, parent_id,
            raw_alpaca_json, last_synced_at
        ) VALUES (
            :order_id, :symbol, :side, :qty, :filled_qty, :fill_price,
            :status, :submitted_at, :filled_at, :order_class, :parent_id,
            :raw_alpaca_json, :last_synced_at
        )
        ON CONFLICT(order_id) DO UPDATE SET
            symbol          = excluded.symbol,
            side            = excluded.side,
            qty             = excluded.qty,
            filled_qty      = excluded.filled_qty,
            fill_price      = excluded.fill_price,
            status          = excluded.status,
            submitted_at    = excluded.submitted_at,
            filled_at       = excluded.filled_at,
            order_class     = excluded.order_class,
            parent_id       = excluded.parent_id,
            raw_alpaca_json = excluded.raw_alpaca_json,
            last_synced_at  = excluded.last_synced_at
    """
    if dry_run:
        log.info("DRY-RUN: would upsert %d rows into paper_broker_orders", len(rows))
        return len(rows)

    db_conn.executemany(sql, rows)
    return len(rows)


# ── Strategy resolution ───────────────────────────────────────────────────────

def _lookup_strategy_from_plans(
    ticker: str,
    universe: str,
    filled_at_date: str,
) -> str | None:
    """Search recent plan files to find the strategy for this ticker.

    Looks up to 8 calendar days back.  Returns strategy string or None.
    """
    try:
        filled_dt = datetime.strptime(filled_at_date, "%Y-%m-%d")
    except ValueError:
        filled_dt = datetime.now()

    plans_dir = PROJECT / "plans"
    markets_to_try = sorted({universe, "sp500"})

    for days_ago in range(8):
        date_str = (filled_dt - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        for market in markets_to_try:
            plan_file = plans_dir / f"plan_{market}_{date_str}.json"
            if not plan_file.exists():
                continue
            try:
                plan = json.loads(plan_file.read_text())
                for entry in plan.get("proposed_entries", []):
                    if entry.get("ticker") == ticker:
                        strat = entry.get("strategy")
                        if strat:
                            log.debug(
                                "_lookup_strategy_from_plans: %s → %s (plan %s)",
                                ticker, strat, plan_file.name,
                            )
                            return strat
            except Exception as exc:
                log.debug("Failed to parse plan %s: %s", plan_file, exc)

    return None


def _lookup_strategy(
    ticker: str,
    universe: str,
    filled_at_date: str,
    db_conn: Any,
) -> str | None:
    """Resolve the strategy for a filled paper order.

    Priority order:
    1. If exactly one PAPER-state strategy for universe → use it.
    2. If multiple → fall back to plan file lookup.
    3. If zero or still unresolvable → return None (caller logs WARNING + skips).
    """
    rows = db_conn.execute(
        "SELECT strategy FROM strategy_lifecycle WHERE state='PAPER' AND universe=?",
        (universe,),
    ).fetchall()

    strategies = [r[0] for r in rows]

    if len(strategies) == 1:
        return strategies[0]

    if len(strategies) == 0:
        log.warning(
            "_lookup_strategy: no PAPER strategies found for universe=%s (ticker=%s) — skip",
            universe, ticker,
        )
        return None

    # Multiple PAPER strategies: fall back to plan file
    log.debug(
        "_lookup_strategy: %d PAPER strategies for universe=%s ticker=%s, "
        "falling back to plan file",
        len(strategies), universe, ticker,
    )
    strat = _lookup_strategy_from_plans(ticker, universe, filled_at_date)
    if strat is None:
        log.warning(
            "_lookup_strategy: multiple PAPER strategies %s for %s/%s "
            "and no plan entry found — cannot attribute, skipping",
            strategies, ticker, universe,
        )
    return strat


# ── Bracket leg parsing ───────────────────────────────────────────────────────

def _parse_bracket_legs(d: dict) -> tuple[float | None, float | None]:
    """Extract stop_price and take_profit from Alpaca bracket child legs.

    Returns (stop_price, take_profit); either may be None.
    """
    stop_price: float | None = None
    take_profit: float | None = None

    legs = d.get("legs") or []
    for leg in legs:
        if hasattr(leg, "model_dump"):
            ld: dict = leg.model_dump()
        elif isinstance(leg, dict):
            ld = leg
        else:
            ld = {}

        ot = _enum_str(ld.get("order_type") or ld.get("type")) or ""
        if "stop" in ot:
            stop_price = _safe_float(ld.get("stop_price"))
        elif "limit" in ot:
            take_profit = _safe_float(ld.get("limit_price"))

    return stop_price, take_profit


# ── Paper-trades write-back ───────────────────────────────────────────────────

def _record_newly_filled_paper_trades(
    db_conn: Any,
    raw_orders: list[Any],
    dry_run: bool = False,
) -> dict:
    """Ensure paper_trades rows exist for every filled paper order.

    For filled BUY orders: insert into paper_trades (idempotent).
    For filled SELL orders: close the matching open paper_trades row.

    Args:
        db_conn:    Open SQLite connection (inside atlas_db context manager).
        raw_orders: Raw Alpaca order objects from broker.
        dry_run:    Log actions but do not write.

    Returns:
        dict with paper_trades_inserted, paper_exits_recorded, errors keys.
    """
    from db import atlas_db  # local import so test _db_path_override propagates

    try:
        from universe.membership import derive_universe
    except Exception:
        def derive_universe(t: str, h: str | None = None) -> str | None:  # type: ignore[misc]
            return h or "sp500"

    stats: dict = {
        "paper_trades_inserted": 0,
        "paper_exits_recorded": 0,
        "errors": [],
    }

    for order in raw_orders:
        d: dict = {}
        try:
            d = order.model_dump()
            order_id = str(d.get("id") or "")
            status   = _enum_str(d.get("status")) or ""

            if status != "filled":
                continue

            side   = _enum_str(d.get("side")) or ""
            symbol = str(d.get("symbol") or "")
            if not symbol:
                continue

            fill_price  = _safe_float(d.get("filled_avg_price"))
            filled_qty  = _safe_float(d.get("filled_qty"))
            filled_at_v = d.get("filled_at")

            if filled_at_v is None:
                continue
            filled_at_str  = _ts(filled_at_v) or ""
            filled_at_date = filled_at_str[:10]

            universe = derive_universe(symbol, "sp500") or "sp500"

            # ── BUY fill → insert paper_trade entry ──────────────────────
            if side == "buy":
                if not fill_price or fill_price <= 0:
                    log.debug("Skipping BUY %s — no fill_price in order %s", symbol, order_id)
                    continue

                # Idempotency: skip if matching row already exists
                existing = db_conn.execute(
                    """SELECT id FROM paper_trades
                       WHERE ticker = ?
                         AND DATE(entry_date) >= DATE(?, '-2 days')
                         AND ABS(entry_price - ?) < 0.01
                       LIMIT 1""",
                    (symbol, filled_at_date, fill_price),
                ).fetchone()
                if existing:
                    log.debug(
                        "paper_trades idempotency hit: %s %.2f near %s (id=%s) — skip",
                        symbol, fill_price, filled_at_date, existing[0],
                    )
                    continue

                strategy = _lookup_strategy(symbol, universe, filled_at_date, db_conn)
                if strategy is None:
                    stats["errors"].append(
                        f"strategy_ambiguous:{symbol}:{filled_at_date}"
                    )
                    continue

                stop_price, take_profit = _parse_bracket_legs(d)

                # Direction guard: stop must be strictly below entry for long
                if stop_price is not None and stop_price >= fill_price:
                    log.warning(
                        "paper_trades: inverted stop %.2f >= entry %.2f for %s "
                        "— setting stop_price=NULL",
                        stop_price, fill_price, symbol,
                    )
                    stop_price = None

                shares = max(1, int(filled_qty or 1))

                if dry_run:
                    log.info(
                        "DRY-RUN: would insert paper_trade %s/%s strategy=%s "
                        "entry=%.2f shares=%d stop=%s tp=%s",
                        symbol, universe, strategy, fill_price, shares,
                        stop_price, take_profit,
                    )
                    stats["paper_trades_inserted"] += 1
                    continue

                row_id = atlas_db.record_paper_trade_entry(
                    ticker=symbol,
                    strategy=strategy,
                    universe=universe,
                    entry_price=fill_price,
                    shares=shares,
                    stop_price=stop_price,
                    take_profit=take_profit,
                    confidence=0.7,
                    regime_state=None,
                    direction="long",
                )
                if row_id is not None:
                    log.info(
                        "paper_trade inserted id=%d: %s/%s strategy=%s "
                        "entry=%.2f shares=%d",
                        row_id, symbol, universe, strategy, fill_price, shares,
                    )
                    stats["paper_trades_inserted"] += 1
                else:
                    log.debug(
                        "paper_trade insert blocked (UNIQUE): %s/%s",
                        symbol, universe,
                    )

            # ── SELL fill → close open paper_trade ───────────────────────
            elif side == "sell":
                if not fill_price or fill_price <= 0:
                    log.debug(
                        "Skipping SELL %s — no fill_price in order %s",
                        symbol, order_id,
                    )
                    continue

                open_row = db_conn.execute(
                    """SELECT id, strategy FROM paper_trades
                       WHERE ticker = ? AND universe = ? AND status = 'open'
                       ORDER BY id DESC LIMIT 1""",
                    (symbol, universe),
                ).fetchone()

                if not open_row:
                    log.debug(
                        "No open paper_trade for SELL %s/%s — nothing to close",
                        symbol, universe,
                    )
                    continue

                if dry_run:
                    log.info(
                        "DRY-RUN: would record paper exit %s/%s exit=%.2f",
                        symbol, universe, fill_price,
                    )
                    stats["paper_exits_recorded"] += 1
                    continue

                strategy = str(open_row[1] or "unknown")
                atlas_db.record_paper_trade_exit(
                    ticker=symbol,
                    strategy=strategy,
                    exit_price=fill_price,
                    exit_reason="paper_fill_recorded",
                    regime_at_exit=None,
                )
                log.info(
                    "paper_trade exit recorded: %s/%s strategy=%s exit=%.2f",
                    symbol, universe, strategy, fill_price,
                )
                stats["paper_exits_recorded"] += 1

        except Exception as exc:
            oid = str(d.get("id", "?")) if d else "?"
            log.warning(
                "_record_newly_filled_paper_trades: error on order %s: %s",
                oid, exc, exc_info=True,
            )
            stats["errors"].append(f"record:{oid}:{exc}")

    return stats


# ── Heartbeat + staleness ────────────────────────────────────────────────────

def _write_heartbeat(dry_run: bool, stats: dict) -> None:
    """Write heartbeat record (non-fatal)."""
    if dry_run:
        return
    try:
        from db import atlas_db
        atlas_db.record_heartbeat(
            service=_SERVICE_NAME,
            status="ok" if not stats.get("errors") else "warn",
            detail={
                "fetched":               stats.get("fetched", 0),
                "upserted":              stats.get("upserted", 0),
                "filled_count":          stats.get("filled_count", 0),
                "paper_trades_inserted": stats.get("paper_trades_inserted", 0),
                "paper_exits_recorded":  stats.get("paper_exits_recorded", 0),
                "errors":                stats.get("errors", []),
            },
        )
    except Exception as exc:
        log.warning("Heartbeat write failed (non-fatal): %s", exc)


def _check_staleness() -> None:
    """Alert if last-success stamp is > threshold hours old."""
    if not _SUCCESS_STAMP_PATH.exists():
        log.warning("sync_paper_orders: no last-success stamp — first run or deleted")
        return
    import time
    age_h = (time.time() - _SUCCESS_STAMP_PATH.stat().st_mtime) / 3600
    if age_h > _STALE_THRESHOLD_HOURS:
        msg = (
            f"⚠️ sync_paper_orders stale: last success was {age_h:.1f}h ago "
            f"(threshold {_STALE_THRESHOLD_HOURS}h). paper_broker_orders may be stale."
        )
        log.warning(msg)
        try:
            from utils.telegram import send_message
            send_message(msg)
        except Exception as _exc:
            log.debug("Telegram alert failed (non-fatal): %s", _exc)


def _update_success_stamp() -> None:
    """Touch the last-success stamp file."""
    try:
        _SUCCESS_STAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SUCCESS_STAMP_PATH.touch()
    except Exception as exc:
        log.debug("Failed to update success stamp: %s", exc)


# ── Main sync function ───────────────────────────────────────────────────────

def sync_paper_orders(
    days: int = _DEFAULT_DAYS,
    dry_run: bool = False,
    backfill_ids: list[str] | None = None,
) -> dict:
    """Main sync logic for the Alpaca PAPER account.

    Args:
        days:         How many calendar days to look back.
        dry_run:      Fetch and process but do not write to DB.
        backfill_ids: If given, only process orders whose IDs start with
                      any of the provided prefixes (enables ad-hoc backfill).

    Returns:
        dict with keys: fetched, upserted, filled_count,
                        paper_trades_inserted, paper_exits_recorded, errors.
    """
    from db import atlas_db

    stats: dict = {
        "fetched": 0,
        "upserted": 0,
        "filled_count": 0,
        "paper_trades_inserted": 0,
        "paper_exits_recorded": 0,
        "errors": [],
    }

    # ── Construct paper broker via routing policy ────────────────────────────
    try:
        from utils.config import get_active_config
        from brokers.routing_policy import BrokerRoutingPolicy
        from brokers.registry import get_live_broker

        live_config = get_active_config("sp500")
        policy      = BrokerRoutingPolicy(live_config, "sp500")
        paper_config = policy.paper_config          # forces trading.mode="paper"
        broker = get_live_broker(paper_config)

        if broker is None:
            log.error(
                "Paper broker not available — check ALPACA_PAPER_API_KEY / "
                "ALPACA_PAPER_SECRET_KEY in secrets"
            )
            stats["errors"].append("paper_broker_unavailable")
            return stats

        if not broker.connect():
            log.error("Cannot connect to Alpaca paper account")
            stats["errors"].append("paper_broker_connect_failed")
            return stats
    except Exception as exc:
        log.error("Paper broker init failed: %s", exc, exc_info=True)
        stats["errors"].append(f"broker_init:{exc}")
        return stats

    now_iso  = datetime.now(timezone.utc).isoformat()
    now_utc  = datetime.now(timezone.utc)
    start    = now_utc - timedelta(days=days)

    try:
        # ── Fetch orders from Alpaca paper account ────────────────────────
        log.info(
            "Fetching paper orders from Alpaca: last %d days (since %s)",
            days, start.date(),
        )
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus

            req = GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                after=start,
                limit=500,
            )
            raw_orders = broker._broker_call(broker._trade_client.get_orders, req)
            log.info("Fetched %d paper orders", len(raw_orders or []))
        except Exception as exc:
            log.error("get_orders (paper) failed: %s", exc, exc_info=True)
            stats["errors"].append(f"get_orders:{exc}")
            raw_orders = []

        if not raw_orders:
            log.warning("No paper orders returned from Alpaca")
            _write_heartbeat(dry_run, stats)
            return stats

        # ── Filter to backfill IDs when requested ─────────────────────────
        if backfill_ids:
            id_prefixes = set(backfill_ids)

            def _id_matches(o: Any) -> bool:
                try:
                    oid = str(o.model_dump().get("id", ""))
                    return any(oid == p or oid.startswith(p) for p in id_prefixes)
                except Exception:
                    return False

            raw_orders = [o for o in raw_orders if _id_matches(o)]
            log.info(
                "Backfill filter: %d orders match IDs %s",
                len(raw_orders), list(id_prefixes),
            )

        stats["fetched"] = len(raw_orders)

        # ── Convert to row dicts ──────────────────────────────────────────
        rows: list[dict] = []
        for order in raw_orders:
            try:
                row = _order_to_row(order, now_iso)
                rows.append(row)
                if row.get("fill_price") and row["fill_price"] > 0:
                    stats["filled_count"] += 1
            except Exception as exc:
                oid = getattr(order, "id", "?")
                log.warning("Failed to convert order %s: %s", oid, exc)
                stats["errors"].append(f"convert:{oid}:{exc}")

        log.info(
            "Converted %d rows (%d with fill prices)",
            len(rows), stats["filled_count"],
        )

        # ── Upsert into paper_broker_orders ────────────────────────────────
        # IMPORTANT: split into two separate `with get_db()` blocks.
        # _upsert_rows holds a write transaction; record_paper_trade_entry
        # (called inside _record_newly_filled_paper_trades) opens a second
        # write connection.  SQLite only allows one writer at a time — even
        # in WAL mode, a nested write from the same process blocks for the
        # full busy_timeout (30 s) then raises "database is locked".
        # Splitting the blocks ensures the upsert commits before the
        # paper_trades write begins.
        if rows:
            with atlas_db.get_db() as db:
                upserted = _upsert_rows(db, rows, dry_run=dry_run)
                stats["upserted"] = upserted

                if not dry_run:
                    count = db.execute(
                        "SELECT COUNT(*) FROM paper_broker_orders"
                    ).fetchone()[0]
                    log.info(
                        "paper_broker_orders: %d total rows (upserted %d this run)",
                        count, upserted,
                    )
                    stats["total_in_table"] = count
            # <-- upsert committed; write lock released

            # ── Write back fills to paper_trades ─────────────────────────
            # Fresh connection: only reads (idempotency/strategy lookups)
            # so record_paper_trade_entry's own write connection is free.
            with atlas_db.get_db() as db:
                fill_stats = _record_newly_filled_paper_trades(
                    db, raw_orders, dry_run=dry_run
                )
                stats["paper_trades_inserted"] += fill_stats["paper_trades_inserted"]
                stats["paper_exits_recorded"]  += fill_stats["paper_exits_recorded"]
                stats["errors"].extend(fill_stats["errors"])

    except Exception as exc:
        log.error("sync_paper_orders failed: %s", exc, exc_info=True)
        stats["errors"].append(f"sync:{exc}")
    finally:
        try:
            broker.disconnect()
        except Exception:
            pass

    _write_heartbeat(dry_run, stats)
    log.info(
        "sync_paper_orders complete: fetched=%d upserted=%d filled=%d "
        "paper_trades_inserted=%d paper_exits=%d errors=%d",
        stats["fetched"], stats["upserted"], stats["filled_count"],
        stats["paper_trades_inserted"], stats["paper_exits_recorded"],
        len(stats["errors"]),
    )
    return stats


# ── CLI entry point ───────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--days", type=int, default=_DEFAULT_DAYS,
        help=f"Days to look back (default: {_DEFAULT_DAYS})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch from Alpaca paper account but do not write to DB",
    )
    parser.add_argument(
        "--backfill-ids", type=str, default="",
        help="Comma-separated order ID prefixes to process (backfill mode)",
    )
    args = parser.parse_args(argv)

    _check_staleness()

    backfill_ids: list[str] | None = None
    if args.backfill_ids:
        backfill_ids = [x.strip() for x in args.backfill_ids.split(",") if x.strip()]

    stats = sync_paper_orders(
        days=args.days,
        dry_run=args.dry_run,
        backfill_ids=backfill_ids,
    )

    if stats.get("errors"):
        log.warning(
            "Completed with %d error(s): %s",
            len(stats["errors"]), stats["errors"],
        )
        return 1

    if not args.dry_run:
        _update_success_stamp()
    return 0


if __name__ == "__main__":
    sys.exit(main())
