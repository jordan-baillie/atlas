"""Rapid validate->live pipeline orchestrator (board memo 2026-06-03, #420).

Ties the three gate modules into one staged flow and tracks each candidate's stage in a
JSON registry. It does NOT run backtests or trade; it records state and computes the next
action from the existing gates:

    queued -> battery -> screen -> paper -> microlive_gate -> microlive -> scale
                                                  (any stage -> failed)

Stage gates:
  battery  : research.cross_oos battery -> tier SCREEN or PROMOTE (else failed).
  paper    : research.forward_evidence.evaluate_forward(daily net-of-cost returns).
             PASS -> microlive_gate ; FAIL -> failed ; INSUFFICIENT -> keep running.
  microlive: research.microlive_gate.arm_microlive (refused unless drilled + confirmed).

Throughput over latency: many candidates sit in `paper` concurrently; the FIRST to clear
the forward gate goes to the micro-live gate. Slow strategies simply stay in paper longer.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
REGISTRY = PROJECT / "data" / "pipeline_candidates.json"

STAGES = ["queued", "battery", "screen", "paper", "microlive_gate", "microlive", "scale", "failed"]


@dataclass
class Candidate:
    name: str                          # strategy name (sandbox or registry)
    params: dict = field(default_factory=dict)   # config preset (e.g. fast variant)
    label: str = ""                    # unique cohort label
    stage: str = "queued"
    battery_tier: str | None = None    # SCREEN | PROMOTE | FAIL
    battery_artifact: str | None = None
    forward_verdict: str | None = None # PASS | INSUFFICIENT | FAIL
    forward_start: str = ""            # ISO date when forward (paper) accrual began
    forward_days: int = 0              # forward trading days accrued so far
    notes: str = ""
    updated_at: str = ""

    def touch(self):
        self.updated_at = datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    try:
        return json.loads(REGISTRY.read_text())
    except Exception:
        return {"candidates": {}}


def _save(state: dict) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(REGISTRY)


def register(name: str, *, label: str | None = None, params: dict | None = None,
             stage: str = "queued", **extra) -> Candidate:
    state = _load()
    label = label or (name if not params else f"{name}@{'_'.join(f'{k}{v}' for k,v in sorted(params.items()))}")
    c = Candidate(name=name, params=params or {}, label=label, stage=stage, **extra)
    c.touch()
    state["candidates"][label] = asdict(c)
    _save(state)
    return c


def set_stage(label: str, stage: str, **fields) -> dict:
    assert stage in STAGES, f"unknown stage {stage}"
    state = _load()
    c = state["candidates"].get(label)
    if not c:
        raise KeyError(label)
    c["stage"] = stage
    c.update({k: v for k, v in fields.items() if k in Candidate.__dataclass_fields__})
    c["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(state)
    return c


def next_action(label: str, *, forward_returns=None, clv: float | None = None,
                aum: float = 0.0, confirmed: bool = False) -> dict:
    """Compute the next pipeline action for a candidate from the live gate modules."""
    state = _load()
    c = state["candidates"].get(label)
    if not c:
        raise KeyError(label)
    tier = c.get("battery_tier")
    if c["stage"] in ("queued", "battery"):
        return {"action": "run_battery", "label": label,
                "note": "run scripts/run_strategy_battery.py for this candidate"}
    if tier not in ("SCREEN", "PROMOTE"):
        return {"action": "fail", "label": label, "reason": f"battery tier {tier}"}
    # paper -> evaluate forward evidence
    from research.forward_evidence import evaluate_forward
    if forward_returns is None:
        return {"action": "accumulate_paper", "label": label,
                "note": "no forward returns yet; keep running in paper"}
    fe = evaluate_forward(forward_returns, clv=clv)
    if fe["verdict"] == "FAIL":
        return {"action": "fail", "label": label, "reason": "forward gate FAIL", "forward": fe}
    if fe["verdict"] == "INSUFFICIENT":
        return {"action": "accumulate_paper", "label": label, "forward": fe}
    # forward PASS -> micro-live gate
    from research.microlive_gate import arm_microlive
    arm = arm_microlive(c["name"], backtest_tier=tier, forward_verdict="PASS",
                        aum=aum, confirmed=confirmed)
    return {"action": "microlive_gate", "label": label, "forward": fe, "arm": arm}


def status() -> list[dict]:
    return list(_load().get("candidates", {}).values())


def format_status() -> str:
    rows = status()
    if not rows:
        return "pipeline: no candidates registered."
    lines = ["Rapid pipeline candidates:",
             f"{'label':40s} {'stage':14s} {'battery':8s} {'forward':12s}"]
    for c in sorted(rows, key=lambda x: x.get("stage", "")):
        lines.append(f"{c['label'][:40]:40s} {c['stage']:14s} "
                     f"{str(c.get('battery_tier')):8s} {str(c.get('forward_verdict')):12s}")
    return "\n".join(lines)


__all__ = ["Candidate", "STAGES", "register", "set_stage", "next_action",
           "status", "format_status", "REGISTRY"]
