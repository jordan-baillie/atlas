#!/usr/bin/env python3
"""Sync Alpaca order history into local SQLite broker_orders table.

Idempotent — safe to run multiple times per day. Run daily via cron.

Fetches all orders for the last N days (default 7) and upserts them into the
local broker_orders cache. This gives reconciliation scripts a source-of-truth
fill price so they never have to infer prices from broker position data.

Kills the CHTR phantom-price bug class: any code that needs a historical fill
price should JOIN against broker_orders instead of inferring from current
position avg_entry_price.

Cron entry (add to pi-cron.sh or /etc/cron.d/atlas):
    0 4 * * *  /usr/bin/flock -n /tmp/sync_broker_orders.lock bash -c \
        'cd /root/atlas && timeout 10m python3 scripts/sync_broker_orders.py' \
        >> /root/atlas/logs/sync_broker_orders.log 2>&1

Usage:
    python3 scripts/sync_broker_orders.py [--days N] [--dry-run]
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
from brokers.registry import get_live_broker             # noqa: E402
from utils.config import get_active_config               # noqa: E402

log = setup_logging("sync_broker_orders", extra_log_file="sync_broker_orders")

# ── Constants ────────────────────────────────────────────────────────────────
_SERVICE_NAME = "sync_broker_orders"
_DEFAULT_DAYS = 7


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe_float(val: Any) -> float | None:
    """Convert a value to float, returning None if invalid/missing."""
    if val is None:
        return None
    try:
        f = float(val)
        return f if f == f else None  # NaN check
    except (TypeError, ValueError):
        return None


def _safe_str(val: Any) -> str | None:
    """Stringify a value; return None if it would be 'None'."""
    if val is None:
        return None
    s = str(val)
    return None if s.lower() == "none" else s


def _order_to_row(order: Any, now_iso: str) -> dict:
    """Convert a raw Alpaca order object to a broker_orders row dict.

    Accepts the raw Alpaca alpaca-py Order model object.
    """
    d = order.model_dump()

    # Normalise enums to strings
    def _enum_str(v: Any) -> str | None:
        if v is None:
            return None
        s = str(v)
        # Remove 'OrderStatus.FILLED' → 'filled' etc.
        if "." in s:
            s = s.split(".", 1)[-1]
        return s.lower()

    order_id = str(d.get("id") or "")
    symbol = str(d.get("symbol") or "")

    # Side normalisation
    side_raw = d.get("side")
    side = _enum_str(side_raw) or "unknown"

    # Status normalisation
    status_raw = d.get("status")
    status = _enum_str(status_raw) or "unknown"

    # Order class
    oc_raw = d.get("order_class")
    order_class = _enum_str(oc_raw)

    # Timestamps — convert to ISO string if datetime object
    def _ts(v: Any) -> str | None:
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.isoformat()
        s = str(v)
        return None if s.lower() == "none" else s

    submitted_at = _ts(d.get("submitted_at")) or now_iso
    filled_at = _ts(d.get("filled_at"))

    # Quantities / prices
    qty = _safe_float(d.get("qty")) or 0.0
    filled_qty = _safe_float(d.get("filled_qty"))
    fill_price = _safe_float(d.get("filled_avg_price"))

    # Parent order ID (set on bracket/OCO child legs)
    parent_id = _safe_str(d.get("client_order_id"))  # Alpaca uses client_order_id as parent link
    # Actually Alpaca uses 'replaces' / nested legs. For our purposes, we capture
    # the raw JSON to allow forensic inspection; parent_id can be derived from legs.
    # Simplification: set parent_id from 'replaces' if present
    replaces = _safe_str(d.get("replaces"))

    # Serialise full order to JSON for forensic use (strip non-serialisable datetimes)
    raw_json = json.dumps(
        {k: str(v) if isinstance(v, datetime) else (
            v.model_dump() if hasattr(v, "model_dump") else (
                [leg.model_dump() if hasattr(leg, "model_dump") else str(leg)
                 for leg in v] if isinstance(v, list) else str(v)
            )
        )
        for k, v in d.items()},
        default=str,
    )

    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "filled_qty": filled_qty,
        "fill_price": fill_price,
        "status": status,
        "submitted_at": submitted_at,
        "filled_at": filled_at,
        "order_class": order_class,
        "parent_id": replaces,
        "raw_alpaca_json": raw_json,
        "last_synced_at": now_iso,
    }


def _upsert_rows(db_conn: Any, rows: list[dict], dry_run: bool) -> int:
    """Upsert a list of row dicts into broker_orders. Returns count upserted."""
    if not rows:
        return 0

    upsert_sql = """
        INSERT INTO broker_orders (
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
        log.info("DRY-RUN: would upsert %d rows into broker_orders", len(rows))
        return len(rows)

    db_conn.executemany(upsert_sql, rows)
    return len(rows)


def sync_broker_orders(days: int = _DEFAULT_DAYS, dry_run: bool = False) -> dict:
    """Main sync logic. Returns stats dict.

    Args:
        days:    How many calendar days to look back for orders.
        dry_run: If True, fetch and process but do not write to DB.

    Returns:
        dict with keys: fetched, upserted, filled_count, errors
    """
    from db import atlas_db

    stats: dict = {"fetched": 0, "upserted": 0, "filled_count": 0, "errors": []}

    # ── Broker connection ────────────────────────────────────────────────────
    try:
        config = get_active_config("sp500")
        broker = get_live_broker(config)
        if not broker or not broker.connect():
            log.error("Cannot connect to broker")
            stats["errors"].append("broker_connect_failed")
            return stats
    except Exception as exc:
        log.error("Broker init failed: %s", exc, exc_info=True)
        stats["errors"].append(f"broker_init: {exc}")
        return stats

    now_iso = datetime.now(timezone.utc).isoformat()
    now_utc = datetime.now(timezone.utc)
    start = now_utc - timedelta(days=days)

    try:
        # ── Fetch orders from Alpaca ─────────────────────────────────────────
        log.info("Fetching orders from Alpaca: last %d days (since %s)", days, start.date())
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus

            req = GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                after=start,
                limit=500,
            )
            raw_orders = broker._broker_call(broker._trade_client.get_orders, req)
            log.info("Fetched %d orders from Alpaca", len(raw_orders or []))
        except Exception as exc:
            log.error("get_orders failed: %s", exc, exc_info=True)
            stats["errors"].append(f"get_orders: {exc}")
            raw_orders = []

        if not raw_orders:
            log.warning("No orders returned from Alpaca")
            _write_heartbeat(dry_run, stats)
            return stats

        stats["fetched"] = len(raw_orders)

        # ── Convert to row dicts ─────────────────────────────────────────────
        rows: list[dict] = []
        for order in raw_orders:
            try:
                row = _order_to_row(order, now_iso)
                rows.append(row)
                if row.get("fill_price") and row["fill_price"] > 0:
                    stats["filled_count"] += 1
            except Exception as exc:
                order_id = getattr(order, "id", "?")
                log.warning("Failed to convert order %s: %s", order_id, exc)
                stats["errors"].append(f"convert:{order_id}: {exc}")

        log.info(
            "Converted %d rows (%d with fill prices)",
            len(rows), stats["filled_count"],
        )

        # ── Upsert into SQLite ───────────────────────────────────────────────
        if rows:
            with atlas_db.get_db() as db:
                upserted = _upsert_rows(db, rows, dry_run=dry_run)
                stats["upserted"] = upserted
                if not dry_run:
                    # Verify row count
                    count = db.execute("SELECT COUNT(*) FROM broker_orders").fetchone()[0]
                    log.info(
                        "broker_orders table now has %d total rows (upserted %d in this run)",
                        count, upserted,
                    )
                    stats["total_in_table"] = count

    except Exception as exc:
        log.error("Sync failed: %s", exc, exc_info=True)
        stats["errors"].append(f"sync: {exc}")
    finally:
        try:
            broker.disconnect()
        except Exception:
            pass

    _write_heartbeat(dry_run, stats)

    log.info(
        "sync_broker_orders complete: fetched=%d upserted=%d filled=%d errors=%d",
        stats["fetched"], stats["upserted"], stats["filled_count"], len(stats["errors"]),
    )
    return stats


def _write_heartbeat(dry_run: bool, stats: dict) -> None:
    """Write a heartbeat record to the DB (non-fatal)."""
    if dry_run:
        return
    try:
        from db import atlas_db
        atlas_db.record_heartbeat(
            service=_SERVICE_NAME,
            status="ok" if not stats.get("errors") else "warn",
            detail={
                "fetched": stats.get("fetched", 0),
                "upserted": stats.get("upserted", 0),
                "filled_count": stats.get("filled_count", 0),
                "errors": stats.get("errors", []),
            },
        )
    except Exception as exc:
        log.warning("Heartbeat write failed (non-fatal): %s", exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=_DEFAULT_DAYS,
        help=f"Days to look back (default: {_DEFAULT_DAYS})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch from Alpaca but do not write to DB",
    )
    args = parser.parse_args(argv)

    stats = sync_broker_orders(days=args.days, dry_run=args.dry_run)

    if stats.get("errors"):
        log.warning("Completed with %d errors: %s", len(stats["errors"]), stats["errors"])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
