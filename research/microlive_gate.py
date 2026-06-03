"""Staged micro-live gate + code-enforced auto-revert kill-switch (rapid pipeline #419).

Board memo 2026-06-03-rapid-validate-to-live-pipeline (5-0). HARD, code-enforced lines that
are NOT config-tunable (the momentum_breakout failure was a promise, not a control):

  * Micro-live size is hard-capped at min($150, 10% of AUM).
  * A kill-switch auto-reverts to paper on a >=20% tranche drawdown (from peak or initial).
  * Arming live REQUIRES: backtest tier SCREEN/PROMOTE + forward gate PASS + a PASSED drill
    + explicit human confirmation. Any missing -> refused. Backtest evidence alone never arms.
  * The kill-switch must pass a simulated-breach DRILL before any live dollar (run_drill()).

This module never arms live on its own; arm_microlive(confirmed=True) is the only path and
must be called deliberately by a human/operator after the gates pass.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DRILL_MARKER = PROJECT / "data" / ".microlive_drill_passed.json"


# ── Hard limits (code-enforced; do NOT move to config) ──────────────────────────
@dataclass(frozen=True)
class MicroLiveLimits:
    max_usd: float = 150.0          # absolute cap on micro-live capital at risk
    max_aum_pct: float = 0.10       # and never more than 10% of AUM
    drawdown_trip_pct: float = 0.20 # kill-switch trips at 20% drawdown (peak or initial)


LIMITS = MicroLiveLimits()


def microlive_cap(aum: float, limits: MicroLiveLimits = LIMITS) -> float:
    """The hard micro-live size cap for a given AUM = min($cap, pct*AUM)."""
    return round(min(limits.max_usd, max(0.0, aum) * limits.max_aum_pct), 2)


# ── Kill-switch ─────────────────────────────────────────────────────────────────
class KillSwitch:
    """Tracks a micro-live tranche's equity and trips (latching) on a drawdown breach."""

    def __init__(self, initial_equity: float, limits: MicroLiveLimits = LIMITS):
        self.initial = float(initial_equity)
        self.peak = float(initial_equity)
        self.limits = limits
        self.tripped = False
        self.reason: str | None = None

    def update(self, equity: float) -> bool:
        """Feed the latest tranche equity. Returns True once tripped (latches)."""
        if self.tripped:
            return True
        equity = float(equity)
        self.peak = max(self.peak, equity)
        dd_peak = (self.peak - equity) / self.peak if self.peak > 0 else 0.0
        dd_init = (self.initial - equity) / self.initial if self.initial > 0 else 0.0
        worst = max(dd_peak, dd_init)
        if worst >= self.limits.drawdown_trip_pct:
            self.tripped = True
            self.reason = (f"tranche drawdown {worst:.1%} >= {self.limits.drawdown_trip_pct:.0%} "
                           f"(peak={self.peak:.2f}, equity={equity:.2f})")
        return self.tripped


# ── Auto-revert action (disarm live -> paper) ───────────────────────────────────
def revert_to_paper(config_rel: str = "config/active/sp500.json",
                    *, dry_run: bool = True, reason: str = "") -> dict:
    """Disarm live trading: set trading.mode=paper, live_enabled=false, auto_approve=false.

    dry_run=True (default) returns the plan without writing — used by the drill.
    """
    path = PROJECT / config_rel
    try:
        cfg = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        return {"would_disarm": True, "ok": False, "error": f"cannot read config: {exc}",
                "dry_run": dry_run}
    trading = cfg.get("trading", {})
    current = {"mode": trading.get("mode"), "live_enabled": trading.get("live_enabled"),
               "auto_approve": trading.get("auto_approve")}
    plan = {"would_disarm": True, "config": str(path), "from": current,
            "to": {"mode": "paper", "live_enabled": False, "auto_approve": False},
            "reason": reason, "dry_run": dry_run}
    if dry_run:
        return plan
    # Real disarm: backup + write.
    try:
        bak = path.with_suffix(f".json.bak-killswitch-{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        bak.write_text(path.read_text())
        cfg.setdefault("trading", {}).update({"mode": "paper", "live_enabled": False,
                                              "auto_approve": False})
        cfg["_killswitch_note"] = f"AUTO-REVERTED to paper {datetime.now(timezone.utc).isoformat()}: {reason}"
        path.write_text(json.dumps(cfg, indent=2))
        plan["ok"] = True
        plan["backup"] = str(bak)
    except Exception as exc:  # noqa: BLE001
        plan["ok"] = False
        plan["error"] = str(exc)
    return plan


# ── Drill (must pass before any live dollar) ────────────────────────────────────
def run_drill(record: bool = True) -> dict:
    """Simulate a breach and prove the kill-switch trips + the revert action fires (dry).

    Writes a drill-passed marker so arm_microlive() can require a recent successful drill.
    """
    ks = KillSwitch(100.0)
    # Equity path that draws down ~22% from peak.
    path = [100.0, 100.5, 101.0, 96.0, 90.0, 82.0, 78.0]
    tripped_at = None
    for i, eq in enumerate(path):
        if ks.update(eq):
            tripped_at = i
            break
    revert = revert_to_paper(dry_run=True, reason="DRILL")
    passed = bool(ks.tripped and tripped_at is not None and revert.get("would_disarm"))
    # Also assert it does NOT trip on a benign path.
    ks2 = KillSwitch(100.0)
    benign = any(ks2.update(eq) for eq in [100, 101, 99.5, 100.2, 101.5])
    passed = passed and (not benign)
    result = {"passed": passed, "tripped_at_step": tripped_at, "trip_reason": ks.reason,
              "false_trip_on_benign": benign, "revert_plan_ok": revert.get("would_disarm"),
              "ts": datetime.now(timezone.utc).isoformat()}
    if record and passed:
        try:
            DRILL_MARKER.parent.mkdir(parents=True, exist_ok=True)
            DRILL_MARKER.write_text(json.dumps(result, indent=2))
        except Exception:
            pass
    return result


def drill_recent(max_age_sec: int = 30 * 86400) -> bool:
    """True if a successful kill-switch drill was recorded within max_age_sec."""
    try:
        d = json.loads(DRILL_MARKER.read_text())
        if not d.get("passed"):
            return False
        ts = datetime.fromisoformat(d["ts"]).timestamp()
        return (time.time() - ts) < max_age_sec
    except Exception:
        return False


# ── Arming gate (refused by default) ────────────────────────────────────────────
def can_arm(*, backtest_tier: str, forward_verdict: str, aum: float,
            confirmed: bool = False) -> dict:
    """Evaluate whether micro-live arming is permitted. Refuses unless ALL gates pass."""
    blockers = []
    if backtest_tier not in ("SCREEN", "PROMOTE"):
        blockers.append(f"backtest tier '{backtest_tier}' is not SCREEN/PROMOTE")
    if forward_verdict != "PASS":
        blockers.append(f"forward-evidence verdict '{forward_verdict}' != PASS")
    if not drill_recent():
        blockers.append("kill-switch drill not passed recently (run run_drill())")
    if not confirmed:
        blockers.append("explicit human confirmation required (confirmed=False)")
    return {"armed": not blockers, "blockers": blockers,
            "size_usd": microlive_cap(aum), "aum": aum}


def arm_microlive(strategy: str, *, backtest_tier: str, forward_verdict: str,
                  aum: float, confirmed: bool = False) -> dict:
    """Arm a strategy for micro-live. REFUSES unless every gate passes AND confirmed=True.

    This function does NOT itself flip the live config; it returns the approved size +
    instructions. Actual live execution still goes through the normal config-gated path
    with the kill-switch attached. Intentionally conservative.
    """
    gate = can_arm(backtest_tier=backtest_tier, forward_verdict=forward_verdict,
                   aum=aum, confirmed=confirmed)
    if not gate["armed"]:
        return {"status": "REFUSED", "strategy": strategy, **gate}
    return {"status": "ARMED", "strategy": strategy, "size_usd": gate["size_usd"],
            "limits": asdict(LIMITS),
            "note": "Attach KillSwitch(size_usd) to the live tranche; auto-revert on breach."}


__all__ = [
    "MicroLiveLimits", "LIMITS", "microlive_cap", "KillSwitch", "revert_to_paper",
    "run_drill", "drill_recent", "can_arm", "arm_microlive", "DRILL_MARKER",
]
