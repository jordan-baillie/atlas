#!/usr/bin/env python3
"""
Retro-attach missing TP limit orders for open positions.

For positions whose OCO bracket was upgraded to a trailing-stop-only order
(RCA Phase 2C - commit 35d2286a) without placing a replacement standalone
TP limit order, this script:

  1. Reads the derivable TP price from the trades DB
  2. Cancels the existing trailing stop at Alpaca and waits for confirmation
  3. Places a replacement OCO order (static stop at current trailing-stop level
     + GTC sell limit at TP price) - this satisfies BOTH stop and TP coverage
  4. Updates the live_<market>.json state file with the new OCO order_id
  5. Updates the trades DB with the new tp_order_id (and stop_order_id)

Trade-off documented:
    Converting from trailing_stop (simple) to static_stop (OCO) means the stop
    will no longer ratchet up with price.  The current trailing stop price is
    used as the static stop level.

    This is the ONLY viable path because Alpaca error 40310000 blocks any
    standalone SELL limit order when a trailing stop already reserves all shares.

Supports --dry-run (no broker calls, no state file writes).

Usage:
    python3 scripts/retro_attach_tp_legs.py --dry-run
    python3 scripts/retro_attach_tp_legs.py

FCX is explicitly skipped: connors_rsi2 strategy has profit_target_atr_mult=0
(disabled). No fixed-price TP exists by design. FCX exits via SMA(5)-close or
RSI>65, not a price target.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# -- Path bootstrap ------------------------------------------------------------
_HERE = Path(__file__).resolve()
_ATLAS_ROOT = _HERE.parent.parent
if str(_ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATLAS_ROOT))

logger = logging.getLogger(__name__)

# -- Constants -----------------------------------------------------------------
_STATE_DIR = _ATLAS_ROOT / "brokers" / "state"
_DB_PATH = _ATLAS_ROOT / "data" / "atlas.db"
_HEALTHCHECK_STATE = _ATLAS_ROOT / "data" / "healthcheck_tp_coverage_state.json"

_CANCEL_POLL_INTERVAL = 0.3
_CANCEL_TIMEOUT_S = 15.0


def _state_path(market_id: str) -> Path:
    return _STATE_DIR / f"live_{market_id}.json"


# -- TP price lookup -----------------------------------------------------------

def _get_tp_from_db(ticker: str) -> float | None:
    """Return the take_profit price for the most recent open trade for ticker."""
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT take_profit FROM trades
                WHERE ticker = ? AND status = 'open' AND take_profit IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (ticker,),
            ).fetchone()
        if row:
            return float(row["take_profit"])
    except sqlite3.Error as exc:
        logger.error("DB query failed for %s: %s", ticker, exc)
    return None


def _update_db_orders(ticker: str, tp_order_id: str, stop_order_id: str, dry_run: bool) -> bool:
    """Update both tp_order_id and stop_order_id on the open trade for ticker."""
    if dry_run:
        logger.info(
            "[DRY-RUN] Would update DB: %s  stop_order_id=%s  tp_order_id=%s",
            ticker, stop_order_id, tp_order_id,
        )
        return True
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            affected = conn.execute(
                """
                UPDATE trades
                SET tp_order_id = ?, stop_order_id = ?, updated_at = ?
                WHERE ticker = ? AND status = 'open'
                """,
                (
                    tp_order_id,
                    stop_order_id,
                    datetime.now(tz=timezone.utc).isoformat(),
                    ticker,
                ),
            ).rowcount
            conn.commit()
        if affected == 0:
            logger.warning("DB update: no open trade row found for %s", ticker)
            return False
        logger.info(
            "DB updated: %s  stop_order_id=%s  tp_order_id=%s  (%d row)",
            ticker, stop_order_id, tp_order_id, affected,
        )
        return True
    except sqlite3.Error as exc:
        logger.error("DB update failed for %s: %s", ticker, exc)
        return False


# -- State file helpers --------------------------------------------------------

def _load_state_file(market_id: str) -> dict[str, Any]:
    path = _state_path(market_id)
    try:
        data = json.loads(path.read_text())
        return data
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot load state file for {market_id}: {exc}") from exc


def _save_state_file(market_id: str, data: dict[str, Any], dry_run: bool) -> None:
    path = _state_path(market_id)
    if dry_run:
        logger.info("[DRY-RUN] Would write state file: %s", path)
        return
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
    logger.info("State file updated: %s", path)


def _update_state_orders(
    market_id: str,
    ticker: str,
    stop_order_id: str,
    tp_order_id: str,
    dry_run: bool,
) -> bool:
    """Update both stop_order_id and tp_order_id for ticker in market state file."""
    try:
        data = _load_state_file(market_id)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return False

    positions: list[dict[str, Any]] = data.get("positions", [])
    updated = False
    for pos in positions:
        if pos.get("ticker") == ticker:
            pos["stop_order_id"] = stop_order_id
            pos["tp_order_id"] = tp_order_id
            updated = True
            logger.info(
                "State: %s/%s  stop_order_id=%s  tp_order_id=%s",
                market_id, ticker, stop_order_id, tp_order_id,
            )
            break

    if not updated:
        logger.warning("Ticker %s not found in %s state file", ticker, market_id)
        return False

    _save_state_file(market_id, data, dry_run)
    return True


# -- Broker --------------------------------------------------------------------

def _get_broker():
    """Return a connected AlpacaBroker (sp500 config = same Alpaca account)."""
    from brokers.alpaca.broker import AlpacaBroker
    from utils.config import get_active_config

    cfg = get_active_config("sp500")
    broker = AlpacaBroker(cfg)
    connected = broker.connect()
    if not connected:
        raise RuntimeError("Broker.connect() returned False")
    return broker


def _cancel_and_confirm(broker: Any, order_id: str, ticker: str) -> bool:
    """Cancel order_id and wait for Alpaca to confirm cancellation."""
    cancel_result = broker.cancel_order(order_id)
    if not cancel_result.success:
        logger.error(
            "cancel_order(%s) returned failure for %s: %s",
            order_id[:8], ticker, cancel_result.message,
        )
        return False

    confirmed = broker._wait_for_cancel_confirmed(
        order_id, timeout_s=_CANCEL_TIMEOUT_S, poll_interval_s=_CANCEL_POLL_INTERVAL,
    )
    if not confirmed:
        logger.error(
            "Cancel of %s (%s) NOT confirmed within %.1fs -- aborting OCO placement",
            order_id[:8], ticker, _CANCEL_TIMEOUT_S,
        )
    return confirmed


def _place_oco(
    broker: Any,
    ticker: str,
    qty: int,
    stop_price: float,
    tp_price: float,
    dry_run: bool,
) -> str | None:
    """Place a GTC OCO order: SELL LIMIT at tp_price + stop_loss at stop_price.

    An OCO satisfies BOTH stop AND TP coverage in a single order.
    Healthcheck sees order_class='oco' as fully covered.

    Returns OCO parent order_id, or None on failure.
    """
    if dry_run:
        fake_id = f"dryrun-oco-{ticker.lower()}-fake"
        logger.info(
            "[DRY-RUN] Would place OCO: %s x %d  stop=%.4f  tp=%.4f -> id=%s",
            ticker, qty, stop_price, tp_price, fake_id,
        )
        return fake_id

    try:
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest
        from alpaca.trading.enums import OrderSide as AlpacaSide, TimeInForce, OrderClass
        from brokers.alpaca import mapper
    except ImportError as exc:
        logger.error("Alpaca SDK import failed: %s", exc)
        return None

    alpaca_symbol = mapper.to_alpaca(ticker)
    client_id = f"atlas_retro_tp_{uuid.uuid4().hex[:8]}"

    try:
        request = LimitOrderRequest(
            symbol=alpaca_symbol,
            qty=qty,
            side=AlpacaSide.SELL,
            limit_price=round(tp_price, 2),
            order_class=OrderClass.OCO,
            take_profit=TakeProfitRequest(limit_price=round(tp_price, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
            time_in_force=TimeInForce.GTC,
            client_order_id=client_id,
        )
        order = broker._broker_call(broker._trade_client.submit_order, request)
        oco_id = str(order.id)
        logger.info(
            "OCO placed: %s x %d  stop=%.4f  tp=%.4f -> oco_id=%s",
            ticker, qty, stop_price, tp_price, oco_id,
        )
        return oco_id
    except Exception as exc:
        logger.error("OCO placement failed for %s: %s", ticker, exc, exc_info=True)
        return None


# -- Per-ticker definitions ----------------------------------------------------

_POSITIONS_TO_FIX: list[tuple[str, str, int]] = [
    ("CAT", "sp500",          1),
    ("GLD", "commodity_etfs", 2),
    ("XLI", "sector_etfs",    9),
]

_FCX_SKIP_REASON = (
    "FCX -- connors_rsi2 strategy: profit_target_atr_mult=0 (disabled). "
    "No price-based TP exists by design. Strategy exits via SMA(5) close or RSI>65. "
    "FCX will remain flagged in healthcheck until a strategy-level TP-exempt rule is added."
)

# -- Healthcheck state cleanup -------------------------------------------------

_ORPHAN_KEYS_TO_REMOVE = frozenset({
    "sector_etfs:XLY",        # XLY no longer held
    "sp500:XLY",
    "commodity_etfs:XLY",
    "sp500:GLD",              # GLD is in commodity_etfs, not sp500
    "sp500:XLI",              # XLI is in sector_etfs, not sp500
    "commodity_etfs:XLI",    # XLI is in sector_etfs, not commodity_etfs
    "sector_etfs:GLD",       # GLD is in commodity_etfs, not sector_etfs
    "sp500:FCX", "commodity_etfs:FCX", "sector_etfs:FCX",  # FCX: no TP by design
})

_CANONICAL_KEYS: dict[str, str] = {
    "CAT": "sp500:CAT",
    "GLD": "commodity_etfs:GLD",
    "XLI": "sector_etfs:XLI",
}

_CROSS_MARKET_NOISE_KEYS: dict[str, list[str]] = {
    "CAT": ["commodity_etfs:CAT", "sector_etfs:CAT"],
}


def _clean_healthcheck_state(successfully_fixed: list[str], dry_run: bool) -> None:
    """Remove resolved/stale entries from the healthcheck state file."""
    try:
        raw = _HEALTHCHECK_STATE.read_text()
        state = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.error("Cannot load healthcheck state: %s", exc)
        return

    first_missing: dict[str, str] = state.get("first_missing_at", {})
    keys_to_remove: set[str] = set(_ORPHAN_KEYS_TO_REMOVE)

    for ticker in successfully_fixed:
        canonical = _CANONICAL_KEYS.get(ticker)
        if canonical:
            keys_to_remove.add(canonical)
        for extra_key in _CROSS_MARKET_NOISE_KEYS.get(ticker, []):
            keys_to_remove.add(extra_key)

    removed = [k for k in list(first_missing.keys()) if k in keys_to_remove]
    for k in removed:
        del first_missing[k]

    now_iso = datetime.now(tz=timezone.utc).isoformat()
    state["first_missing_at"] = first_missing
    state["last_run_at"] = now_iso

    if dry_run:
        logger.info(
            "[DRY-RUN] Would remove %d keys from healthcheck state: %s",
            len(removed), removed,
        )
        return

    tmp = _HEALTHCHECK_STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(_HEALTHCHECK_STATE)
    logger.info(
        "Healthcheck state cleaned: removed %d keys (%s), last_run_at=%s",
        len(removed), removed, now_iso,
    )


# -- Main ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Retro-attach missing TP coverage for open positions.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without placing orders or modifying files.",
    )
    parser.add_argument(
        "--skip-healthcheck-cleanup",
        action="store_true",
        help="Do not modify data/healthcheck_tp_coverage_state.json.",
    )
    args = parser.parse_args(argv)

    dry_run = args.dry_run
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    if dry_run:
        logger.info("=== DRY-RUN MODE -- no orders placed, no files modified ===")

    logger.warning("FCX SKIPPED -- %s", _FCX_SKIP_REASON)

    try:
        broker = _get_broker()
    except Exception as exc:
        logger.error("Broker connection failed: %s", exc)
        return 2

    results: list[dict[str, Any]] = []
    successfully_fixed: list[str] = []

    for ticker, market_id, shares in _POSITIONS_TO_FIX:
        logger.info("--- Processing %s (%s) ---", ticker, market_id)

        try:
            state_data = _load_state_file(market_id)
        except RuntimeError as exc:
            logger.error("State load failed for %s: %s", market_id, exc)
            results.append({"ticker": ticker, "market": market_id, "status": "STATE_LOAD_FAILED"})
            continue

        positions = state_data.get("positions", [])
        pos_rec = next((p for p in positions if p.get("ticker") == ticker), None)
        if not pos_rec:
            logger.error("Ticker %s not found in %s state file", ticker, market_id)
            results.append({"ticker": ticker, "market": market_id, "status": "NOT_IN_STATE"})
            continue

        existing_stop_order_id = pos_rec.get("stop_order_id", "")
        current_stop_price = float(pos_rec.get("stop_price", 0.0))

        if not existing_stop_order_id:
            logger.error("No stop_order_id in state for %s/%s", ticker, market_id)
            results.append({"ticker": ticker, "market": market_id, "status": "NO_STOP_ORDER_ID"})
            continue

        logger.info(
            "%s: current stop_order_id=%s  stop_price=%.4f",
            ticker, existing_stop_order_id[:8], current_stop_price,
        )

        tp_price = _get_tp_from_db(ticker)
        if tp_price is None:
            logger.error("BLOCKED: no derivable TP price for %s in DB", ticker)
            results.append({"ticker": ticker, "market": market_id, "status": "NO_TP_IN_DB"})
            continue

        logger.info("TP price for %s: %.4f", ticker, tp_price)

        # Cancel existing trailing stop + wait for confirmation
        if dry_run:
            logger.info(
                "[DRY-RUN] Would cancel stop order %s for %s",
                existing_stop_order_id[:8], ticker,
            )
            cancel_ok = True
        else:
            logger.info(
                "Cancelling trailing stop %s for %s...",
                existing_stop_order_id[:8], ticker,
            )
            cancel_ok = _cancel_and_confirm(broker, existing_stop_order_id, ticker)

        if not cancel_ok:
            results.append({
                "ticker": ticker, "market": market_id, "status": "CANCEL_FAILED",
                "stop_price": current_stop_price, "tp_price": tp_price,
            })
            continue

        # Place replacement OCO (static stop + TP limit)
        oco_id = _place_oco(broker, ticker, shares, current_stop_price, tp_price, dry_run)
        if oco_id is None:
            results.append({
                "ticker": ticker, "market": market_id, "status": "OCO_FAILED",
                "stop_price": current_stop_price, "tp_price": tp_price,
            })
            continue

        # Update state file (stop_order_id = tp_order_id = OCO parent)
        state_ok = _update_state_orders(market_id, ticker, oco_id, oco_id, dry_run)

        # Update DB
        db_ok = _update_db_orders(ticker, oco_id, oco_id, dry_run)

        status = "OK" if (state_ok and db_ok) else "PARTIAL"
        if status == "OK":
            successfully_fixed.append(ticker)

        results.append({
            "ticker": ticker,
            "market": market_id,
            "stop_price": current_stop_price,
            "tp_price": tp_price,
            "oco_order_id": oco_id,
            "state_updated": state_ok,
            "db_updated": db_ok,
            "status": status,
        })

    print("\n-- TP Attachment Summary ------------------------------------------")
    for r in results:
        ticker = r["ticker"]
        status = r["status"]
        if status == "OK":
            print(
                f"  OK  {ticker} ({r['market']}): OCO placed  "
                f"stop={r['stop_price']:.2f}  tp={r['tp_price']:.2f}  "
                f"oco_id={r['oco_order_id']}"
            )
        elif status == "PARTIAL":
            print(
                f"  !!  {ticker} ({r['market']}): OCO placed but state/DB update partial  "
                f"oco_id={r.get('oco_order_id','?')}"
            )
        else:
            print(f"  !!  {ticker} ({r['market']}): {status}")

    print("  !!  FCX (commodity_etfs): SKIPPED -- connors_rsi2 has no price-based TP by design")
    print(
        "\n  NOTE: Trailing stops converted to static OCO stops.\n"
        "  Stop will no longer trail up -- current stop price locked in.\n"
        "  Run sync_protective_orders.py to tighten stop further if needed."
    )

    if not args.skip_healthcheck_cleanup:
        logger.info("Cleaning healthcheck state file...")
        _clean_healthcheck_state(successfully_fixed, dry_run)

    all_ok = all(r["status"] in ("OK", "PARTIAL") for r in results)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
