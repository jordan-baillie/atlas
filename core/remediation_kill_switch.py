"""Atlas auto-remediation 8-layer kill-switch chain.

Every cycle of error_monitor.py / fix_worker.py / merger.py MUST call
check_all_layers() before doing any work. Returns Optional[BlockReason] —
None = OK to proceed, otherwise the layer that tripped.

Layers are checked top-down (L1 → L8). The cheapest checks come first
to minimize latency on the hot path.

L1: env var ATLAS_AUTO_REMEDIATION_DISABLED=1
L2: file data/AUTO_REMEDIATION_HALT
L3: file data/HALT (trading kill switch — implies remediation halt)
L4: drawdown breach (>5% daily) — read from latest portfolio snapshot
L5: healthcheck cascade (3+ critical healthchecks failing in 24h)
L6: reviewer rejection rate > 50% over last 10 fixes
L7: Telegram /halt-remediation command (sets L2 file — checked via L2)
L8: systemd handles via ConditionPathExists=! (out of band)

systemd unit's ConditionPathExists=! enforces L1-L3 at OS boot — even before
this Python code runs. Defense in depth.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HALT_FILES = (
    PROJECT_ROOT / "data" / "AUTO_REMEDIATION_HALT",   # L2 (highest priority among files)
    PROJECT_ROOT / "data" / "HALT",                     # L3
    PROJECT_ROOT / ".live_halt",                        # L3 alt
)

ENV_DISABLE = "ATLAS_AUTO_REMEDIATION_DISABLED"
DRAWDOWN_HALT_PCT = 5.0           # L4 — 5% daily DD halts remediation
HEALTHCHECK_FAIL_THRESHOLD = 3    # L5 — 3 critical healthchecks failing
REVIEWER_REJECTION_HALT_PCT = 50.0  # L6 — 50% of last 10 reviews rejected

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

    try:
        with _sqlite3.connect(path, timeout=10) as conn:
            conn.row_factory = _sqlite3.Row
            rows = conn.execute(
                """SELECT date, equity
                   FROM equity_history
                   WHERE market_id = 'sp500'
                     AND date >= ?
                   ORDER BY date ASC""",
                (cutoff_date,),
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


def check_l5_healthcheck_cascade(
    *,
    db_path: Optional[str] = None,
    lookback_hours: int = 24,
    threshold: int = HEALTHCHECK_FAIL_THRESHOLD,
) -> Optional[BlockReason]:
    """Count distinct critical healthcheck failures in last N hours."""
    try:
        import sqlite3
        path = db_path or str(PROJECT_ROOT / "data" / "atlas.db")
        with sqlite3.connect(path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
            ).strftime("%Y-%m-%dT%H:%M:%S")
            # Count CRITICAL-level errors from healthcheck source in last 24h
            n = conn.execute(
                """SELECT COUNT(DISTINCT fingerprint) FROM errors
                   WHERE source = 'healthcheck'
                     AND level = 'CRITICAL'
                     AND last_seen_ts >= ?""",
                (cutoff,),
            ).fetchone()[0]
            if n >= threshold:
                return BlockReason(
                    "L5",
                    f"Healthcheck cascade: {n} critical fails in {lookback_hours}h (≥{threshold})",
                    {
                        "distinct_failures": n,
                        "threshold": threshold,
                        "lookback_hours": lookback_hours,
                    },
                )
    except Exception as e:
        logger.warning("L5 healthcheck cascade check failed (fail-open): %s", e)
    return None


def check_l6_reviewer_rejection_rate(
    *,
    db_path: Optional[str] = None,
    lookback_hours: int = 24,
    min_sample_size: int = 10,
    threshold_pct: float = REVIEWER_REJECTION_HALT_PCT,
) -> Optional[BlockReason]:
    """If reviewer rejected > 50% of last 10 fixes, halt."""
    try:
        import sqlite3
        path = db_path or str(PROJECT_ROOT / "data" / "atlas.db")
        with sqlite3.connect(path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
            ).strftime("%Y-%m-%dT%H:%M:%S")
            # Look at fix_attempts where reviewer ran (review_verdict NOT NULL)
            rows = conn.execute(
                """SELECT review_verdict
                   FROM fix_attempts
                   WHERE review_verdict IS NOT NULL
                     AND started_ts >= ?
                   ORDER BY started_ts DESC LIMIT ?""",
                (cutoff, min_sample_size),
            ).fetchall()
            if len(rows) < min_sample_size:
                return None  # Not enough data
            rejected = sum(1 for r in rows if r["review_verdict"] == "REJECT")
            rate = rejected / len(rows) * 100
            if rate > threshold_pct:
                return BlockReason(
                    "L6",
                    f"Reviewer rejection rate {rate:.1f}% > {threshold_pct}% (last {len(rows)} fixes)",
                    {
                        "rejected": rejected,
                        "total_reviewed": len(rows),
                        "rate_pct": round(rate, 2),
                        "threshold_pct": threshold_pct,
                    },
                )
    except Exception as e:
        logger.warning("L6 reviewer rejection rate check failed (fail-open): %s", e)
    return None


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def check_all_layers(*, db_path: Optional[str] = None) -> Optional[BlockReason]:
    """Check all 8 layers in order. L8 is implicit (systemd off); L7 is via L2.

    Returns the FIRST tripped layer, or None if all clear.
    """
    for fn in (
        check_l1_env,
        check_l2_remediation_halt,
        check_l3_trading_halt,
        lambda: check_l4_drawdown(db_path=db_path),
        lambda: check_l5_healthcheck_cascade(db_path=db_path),
        lambda: check_l6_reviewer_rejection_rate(db_path=db_path),
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
