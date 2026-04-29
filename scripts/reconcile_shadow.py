#!/usr/bin/env python3
"""Shadow-mode reconciliation runner for Atlas — Phase B.2.

Runs the new canonical core.reconcile module in dry_run=True mode alongside
the existing reconcile scripts and compares their findings. Persists each run
to the reconcile_shadow_runs table and sends a Telegram alert when the new
module detects divergence (either finds drift the old scripts missed, or misses
drift the old scripts found).

This runner is the validation gate for the 7-day shadow period before cutover.
Zero divergence for 7 consecutive days → safe to cut over and delete old scripts.

Usage:
    python3 scripts/reconcile_shadow.py [--market <m>] [--no-alert] [--once]

    --market sp500|commodity_etfs|sector_etfs   Run for one market only (default: all)
    --no-alert                                  Suppress Telegram alert
    --once                                      Run once and exit (default behaviour)

Cron entry (add manually after merge — see docs/reconcile.md):
    */30 0-7 * * 2-6 /usr/bin/flock -n /tmp/reconcile_shadow.lock \\
        bash -c 'cd /root/atlas && timeout 5m python3 scripts/reconcile_shadow.py \\
        --once --no-alert' >> /root/atlas/logs/reconcile_shadow.log 2>&1

    # With alerts (once stable):
    */30 0-7 * * 2-6 /usr/bin/flock -n /tmp/reconcile_shadow.lock \\
        bash -c 'cd /root/atlas && timeout 5m python3 scripts/reconcile_shadow.py --once' \\
        >> /root/atlas/logs/reconcile_shadow.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ── Project bootstrap ─────────────────────────────────────────────────────────
ATLAS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ATLAS_ROOT))

from atlas_bootstrap import PROJECT_ROOT as PROJECT  # noqa: E402
from utils.logging_config import setup_logging       # noqa: E402

log = setup_logging("reconcile_shadow", extra_log_file="reconcile_shadow")

# ── Constants ─────────────────────────────────────────────────────────────────
_MARKETS = ("sp500", "commodity_etfs", "sector_etfs")
_ALERT_COOLDOWN_H = 6
_ALERT_STATE_FILE = PROJECT / "data" / "reconcile_shadow_alert_state.json"
_LOG_DIR = PROJECT / "logs"

# Reconcile-positions log path (written by reconcile_positions.py)
_RECONCILE_POS_LOG = _LOG_DIR / "reconciliation.log"
# Reconcile-ledger log path (written by reconcile_ledger.py)
_RECONCILE_LED_LOG = _LOG_DIR / "reconcile_ledger.log"


# ═══════════════════════════════════════════════════════════════════════════════
# Log parsing — extract last-run summary from old scripts' logs
# ═══════════════════════════════════════════════════════════════════════════════

def _tail_lines(path: Path, n: int = 200) -> list[str]:
    """Return last *n* lines of *path* as a list. Empty list on error."""
    if not path.exists():
        return []
    try:
        with open(path) as f:
            return f.readlines()[-n:]
    except Exception as exc:
        log.debug("_tail_lines %s: %s", path, exc)
        return []


def _parse_old_positions_drift(market_id: str) -> int:
    """Parse reconciliation.log for the most recent run for *market_id*.

    Returns the discrepancy count (0 = clean, -1 = not found / parse error).

    Scans forward so that header ("market=sp500") is seen before the "done"
    line — which is the order they appear in the log file.
    """
    lines = _tail_lines(_RECONCILE_POS_LOG, 500)

    # Forward scan: track last complete block for this market
    last_result: int = -1
    in_block = False
    for line in lines:
        if f"market={market_id}" in line:
            in_block = True
        if not in_block:
            continue
        m = re.search(r"discrepancies=(True|False)", line)
        if m:
            if m.group(1) == "False":
                last_result = 0
            else:
                last_result = 1  # at least 1; exact count from Telegram msg is harder to parse
            in_block = False  # reset; next block starts on a new header line

    return last_result


def _parse_old_ledger_dirty(market_id: str) -> int:
    """Parse reconcile_ledger.log for the most recent run.

    Returns backfilled + closed_phantom counts (0 = clean, -1 = not found).
    Note: reconcile_ledger.py doesn't filter by market on log lines, so we
    use any run (it operates on all positions globally).
    """
    lines = _tail_lines(_RECONCILE_LED_LOG, 200)
    for line in reversed(lines):
        # "Reconciliation complete: backfilled=0, closed=0, matched=1, errors=0"
        m = re.search(
            r"backfilled=(\d+),\s*closed=(\d+),\s*matched=\d+,\s*errors=(\d+)",
            line,
        )
        if m:
            return int(m.group(1)) + int(m.group(2))
    return -1


# ═══════════════════════════════════════════════════════════════════════════════
# Shadow table persistence
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_shadow_table() -> None:
    """Create reconcile_shadow_runs table if missing (idempotent)."""
    try:
        from db import atlas_db
        with atlas_db.get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reconcile_shadow_runs (
                    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts                     TEXT NOT NULL,
                    market                 TEXT NOT NULL,
                    new_drift_count        INTEGER NOT NULL DEFAULT 0,
                    old_drift_count        INTEGER NOT NULL DEFAULT 0,
                    divergence_count       INTEGER NOT NULL DEFAULT 0,
                    divergence_detail_json TEXT,
                    report_json            TEXT,
                    created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_runs_market_ts "
                "ON reconcile_shadow_runs(market, ts)"
            )
    except Exception as exc:
        log.warning("_ensure_shadow_table failed (non-fatal): %s", exc)


def _persist_run(
    ts: str,
    market: str,
    new_drift: int,
    old_drift: int,
    divergence: int,
    divergence_details: list[str],
    report_fills: dict,
    report_positions: dict,
) -> None:
    """Upsert a reconcile_shadow_runs row. Non-fatal."""
    try:
        from db import atlas_db
        with atlas_db.get_db() as conn:
            conn.execute(
                """INSERT INTO reconcile_shadow_runs
                   (ts, market, new_drift_count, old_drift_count, divergence_count,
                    divergence_detail_json, report_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts, market, new_drift, old_drift, divergence,
                    json.dumps(divergence_details),
                    json.dumps({"fills": report_fills, "positions": report_positions}),
                ),
            )
        log.info("shadow: persisted run for %s (divergence=%d)", market, divergence)
    except Exception as exc:
        log.warning("_persist_run failed (non-fatal): %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
# Alert throttle
# ═══════════════════════════════════════════════════════════════════════════════

def _should_alert(market: str) -> bool:
    """Return True if enough time has passed since the last alert for *market*."""
    try:
        if _ALERT_STATE_FILE.exists():
            state = json.loads(_ALERT_STATE_FILE.read_text())
            last_ts = state.get(market)
            if last_ts:
                last_dt = datetime.fromisoformat(last_ts)
                if datetime.now(timezone.utc) - last_dt < timedelta(hours=_ALERT_COOLDOWN_H):
                    return False
    except Exception:
        pass
    return True


def _record_alert(market: str) -> None:
    """Record that an alert was sent for *market*."""
    try:
        state: dict = {}
        if _ALERT_STATE_FILE.exists():
            state = json.loads(_ALERT_STATE_FILE.read_text())
        state[market] = datetime.now(timezone.utc).isoformat()
        _ALERT_STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as exc:
        log.debug("_record_alert: %s", exc)


def _send_divergence_alert(
    market: str,
    new_drift: int,
    old_drift: int,
    divergence: int,
    details: list[str],
) -> None:
    """Send Telegram alert for divergence. Non-fatal."""
    try:
        from utils.telegram import send_message, tg_escape as _esc
        lines = [
            "🔬 <b>Shadow Reconcile — Divergence Detected</b>",
            f"Market: <b>{_esc(market.upper())}</b>",
            "",
            f"New module drift:  <b>{new_drift}</b>",
            f"Old scripts drift: <b>{old_drift}</b>",
            f"Divergence:        <b>{divergence}</b>",
        ]
        if details:
            lines += ["", "<b>Details:</b>"]
            lines += [f"  • {_esc(d)}" for d in details[:5]]
            if len(details) > 5:
                lines.append(f"  … and {len(details) - 5} more")
        lines += [
            "",
            "<i>Review reconcile_shadow_runs table and "
            "logs/reconcile_shadow.log</i>",
        ]
        send_message("\n".join(lines))
    except Exception as exc:
        log.warning("_send_divergence_alert failed: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
# Core shadow logic for one market
# ═══════════════════════════════════════════════════════════════════════════════

def _shadow_market(
    market_id: str,
    broker,
    send_alert: bool,
) -> dict[str, Any]:
    """Run shadow reconcile for one market. Returns summary dict."""
    from core.reconcile import reconcile_fills, reconcile_positions
    from db import atlas_db

    ts = datetime.now(timezone.utc).isoformat()
    log.info("=" * 60)
    log.info("shadow [%s] START", market_id.upper())

    # ── Run new module (always dry_run) ───────────────────────────────────────
    report_fills = None
    report_pos = None

    try:
        report_fills = reconcile_fills(
            market_id=market_id,
            broker=broker,
            db=atlas_db,
            dry_run=True,
        )
        log.info(
            "shadow [%s] fills: added=%d updated=%d opened=%d closed=%d errors=%d",
            market_id,
            len(report_fills.fills_added), len(report_fills.fills_updated),
            len(report_fills.trades_opened), len(report_fills.trades_closed),
            len(report_fills.errors),
        )
    except Exception as exc:
        log.error("shadow [%s] reconcile_fills CRASHED: %s", market_id, exc, exc_info=True)

    try:
        report_pos = reconcile_positions(
            market_id=market_id,
            broker=broker,
            db=atlas_db,
            dry_run=True,
        )
        log.info(
            "shadow [%s] positions: drift=%d errors=%d",
            market_id, len(report_pos.drift_detected), len(report_pos.errors),
        )
    except Exception as exc:
        log.error("shadow [%s] reconcile_positions CRASHED: %s", market_id, exc, exc_info=True)

    # ── Parse old scripts' results ────────────────────────────────────────────
    old_pos_drift = _parse_old_positions_drift(market_id)
    old_led_dirty = _parse_old_ledger_dirty(market_id)

    # Old total = positions discrepancies + ledger dirty items
    old_pos_count = max(old_pos_drift, 0)
    old_fills_count = max(old_led_dirty, 0)
    old_total = old_pos_count + old_fills_count

    # New total from the new module
    new_pos_drift = len(report_pos.drift_detected) if report_pos else 0
    new_fills_changed = (
        len(report_fills.fills_added) + len(report_fills.trades_opened) + len(report_fills.trades_closed)
        if report_fills else 0
    )
    new_total = new_pos_drift + new_fills_changed

    divergence = abs(new_total - old_total)

    # Build divergence detail strings
    divergence_details: list[str] = []
    if report_pos and report_pos.drift_detected:
        for d in report_pos.drift_detected:
            divergence_details.append(
                f"[NEW] {d['type']} {d['ticker']}: {d['details'][:80]}"
            )
    if old_pos_drift == -1:
        divergence_details.append(
            f"[OLD] reconcile_positions log not parseable for {market_id}"
        )
    if old_led_dirty == -1:
        divergence_details.append(
            f"[OLD] reconcile_ledger log not parseable"
        )
    if new_total > old_total:
        divergence_details.append(
            f"New module found {new_total - old_total} MORE items than old scripts"
        )
    elif new_total < old_total:
        divergence_details.append(
            f"New module found {old_total - new_total} FEWER items than old scripts"
        )

    log.info(
        "shadow [%s] comparison: new_total=%d old_total=%d divergence=%d",
        market_id, new_total, old_total, divergence,
    )

    # ── Persist to DB ─────────────────────────────────────────────────────────
    _persist_run(
        ts=ts,
        market=market_id,
        new_drift=new_total,
        old_drift=old_total,
        divergence=divergence,
        divergence_details=divergence_details,
        report_fills=report_fills.summary() if report_fills else {},
        report_positions=report_pos.summary() if report_pos else {},
    )

    # ── Alert on divergence ───────────────────────────────────────────────────
    if divergence > 0 and send_alert:
        if _should_alert(market_id):
            _send_divergence_alert(
                market=market_id,
                new_drift=new_total,
                old_drift=old_total,
                divergence=divergence,
                details=divergence_details,
            )
            _record_alert(market_id)
            log.info("shadow [%s]: Telegram alert sent (divergence=%d)", market_id, divergence)
        else:
            log.info(
                "shadow [%s]: divergence=%d but alert throttled (%dh cooldown)",
                market_id, divergence, _ALERT_COOLDOWN_H,
            )

    return {
        "market": market_id,
        "ts": ts,
        "new_total": new_total,
        "old_total": old_total,
        "divergence": divergence,
        "divergence_details": divergence_details,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=_MARKETS, default=None,
                        help="Restrict to one market (default: all)")
    parser.add_argument("--no-alert", action="store_true",
                        help="Suppress Telegram divergence alert")
    parser.add_argument("--once", action="store_true",
                        help="Run once and exit (default)")
    args = parser.parse_args(argv)

    markets = [args.market] if args.market else list(_MARKETS)
    send_alert = not args.no_alert

    log.info("reconcile_shadow START: markets=%s send_alert=%s", markets, send_alert)

    # ── Ensure shadow table exists ────────────────────────────────────────────
    _ensure_shadow_table()

    # ── Connect to broker once; reuse across markets ──────────────────────────
    broker = None
    try:
        from brokers.registry import get_live_broker
        from utils.config import get_active_config
        # Use sp500 config for broker connection (all markets share the same Alpaca account)
        config = get_active_config("sp500")
        broker = get_live_broker(config)
        if not broker or not broker.connect():
            log.error("reconcile_shadow: cannot connect to broker — aborting")
            return 1
        log.info("reconcile_shadow: broker connected")
    except Exception as exc:
        log.error("reconcile_shadow: broker setup failed: %s", exc)
        return 1

    results = []
    try:
        for market in markets:
            try:
                result = _shadow_market(market, broker, send_alert=send_alert)
                results.append(result)
            except Exception as exc:
                log.error("shadow [%s] FAILED: %s", market, exc, exc_info=True)
                results.append({"market": market, "error": str(exc)})
    finally:
        try:
            broker.disconnect()
        except Exception:
            pass

    # ── Print summary ─────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  SHADOW RECONCILE SUMMARY")
    print("=" * 60)
    any_divergence = False
    for r in results:
        if "error" in r:
            print(f"  {r['market'].upper()}: ERROR — {r['error']}")
            continue
        div = r.get("divergence", 0)
        icon = "⚠️ " if div > 0 else "✅"
        print(
            f"  {icon} {r['market'].upper():20s}  "
            f"new={r['new_total']:3d}  old={r['old_total']:3d}  div={div:3d}"
        )
        if div > 0:
            any_divergence = True
            for detail in r.get("divergence_details", [])[:3]:
                print(f"       {detail}")
    print()

    log.info("reconcile_shadow COMPLETE: any_divergence=%s", any_divergence)
    return 0


if __name__ == "__main__":
    sys.exit(main())
