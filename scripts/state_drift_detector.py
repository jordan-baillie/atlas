#!/usr/bin/env python3
"""
state_drift_detector.py — JSON-vs-SQLite open-position drift detector.

For each tracked market (sp500, commodity_etfs, sector_etfs):
  1. Load brokers/state/live_<market>.json  → JSON positions
  2. Query trades WHERE universe=<market> AND status='open'  → SQLite positions
  3. Compare for drift:
       • ticker in JSON but not SQLite  → orphan_in_json
       • ticker in SQLite but not JSON  → orphan_in_sqlite
       • ticker in both but values differ → value_drift

This is OBSERVATIONAL ONLY. No auto-fix is applied.

Exit codes:
    0 — no drift detected
    1 — drift detected (alerts sent unless --no-alert)

Usage:
    python3 scripts/state_drift_detector.py                # run once, alert on drift
    python3 scripts/state_drift_detector.py --no-alert     # run once, print only
    python3 scripts/state_drift_detector.py --json         # machine-readable JSON output
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ATLAS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ATLAS_ROOT))

# Lazy import for Telegram (non-fatal if not available in tests)
try:
    from utils.telegram import send_message, tg_escape  # noqa: E402
    _TELEGRAM_AVAILABLE = True
except ImportError:
    _TELEGRAM_AVAILABLE = False

DB_PATH = ATLAS_ROOT / "data" / "atlas.db"
STATE_DIR = ATLAS_ROOT / "brokers" / "state"
COOLDOWN_FILE = ATLAS_ROOT / "data" / "drift_alert_cooldown.json"

MARKETS = ["sp500", "commodity_etfs", "sector_etfs"]

# Fields compared between JSON and SQLite (field_in_json → column_in_db)
COMPARE_FIELDS: list[tuple[str, str]] = [
    ("entry_price", "entry_price"),
    ("shares",      "shares"),
    ("stop_price",  "stop_price"),
]

# How long to suppress repeat Telegram alerts (in seconds)
COOLDOWN_SECONDS = 6 * 3600  # 6 hours

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_json_positions(market: str) -> dict[str, dict[str, Any]]:
    """Return {ticker: position_dict} for a market's JSON state file."""
    path = STATE_DIR / f"live_{market}.json"
    if not path.exists():
        logger.warning("State file not found: %s — treating as empty", path)
        return {}
    with path.open() as f:
        data = json.load(f)
    positions = data.get("positions", [])
    if isinstance(positions, list):
        return {p["ticker"]: p for p in positions if isinstance(p, dict) and "ticker" in p}
    if isinstance(positions, dict):
        return positions
    logger.warning("Unexpected positions type in %s: %s", path, type(positions))
    return {}


def _load_sqlite_positions(market: str, db_path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Return {ticker: row_dict} for open trades in the given universe."""
    path = str(db_path or DB_PATH)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT ticker, entry_price, shares, stop_price, take_profit "
            "FROM trades WHERE universe=? AND status='open'",
            (market,),
        ).fetchall()
        return {r["ticker"]: dict(r) for r in rows}
    except sqlite3.OperationalError as exc:
        logger.warning("SQLite query failed for %s: %s", market, exc)
        return {}
    finally:
        conn.close()


# ── Drift comparison ──────────────────────────────────────────────────────────

class DriftRecord:
    """Represents one discovered drift event."""

    def __init__(
        self,
        market: str,
        ticker: str,
        reason: str,
        sqlite_data: dict[str, Any] | None,
        json_data: dict[str, Any] | None,
        field_diffs: list[tuple[str, Any, Any]] | None = None,
    ) -> None:
        self.market = market
        self.ticker = ticker
        self.reason = reason
        self.sqlite_data = sqlite_data
        self.json_data = json_data
        self.field_diffs = field_diffs or []

    def to_text(self) -> str:
        lines = [f"DRIFT: {self.market} ticker={self.ticker}"]
        lines.append(f"  reason: {self.reason}")
        if self.sqlite_data:
            e = self.sqlite_data.get("entry_price")
            sh = self.sqlite_data.get("shares")
            st = self.sqlite_data.get("stop_price")
            tp = self.sqlite_data.get("take_profit")
            lines.append(
                f"  sqlite: entry={e} shares={sh} stop={st} tp={tp if tp is not None else 'NULL'}"
            )
        else:
            lines.append("  sqlite: <not present>")
        if self.json_data:
            e = self.json_data.get("entry_price")
            sh = self.json_data.get("shares")
            st = self.json_data.get("stop_price")
            tp = self.json_data.get("take_profit")
            lines.append(
                f"  json:   entry={e} shares={sh} stop={st} tp={tp if tp is not None else 'NULL'}"
            )
        else:
            lines.append("  json:   <not present>")
        if self.field_diffs:
            for field, jv, sv in self.field_diffs:
                lines.append(f"  field {field}: json={jv}  sqlite={sv}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "ticker": self.ticker,
            "reason": self.reason,
            "sqlite": self.sqlite_data,
            "json": self.json_data,
            "field_diffs": [
                {"field": f, "json_value": jv, "sqlite_value": sv}
                for f, jv, sv in self.field_diffs
            ],
        }


def _compare_market(
    market: str, db_path: Path | str | None = None
) -> list[DriftRecord]:
    """Compare JSON vs SQLite for one market, return list of drift records."""
    json_pos = _load_json_positions(market)
    sqlite_pos = _load_sqlite_positions(market, db_path=db_path)

    drifts: list[DriftRecord] = []
    all_tickers = set(json_pos) | set(sqlite_pos)

    for ticker in sorted(all_tickers):
        j = json_pos.get(ticker)
        s = sqlite_pos.get(ticker)

        if j is not None and s is None:
            drifts.append(
                DriftRecord(
                    market=market,
                    ticker=ticker,
                    reason="orphan in JSON (not in SQLite)",
                    sqlite_data=None,
                    json_data=j,
                )
            )
        elif s is not None and j is None:
            drifts.append(
                DriftRecord(
                    market=market,
                    ticker=ticker,
                    reason="orphan in SQLite (not in JSON)",
                    sqlite_data=s,
                    json_data=None,
                )
            )
        else:
            # Both present — compare fields
            field_diffs: list[tuple[str, Any, Any]] = []
            for json_field, db_col in COMPARE_FIELDS:
                jv = j.get(json_field)  # type: ignore[union-attr]
                sv = s.get(db_col)      # type: ignore[union-attr]
                # Normalize None equivalents
                jv_norm = None if jv is None else float(jv) if isinstance(jv, (int, float)) else jv
                sv_norm = None if sv is None else float(sv) if isinstance(sv, (int, float)) else sv
                if jv_norm != sv_norm:
                    field_diffs.append((json_field, jv, sv))
            if field_diffs:
                drifts.append(
                    DriftRecord(
                        market=market,
                        ticker=ticker,
                        reason=f"value drift ({len(field_diffs)} field(s))",
                        sqlite_data=s,
                        json_data=j,
                        field_diffs=field_diffs,
                    )
                )

    return drifts


# ── Alert cooldown ────────────────────────────────────────────────────────────

def _is_in_cooldown(state_file: Path | None = None) -> bool:
    """Return True if within the 6h alert cooldown window."""
    path = state_file or COOLDOWN_FILE
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        last_alert_str = data.get("last_alert_utc")
        if not last_alert_str:
            return False
        last_alert = datetime.fromisoformat(last_alert_str)
        if last_alert.tzinfo is None:
            last_alert = last_alert.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(tz=timezone.utc) - last_alert).total_seconds()
        return elapsed < COOLDOWN_SECONDS
    except Exception as exc:
        logger.warning("Could not read cooldown file: %s", exc)
        return False


def _update_cooldown(state_file: Path | None = None) -> None:
    """Write current timestamp to the cooldown file."""
    path = state_file or COOLDOWN_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"last_alert_utc": datetime.now(tz=timezone.utc).isoformat()})
        )
    except Exception as exc:
        logger.warning("Could not write cooldown file: %s", exc)


# ── Telegram alert ────────────────────────────────────────────────────────────

def _build_alert_text(all_drifts: list[DriftRecord]) -> str:
    """Build a compact Telegram HTML alert."""
    if not _TELEGRAM_AVAILABLE:
        return ""
    lines = [
        f"⚠️ <b>State Drift Detected</b> — {len(all_drifts)} item(s)",
        "",
    ]
    for d in all_drifts[:10]:  # cap at 10 for message length
        market_esc = tg_escape(d.market)
        ticker_esc = tg_escape(d.ticker)
        reason_esc = tg_escape(d.reason)
        lines.append(f"• <b>{market_esc}/{ticker_esc}</b>: {reason_esc}")
    if len(all_drifts) > 10:
        lines.append(f"  … and {len(all_drifts) - 10} more")
    lines += [
        "",
        "SQLite is canonical. JSON is derived cache.",
        "Run: <code>python3 scripts/state_drift_detector.py --no-alert</code>",
    ]
    return "\n".join(lines)


def _send_alert(all_drifts: list[DriftRecord]) -> None:
    if not _TELEGRAM_AVAILABLE:
        logger.warning("Telegram unavailable — skipping alert.")
        return
    text = _build_alert_text(all_drifts)
    try:
        send_message(text)
        logger.info("Telegram alert sent.")
    except Exception as exc:
        logger.warning("Telegram alert failed: %s", exc)


# ── Main ──────────────────────────────────────────────────────────────────────

def run_detection(
    markets: list[str] | None = None,
    no_alert: bool = False,
    as_json: bool = False,
    db_path: Path | str | None = None,
    cooldown_file: Path | None = None,
) -> tuple[list[DriftRecord], int]:
    """
    Run drift detection across all markets.

    Returns (drifts, exit_code) where exit_code is 0 (clean) or 1 (drift).
    Useful for tests that call this directly.
    """
    targets = markets or MARKETS
    all_drifts: list[DriftRecord] = []

    for market in targets:
        drifts = _compare_market(market, db_path=db_path)
        all_drifts.extend(drifts)

    if as_json:
        print(json.dumps({"drifts": [d.to_dict() for d in all_drifts]}, indent=2))
        return all_drifts, (1 if all_drifts else 0)

    if not all_drifts:
        print("✓  No drift detected across all markets.")
        return all_drifts, 0

    # Print human-readable drift report
    print(f"\n{'='*60}")
    print(f"  DRIFT REPORT — {len(all_drifts)} item(s) found")
    print(f"{'='*60}\n")
    for d in all_drifts:
        print(d.to_text())
        print()

    # Telegram alert (with cooldown)
    if not no_alert:
        if _is_in_cooldown(state_file=cooldown_file):
            logger.info("Alert cooldown active — skipping Telegram notification.")
        else:
            _send_alert(all_drifts)
            _update_cooldown(state_file=cooldown_file)

    return all_drifts, 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare JSON state files vs SQLite open trades for drift."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=True,
        help="Run once and exit (default behaviour).",
    )
    parser.add_argument(
        "--no-alert",
        action="store_true",
        default=False,
        help="Skip Telegram alert even if drift is found.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        dest="as_json",
        help="Output machine-readable JSON.",
    )
    args = parser.parse_args(argv)

    _, exit_code = run_detection(no_alert=args.no_alert, as_json=args.as_json)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
