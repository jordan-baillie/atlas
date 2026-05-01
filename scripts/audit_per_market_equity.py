#!/usr/bin/env python3
"""Sanity audit: per-market equity sum reconciliation + drift detection.

Run anytime to verify per-market attribution is healthy:
  python3 scripts/audit_per_market_equity.py

Checks:
  1. Latest snapshot per market — sum vs broker_equity
  2. Snapshot freshness (no market >3 days stale)
  3. State-file ghost detection (cross-market positions)
  4. Universe-membership drift (positions outside their market's universe def)
  5. HWM consistency across markets

Exits 0 if clean, 1 if drift > $20 OR any state-file ghost.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

# Bootstrap sys.path so local modules resolve
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DRIFT_THRESHOLD = 20.0   # dollars — fail if sum(allocated) vs broker_eq drifts more than this
_STALE_DAYS = 3            # days — fail if any market snapshot is older than this
_STATE_DIR = _PROJECT_ROOT / "brokers" / "state"

# ──────────────────────────────────────────────────────────────────────────────
# Helper
# ──────────────────────────────────────────────────────────────────────────────

def _load_state_file(market_id: str) -> dict:
    path = _STATE_DIR / f"live_{market_id}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s: %s", path.name, exc)
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# Check 1: Snapshot sum reconciliation
# ──────────────────────────────────────────────────────────────────────────────

def check_snapshot_reconciliation() -> tuple[bool, str]:
    """Return (pass, report_text)."""
    try:
        from db.atlas_db import get_db
        with get_db() as db:
            rows = db.execute(
                """
                SELECT market_id, allocated_equity, position_mv, cash_attributed,
                       broker_equity, broker_cash, date, snapshot_time
                FROM market_equity_history
                WHERE date = (SELECT MAX(date) FROM market_equity_history)
                ORDER BY market_id
                """
            ).fetchall()
    except Exception as exc:
        return False, f"DB read failed: {exc}"

    if not rows:
        return False, "No rows in market_equity_history — snapshot never written"

    snap_date = rows[0]["date"]
    broker_eq = rows[0]["broker_equity"] or 0.0
    total_alloc = sum(r["allocated_equity"] or 0.0 for r in rows)
    drift = abs(total_alloc - broker_eq)

    lines = [f"Snapshot date: {snap_date}"]
    lines.append(f"  broker_equity (from snapshot): ${broker_eq:.2f}")
    for r in rows:
        lines.append(
            f"  {r['market_id']}: allocated=${r['allocated_equity']:.2f}  "
            f"(pos_mv=${r['position_mv']:.2f}  cash=${r['cash_attributed']:.2f})"
        )
    lines.append(f"  sum(allocated_equity): ${total_alloc:.2f}")
    lines.append(f"  drift = ${drift:.2f} {'✓' if drift <= _DRIFT_THRESHOLD else '✗ EXCEEDS THRESHOLD'}")

    ok = drift <= _DRIFT_THRESHOLD
    return ok, "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Check 2: Snapshot freshness
# ──────────────────────────────────────────────────────────────────────────────

def check_snapshot_freshness() -> tuple[bool, str]:
    """Return (pass, report_text)."""
    try:
        from db.atlas_db import get_db
        with get_db() as db:
            rows = db.execute(
                """
                SELECT market_id, MAX(date) AS latest_date
                FROM market_equity_history
                GROUP BY market_id
                """
            ).fetchall()
    except Exception as exc:
        return False, f"DB read failed: {exc}"

    today = date.today()
    lines = []
    all_ok = True
    for r in rows:
        snap_date_str = r["latest_date"]
        try:
            snap_d = date.fromisoformat(snap_date_str)
            days_old = (today - snap_d).days
        except (ValueError, TypeError):
            days_old = 9999
        stale = days_old > _STALE_DAYS
        if stale:
            all_ok = False
        lines.append(
            f"  {r['market_id']}: latest={snap_date_str}  "
            f"({days_old}d old)  {'✓' if not stale else '✗ STALE'}"
        )
    return all_ok, "Snapshot freshness:\n" + "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Check 3: State-file ghost detection
# ──────────────────────────────────────────────────────────────────────────────

def check_state_file_ghosts() -> tuple[bool, str]:
    """Return (pass, report_text).

    A ghost is a position in live_X.json whose canonical universe ≠ X.
    """
    try:
        from universe.membership import check_state_file_universes, clear_cache
        clear_cache()
        violations = check_state_file_universes(_STATE_DIR)
    except Exception as exc:
        return False, f"check_state_file_universes failed: {exc}"

    if not violations:
        return True, "State-file ghosts: NONE ✓"

    lines = [f"State-file ghosts: {len(violations)} FOUND ✗"]
    for v in violations:
        lines.append(
            f"  {v['ticker']} in {v['file']} (market={v['market_id']}) "
            f"but canonical universe={v['canonical_universe']}"
        )
    return False, "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Check 4: Universe-membership drift
# ──────────────────────────────────────────────────────────────────────────────

def check_universe_membership_drift() -> tuple[bool, str]:
    """Return (pass, report_text).

    Drift = a position held by the broker whose ticker is NOT in the
    universe definition for that market.  (It was added historically but
    universe def may have shrunk.)
    """
    try:
        from universe.definitions import UNIVERSES
        from universe.builder import get_universe_tickers
        from universe.membership import derive_universe, clear_cache
        clear_cache()
    except Exception as exc:
        return True, f"Universe check skipped (import error): {exc}"

    markets = ["sp500", "sector_etfs", "commodity_etfs"]
    drift: list[str] = []

    for market in markets:
        state = _load_state_file(market)
        positions = state.get("positions", [])
        # Get universe tickers for this market
        try:
            if UNIVERSES.get(market, {}).get("method") == "static":
                universe_tickers = set(UNIVERSES[market].get("tickers", []))
            else:
                universe_tickers = set(get_universe_tickers(market))
        except Exception as exc:
            drift.append(f"  {market}: universe load failed — {exc}")
            continue

        for pos in positions:
            ticker = pos.get("ticker", "")
            if not ticker:
                continue
            canonical = derive_universe(ticker)
            if canonical != market:
                drift.append(
                    f"  {ticker} in {market} state: canonical={canonical} "
                    f"(cross-market attribution OK, research/sweeps may miss it)"
                )
            elif ticker not in universe_tickers:
                drift.append(
                    f"  {ticker} in {market} state: NOT in universe definition "
                    f"(universe shrank after position was opened)"
                )

    if not drift:
        return True, "Universe-membership drift: NONE ✓"
    return True, "Universe-membership drift (non-fatal):\n" + "\n".join(drift)


# ──────────────────────────────────────────────────────────────────────────────
# Check 5: HWM consistency
# ──────────────────────────────────────────────────────────────────────────────

def check_hwm_consistency() -> tuple[bool, str]:
    """Return (pass, report_text).

    Checks:
    - HWM not None (should be set from starting_equity at minimum)
    - HWM not > 5× starting_equity (would have been set from global broker equity)
    - daily_high_water_date is today or None (None triggers a HWM reset, which is safe)
    """
    import json

    today_str = date.today().isoformat()
    lines = []
    all_ok = True

    try:
        configs_dir = _PROJECT_ROOT / "config" / "active"
        for market in ["sp500", "sector_etfs", "commodity_etfs"]:
            state_path = _STATE_DIR / f"live_{market}.json"
            cfg_path = configs_dir / f"{market}.json"
            if not state_path.exists():
                lines.append(f"  {market}: state file MISSING ✗")
                all_ok = False
                continue

            state = json.loads(state_path.read_text())
            hwm = state.get("daily_high_water", 0.0)
            hwm_date = state.get("daily_high_water_date")
            halted = state.get("halted", False)

            starting_equity = 5000.0
            try:
                cfg = json.loads(cfg_path.read_text())
                starting_equity = cfg.get("risk", {}).get("starting_equity", 5000.0)
            except Exception:
                pass

            issues = []
            if hwm is None:
                issues.append("HWM is None")
            elif hwm > starting_equity * 5:
                issues.append(f"HWM ${hwm:.2f} > 5× starting_equity ${starting_equity:.2f} (stale global HWM?)")
                all_ok = False

            if hwm_date is None:
                issues.append("hwm_date=None (will reset on next drawdown check — safe)")
            elif hwm_date != today_str:
                issues.append(f"hwm_date={hwm_date} (not today, will reset — OK)")

            if halted:
                issues.append("⚠️  market is HALTED")

            status = "✓" if not issues or all(i.startswith("hwm_date") or "safe" in i for i in issues) else "✗"
            lines.append(
                f"  {market}: HWM=${hwm:.2f}  date={hwm_date}  "
                f"halted={halted}  {'|'.join(issues) if issues else 'OK'} {status}"
            )
    except Exception as exc:
        return False, f"HWM check failed: {exc}"

    return all_ok, "HWM consistency:\n" + "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 60)
    print(f"Per-market equity audit  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
    print("=" * 60)

    overall_pass = True
    hard_failures: list[str] = []

    checks = [
        ("1. Snapshot reconciliation", check_snapshot_reconciliation, True),   # hard fail
        ("2. Snapshot freshness",      check_snapshot_freshness,      False),  # soft
        ("3. State-file ghosts",       check_state_file_ghosts,       True),   # hard fail
        ("4. Universe-membership drift", check_universe_membership_drift, False),  # soft
        ("5. HWM consistency",         check_hwm_consistency,         False),  # soft
    ]

    for name, fn, is_hard in checks:
        print(f"\n── {name} ──")
        try:
            ok, report = fn()
        except Exception as exc:
            ok = False
            report = f"CHECK ERRORED: {exc}"
        print(report)
        if not ok and is_hard:
            overall_pass = False
            hard_failures.append(name)

    print("\n" + "=" * 60)
    if overall_pass:
        print("RESULT: PASS ✓  (no hard failures)")
    else:
        print(f"RESULT: FAIL ✗  hard failures: {hard_failures}")
    print("=" * 60)

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
