#!/usr/bin/env python3
"""Reset per-market HWM to current live equity values after FIX-PMEQ-001.

Connects to the live Alpaca broker, computes the new per-market equity for
each of (sp500, sector_etfs, commodity_etfs) using the FIX-PMEQ-001 formula
(position MV + live cash attribution via activities API), and writes the
computed values as the day's HWM to:
  - JSON state files: brokers/state/live_{market}.json
  - SQLite market_state table

Usage:
  python3 scripts/reset_per_market_hwm.py          # --dry-run (default)
  python3 scripts/reset_per_market_hwm.py --apply  # write to state files + DB
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, date
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
_ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(_ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATLAS_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("reset_per_market_hwm")

# ── Markets to reset ──────────────────────────────────────────────────────────
_MARKETS = ("sp500", "sector_etfs", "commodity_etfs")

_STATE_DIR = _ATLAS_ROOT / "brokers" / "state"
_DB_PATH = _ATLAS_ROOT / "data" / "atlas.db"


def _get_broker(market_id: str):
    """Connect to the live broker for a given market."""
    from utils.config import get_active_config
    from brokers.registry import get_live_broker

    config = get_active_config(market_id)
    broker = get_live_broker(config)
    if broker is None:
        raise RuntimeError(f"Could not instantiate broker for market {market_id!r}")
    if not broker.connect():
        raise RuntimeError(f"broker.connect() failed for market {market_id!r}")
    return broker


def _get_live_positions(broker, market_id: str) -> list:
    """Fetch live positions from the broker (filtered to market universe)."""
    from universe.membership import derive_universe

    all_positions = broker.get_positions()
    market_positions = []
    for pos in all_positions:
        ticker = pos.ticker if hasattr(pos, "ticker") else getattr(pos, "symbol", None)
        if ticker is None:
            continue
        market = derive_universe(ticker)
        if market == market_id:
            market_positions.append(pos)
    return market_positions


def _build_positions_from_broker(broker, market_id: str):
    """Return simple position mocks compatible with _get_per_market_equity."""
    # Use actual Position objects from broker.get_positions()
    from universe.membership import derive_universe

    all_positions = broker.get_positions()
    market_pos = []
    for pos in all_positions:
        ticker = pos.ticker if hasattr(pos, "ticker") else getattr(pos, "symbol", None)
        if ticker is None:
            continue
        market = derive_universe(ticker)
        if market == market_id:
            market_pos.append(pos)
    return market_pos


def compute_per_market_equity(broker, market_id: str) -> tuple[float | None, bool]:
    """Compute the current per-market equity using the FIX-PMEQ-001 formula.

    Returns (equity, degraded).
    """
    from brokers.live_portfolio import LivePortfolio
    from utils.config import get_active_config

    config = get_active_config(market_id)
    from unittest.mock import patch
    with patch.object(LivePortfolio, "_load_local_state", return_value=None):
        lp = LivePortfolio(config, market_id=market_id)

    # Populate with live broker data
    lp._broker = broker
    lp._broker_equity = broker.get_account_info().equity
    lp.positions = _build_positions_from_broker(broker, market_id)

    # Get live prices from broker positions (use current_price attribute if available)
    prices: dict[str, float] = {}
    for pos in lp.positions:
        ticker = pos.ticker if hasattr(pos, "ticker") else getattr(pos, "symbol", None)
        price = (
            getattr(pos, "current_price", None)
            or getattr(pos, "entry_price", None)
            or 0.0
        )
        if ticker and price:
            prices[ticker] = float(price)

    equity = lp._get_per_market_equity(lp._broker_equity, prices=prices or None)
    degraded = getattr(lp, "_per_market_equity_degraded", False)
    return equity, degraded


def _write_json_hwm(market_id: str, new_hwm: float, today_str: str) -> None:
    """Write new HWM to live_{market}.json state file."""
    state_path = _STATE_DIR / f"live_{market_id}.json"
    if state_path.exists():
        with open(state_path) as f:
            state = json.load(f)
    else:
        state = {}

    state["daily_high_water"] = new_hwm
    state["daily_high_water_date"] = today_str

    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


def _write_sqlite_hwm(market_id: str, new_hwm: float, today_str: str) -> None:
    """Write new HWM to market_state SQLite table."""
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """
        INSERT INTO market_state (market_id, daily_high_water, hwm_date, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(market_id) DO UPDATE SET
            daily_high_water = excluded.daily_high_water,
            hwm_date = excluded.hwm_date,
            updated_at = excluded.updated_at
        """,
        (market_id, new_hwm, today_str, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset per-market HWM to live equity")
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write the new HWM to state files and DB (default: dry-run only)",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    today_str = date.today().isoformat()
    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"\n{'='*60}")
    print(f"reset_per_market_hwm.py  [{mode}]  {today_str}")
    print(f"{'='*60}")

    # Connect once — all markets share the same Alpaca account
    print("\nConnecting to broker…")
    try:
        broker = _get_broker("sp500")
        acct = broker.get_account_info()
        broker_equity = acct.equity
        print(f"✓ Broker connected  equity=${broker_equity:,.2f}")
    except Exception as exc:
        logger.error("Broker connection failed: %s", exc)
        sys.exit(1)

    results: dict[str, dict] = {}
    sum_per_market = 0.0

    for market_id in _MARKETS:
        print(f"\n── {market_id} ──────────────────────────────────────────")
        try:
            per_market_eq, degraded = compute_per_market_equity(broker, market_id)
        except Exception as exc:
            logger.error("compute_per_market_equity failed for %s: %s", market_id, exc)
            results[market_id] = {"error": str(exc)}
            continue

        # Read current HWM from JSON state file
        state_path = _STATE_DIR / f"live_{market_id}.json"
        current_hwm = None
        if state_path.exists():
            try:
                with open(state_path) as f:
                    state = json.load(f)
                current_hwm = state.get("daily_high_water")
            except Exception:
                pass

        status_icon = "⚠️ DEGRADED" if degraded else "✓"
        eq_str = f"${per_market_eq:,.2f}" if per_market_eq is not None else "None"
        hwm_str = f"${current_hwm:,.2f}" if current_hwm is not None else "None"

        print(f"  current_HWM:       {hwm_str}")
        print(f"  per_market_equity: {eq_str}  {status_icon}")
        print(f"  degraded:          {degraded}")

        results[market_id] = {
            "per_market_eq": per_market_eq,
            "current_hwm": current_hwm,
            "degraded": degraded,
        }

        if per_market_eq is not None:
            sum_per_market += per_market_eq

    # Summary
    drift = abs(sum_per_market - broker_equity)
    print(f"\n{'='*60}")
    print(f"Σ per_market_equity:  ${sum_per_market:,.2f}")
    print(f"broker_equity:        ${broker_equity:,.2f}")
    print(f"drift:                ${drift:,.2f}")
    if drift > 50:
        print("⚠️  Drift >$50 — check activities API / universe membership")
    else:
        print("✓  Drift within expected range (timing + small attribution gaps)")

    # Sanity checks
    any_degraded = any(r.get("degraded", False) for r in results.values() if isinstance(r, dict))
    any_none = any(r.get("per_market_eq") is None for r in results.values() if isinstance(r, dict))

    if any_degraded:
        print("\n⚠️  Activities API DEGRADED on at least one market — per-market equity is estimate")
    if any_none:
        print("\n✗  Some markets returned None — do not apply until resolved")
        if not dry_run:
            print("Aborting --apply due to None equity values.")
            sys.exit(1)

    # Write
    if dry_run:
        print(f"\n[DRY-RUN] Would write new HWM values. Re-run with --apply to commit.")
    else:
        print(f"\nApplying new HWM values…")
        applied = []
        for market_id in _MARKETS:
            r = results.get(market_id, {})
            if not isinstance(r, dict) or r.get("per_market_eq") is None:
                print(f"  {market_id}: SKIP (None equity)")
                continue
            new_hwm = r["per_market_eq"]
            try:
                _write_json_hwm(market_id, new_hwm, today_str)
                _write_sqlite_hwm(market_id, new_hwm, today_str)
                print(f"  {market_id}: HWM → ${new_hwm:,.2f}  ✓")
                applied.append(market_id)
            except Exception as exc:
                logger.error("Failed to write HWM for %s: %s", market_id, exc)

        # Telegram notification
        if applied:
            try:
                from utils.telegram import send_message
                lines = ["📊 *Per-market HWM reset* (FIX-PMEQ-001)"]
                for m in applied:
                    r = results[m]
                    old = r.get("current_hwm", 0.0) or 0.0
                    new = r["per_market_eq"]
                    lines.append(f"• {m}: ${old:,.2f} → ${new:,.2f}")
                lines.append(f"Σ = ${sum_per_market:,.2f}  broker = ${broker_equity:,.2f}")
                send_message("\n".join(lines))
                print("\n✓ Telegram notification sent")
            except Exception as exc:
                logger.warning("Telegram notification failed (non-fatal): %s", exc)

    print()


if __name__ == "__main__":
    main()
