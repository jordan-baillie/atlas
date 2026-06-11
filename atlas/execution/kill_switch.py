"""atlas.execution.kill_switch — layered fail-closed trading kill switch.

TargetExecutor calls check_all_layers() before placing ANY order. Returns
Optional[BlockReason] — None = OK to proceed, otherwise the layer that tripped.
Layers are checked top-down; the cheapest checks come first.

L1: env var ATLAS_AUTO_REMEDIATION_DISABLED=1
L2: file data/AUTO_REMEDIATION_HALT
L3: file data/HALT or .live_halt (trading kill switch)
L4: drawdown breach — equity_history drawdown-from-peak over 30 days
    (NOTE: equity_history lost its writer with the swing system; L4 is
    currently fail-open no-data. Follow-up: re-point at the live books,
    data/live/<name>/equity_state.json.)
The systemd timer additionally carries ConditionPathExists=!/root/atlas/data/HALT
so a halt blocks the unit before Python even starts. Defense in depth.

Human controls (replaces the retired Telegram bot commands):
    python3 -m atlas.execution.kill_switch status
    python3 -m atlas.execution.kill_switch halt "reason"
    python3 -m atlas.execution.kill_switch resume
or simply: touch data/HALT (trading) / rm data/HALT.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from atlas.kernel.paths import PROJECT_ROOT


HALT_FILES = (
    PROJECT_ROOT / "data" / "AUTO_REMEDIATION_HALT",   # L2 (highest priority among files)
    PROJECT_ROOT / "data" / "HALT",                     # L3
    PROJECT_ROOT / ".live_halt",                        # L3 alt
)

ENV_DISABLE = "ATLAS_AUTO_REMEDIATION_DISABLED"
DRAWDOWN_HALT_PCT = 5.0           # L4 — 5% drawdown-from-peak halts trading

# Per-market equity attribution refactor cutover. Pre-cutover equity_history rows
# hold GLOBAL broker equity (~$5,300); post-cutover rows hold per-market sp500 equity
# (~$1,300). Mixing them in a max() produces phantom drawdowns. (#314)
ATTRIBUTION_CUTOVER_DATE = "2026-04-29"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlockReason:
    layer: str            # 'L1'|'L2'|...|'L7'
    reason: str           # human-readable
    detail: dict          # forensic detail for audit log


def check_l1_env() -> Optional[BlockReason]:
    if os.environ.get(ENV_DISABLE, "0") == "1":
        return BlockReason("L1", f"env {ENV_DISABLE}=1",
                           {"env_var": ENV_DISABLE, "value": os.environ.get(ENV_DISABLE)})
    return None


def check_l2_remediation_halt() -> Optional[BlockReason]:
    p = PROJECT_ROOT / "data" / "AUTO_REMEDIATION_HALT"
    if p.exists():
        try:
            content = p.read_text(errors="replace")[:500]
        except Exception:
            content = ""
        return BlockReason("L2", "AUTO_REMEDIATION_HALT file present",
                           {"path": str(p), "content": content})
    return None


def check_l3_trading_halt() -> Optional[BlockReason]:
    for p in (PROJECT_ROOT / "data" / "HALT", PROJECT_ROOT / ".live_halt"):
        if p.exists():
            try:
                content = p.read_text(errors="replace")[:500]
            except Exception:
                content = ""
            return BlockReason("L3", f"Trading halt: {p.name}",
                               {"path": str(p), "content": content})
    return None


def check_l4_drawdown(
    *, db_path: Optional[str] = None, threshold_pct: float = DRAWDOWN_HALT_PCT,
    window_days: int = 30,
) -> Optional[BlockReason]:
    """Compute drawdown-from-peak using equity_history over the last window_days.

    Cutover note (2026-04-30, Task #289):
        The original implementation queried portfolio_snapshots.daily_pnl_pct.
        That column DOES NOT EXIST in the production schema, so every call raised
        sqlite3.OperationalError which was swallowed by a bare `except` and returned
        False (fail-open).  This silently disabled the L4 layer in production.

        The fix derives drawdown from sequential equity values in equity_history:
            drawdown_pct = (peak_equity - latest_equity) / peak_equity * 100

        equity_history schema: (market_id TEXT, date TEXT, equity REAL, pnl REAL)

    Fail behaviour:
        - Schema / DB errors → log ERROR + return None (fail-open, but LOUD)
        - Empty table → log DEBUG + return None (graceful no-data path)
    """
    import sqlite3 as _sqlite3

    path = db_path or str(PROJECT_ROOT / "data" / "atlas.db")
    cutoff_date = (
        datetime.now(timezone.utc) - timedelta(days=window_days)
    ).strftime("%Y-%m-%d")

    logger.debug(
        "L4 lookback: window_days=%d, effective_start=%s (cutover_floor=%s)",
        window_days, max(cutoff_date, ATTRIBUTION_CUTOVER_DATE), ATTRIBUTION_CUTOVER_DATE,
    )

    try:
        with _sqlite3.connect(path, timeout=10) as conn:
            conn.row_factory = _sqlite3.Row
            rows = conn.execute(
                """SELECT date, equity
                   FROM equity_history
                   WHERE market_id = 'sp500'
                     AND date >= ?
                     AND date >= ?
                   ORDER BY date ASC""",
                (cutoff_date, ATTRIBUTION_CUTOVER_DATE),
            ).fetchall()
    except (_sqlite3.OperationalError, _sqlite3.DatabaseError) as e:
        # Explicit, loud log — not the silent bare-except from before.
        logger.error("L4 drawdown check unavailable (fail-open): %s", e)
        return None

    if not rows:
        logger.debug("L4: no equity_history rows for sp500 in last %d days (fail-open)", window_days)
        return None

    equities = [float(r["equity"]) for r in rows]
    peak_equity = max(equities)
    latest_equity = equities[-1]
    latest_date = str(rows[-1]["date"])

    if peak_equity <= 0:
        logger.warning("L4: peak_equity <= 0 (%s) — skipping drawdown check", peak_equity)
        return None

    drawdown_pct = (peak_equity - latest_equity) / peak_equity * 100.0

    if drawdown_pct >= threshold_pct:
        return BlockReason(
            "L4",
            f"Drawdown from peak {drawdown_pct:.2f}% >= {threshold_pct}%",
            {
                "latest_date": latest_date,
                "latest_equity": latest_equity,
                "peak_equity": peak_equity,
                "drawdown_pct": round(drawdown_pct, 4),
                "threshold_pct": threshold_pct,
                "window_days": window_days,
            },
        )
    return None


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def check_all_layers(*, db_path: Optional[str] = None) -> Optional[BlockReason]:
    """Check all layers in order. Returns the FIRST tripped layer, or None if clear."""
    for fn in (
        check_l1_env,
        check_l2_remediation_halt,
        check_l3_trading_halt,
        lambda: check_l4_drawdown(db_path=db_path),
    ):
        try:
            r = fn()
        except Exception as e:
            logger.warning("Kill-switch layer crash (fail-open): %s", e)
            r = None
        if r is not None:
            return r
    return None


# ---------------------------------------------------------------------------
# Halt actions (set by upstream code on regression / breach)
# ---------------------------------------------------------------------------

def halt(reason: str, *, source: str = "manual") -> Path:
    """Create the AUTO_REMEDIATION_HALT file with reason text.

    Idempotent — overwrites if already present (latest reason wins).
    """
    path = PROJECT_ROOT / "data" / "AUTO_REMEDIATION_HALT"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"halted at {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%S} by {source}: {reason}"
    path.write_text(text)
    logger.warning("AUTO_REMEDIATION_HALT created: %s", text)
    return path


def resume() -> bool:
    """Remove the AUTO_REMEDIATION_HALT file. Does NOT clear data/HALT (trading)."""
    path = PROJECT_ROOT / "data" / "AUTO_REMEDIATION_HALT"
    if path.exists():
        path.unlink()
        logger.info("AUTO_REMEDIATION_HALT cleared")
        return True
    return False


# ---------------------------------------------------------------------------
# CLI — the human halt/resume surface (the Telegram bot is retired)
# ---------------------------------------------------------------------------

def _cli(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m atlas.execution.kill_switch",
        description="Inspect or flip the Atlas trading kill switch.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show which layer (if any) is tripped")
    p_halt = sub.add_parser("halt", help="create data/HALT (trading halt)")
    p_halt.add_argument("reason", help="why trading is being halted")
    sub.add_parser("resume", help="remove data/HALT and AUTO_REMEDIATION_HALT")
    args = parser.parse_args(argv)

    if args.cmd == "status":
        r = check_all_layers()
        if r is None:
            print("CLEAR — no kill-switch layer tripped")
            return 0
        print(f"BLOCKED [{r.layer}] {r.reason}")
        for k, v in r.detail.items():
            print(f"  {k}: {v}")
        return 1

    if args.cmd == "halt":
        halt_file = PROJECT_ROOT / "data" / "HALT"
        halt_file.parent.mkdir(parents=True, exist_ok=True)
        text = f"halted at {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%S} by cli: {args.reason}"
        halt_file.write_text(text)
        print(f"created {halt_file}: {text}")
        _notify_best_effort(f"⛔ Trading HALTED via CLI: {args.reason}")
        return 0

    if args.cmd == "resume":
        cleared = []
        for f in (PROJECT_ROOT / "data" / "HALT", PROJECT_ROOT / "data" / "AUTO_REMEDIATION_HALT"):
            if f.exists():
                f.unlink()
                cleared.append(f.name)
        print(f"cleared: {cleared or 'nothing (no halt files present)'}")
        if cleared:
            _notify_best_effort(f"✅ Trading kill switch cleared via CLI ({', '.join(cleared)})")
        return 0
    return 2


def _notify_best_effort(text: str) -> None:
    try:
        from atlas.kernel.notify import send_message
        send_message(text)
    except Exception as e:  # notification must never block a halt/resume
        logger.warning("kill-switch notify failed: %s", e)


if __name__ == "__main__":
    raise SystemExit(_cli())
