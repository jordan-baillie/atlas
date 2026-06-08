"""Forge monitor API — aggregates live Hephaestus autonomous-research-loop state.

Single read endpoint GET /api/forge/state. Reads (best-effort, never 500s) from:
  - /root/hephaestus/agent/run_log.jsonl       (per-cycle outcomes)
  - /root/research-wiki/.registry/*.jsonl       (FDR family registry + rising bar)
  - /root/research-wiki/candidates.md           (scout candidate queue)
  - /root/research-wiki/experiments|sources/    (counts)
  - /root/hephaestus/LOOP_DISABLED              (killswitch state)
  - systemctl hephaestus-cycle.timer            (next-run / last-trigger)
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.security import HTTPBasicCredentials

try:
    from services.auth import check_auth
except Exception:  # pragma: no cover - auth optional in some test harnesses
    def check_auth():  # type: ignore
        return None

router = APIRouter(prefix="/api/forge", tags=["forge"])

HEPH = Path("/root/hephaestus")
WIKI = Path("/root/research-wiki")
RUN_LOG = HEPH / "agent" / "run_log.jsonl"
REGISTRY_GLOB = str(WIKI / ".registry" / "*.jsonl")
CANDIDATES = WIKI / "candidates.md"
LOOP_DISABLED = HEPH / "LOOP_DISABLED"
CYCLE_LOG = Path("/tmp/heph_cycle.log")


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return out


def _systemctl_status() -> dict:
    info = {"enabled": False, "next_run_ms": None, "next_run_str": None, "last_trigger_str": None}
    try:
        en = subprocess.run(
            ["systemctl", "is-enabled", "hephaestus-cycle.timer"],
            capture_output=True, text=True, timeout=4,
        )
        info["enabled"] = en.stdout.strip() == "enabled"
        show = subprocess.run(
            ["systemctl", "show", "hephaestus-cycle.timer",
             "-p", "NextElapseUSecRealtime", "-p", "LastTriggerUSec"],
            capture_output=True, text=True, timeout=4,
        ).stdout
        for ln in show.splitlines():
            if ln.startswith("NextElapseUSecRealtime="):
                info["next_run_str"] = ln.split("=", 1)[1].strip() or None
            elif ln.startswith("LastTriggerUSec="):
                info["last_trigger_str"] = ln.split("=", 1)[1].strip() or None
        # parse "Tue 2026-06-09 03:30:00 AEST" -> local-naive epoch ms (server runs AEST)
        if info["next_run_str"]:
            m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", info["next_run_str"])
            if m:
                dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                info["next_run_ms"] = int(dt.timestamp() * 1000)
    except Exception:
        pass
    return info


def _parse_cycles(rows: list[dict]) -> list[dict]:
    cycles = []
    for o in rows:
        v = o.get("verdict") or {}
        prop = o.get("proposal") or {}
        ran = bool(o.get("ran"))
        passed = bool(o.get("passed_all"))
        tier = v.get("tier") or ("PASS" if passed else None)
        status = "pass" if passed else ("fail" if ran else "error")
        cycles.append({
            "ts": o.get("ts"),
            "id": o.get("id"),
            "title": o.get("title") or prop.get("title") or "(untitled)",
            "premium": (prop.get("premium") or "")[:160],
            "market": (prop.get("market") or "")[:120],
            "ran": ran,
            "tier": tier,
            "passed_all": passed,
            "holdout_pass": v.get("holdout_pass"),
            "dsr": v.get("dsr"),
            "status": status,
        })
    cycles.reverse()  # newest first
    return cycles


def _parse_registry(rows: list[dict]) -> dict:
    families = []
    history = []
    seen = set()
    bar = 0.90
    for r in rows:
        pd = r.get("promote_dsr")
        if isinstance(pd, (int, float)):
            history.append(round(float(pd), 4))
            bar = max(bar, float(pd))
        fam = r.get("family")
        if fam and fam not in seen:
            seen.add(fam)
            families.append({
                "family": fam,
                "tier": r.get("tier"),
                "dsr": r.get("dsr"),
                "promote_dsr": r.get("promote_dsr"),
                "n_families": r.get("n_families"),
                "passed_all": bool(r.get("passed_all")),
            })
    return {
        "bar": round(bar, 4),
        "n_families": len(seen),
        "families": families,
        "history": history,
    }


_CAND_RE = re.compile(r"^- \*\*(.+?)\*\*\s*\((.*?)\)\s*[—-]\s*(.*)$")


def _parse_candidates(path: Path) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return out
    # newest blocks are appended at the bottom -> walk in reverse so newest wins dedup
    for ln in reversed(lines):
        m = _CAND_RE.match(ln.strip())
        if not m:
            continue
        title, tags, rest = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        key = re.sub(r"[^a-z]", "", title.lower())[:28]
        if key in seen:
            continue
        seen.add(key)
        data_m = re.search(r"\[data:\s*(.*?)\]", rest)
        data_note = (data_m.group(1) if data_m else "").strip()
        free = bool(re.search(r"\b(free|owned)\b", data_note, re.I))
        summary = rest.split("[data:")[0].split(" src:")[0].strip()
        out.append({
            "title": title,
            "tags": tags,
            "summary": summary[:320],
            "data_note": data_note[:140],
            "free": free,
        })
    return out


def _count_glob(pattern: str) -> int:
    try:
        return len(glob.glob(pattern))
    except Exception:
        return 0


@router.get("/state")
def forge_state(_auth: HTTPBasicCredentials = Depends(check_auth)) -> dict:
    rows = _read_jsonl(RUN_LOG)
    cycles = _parse_cycles(rows)
    reg_rows: list[dict] = []
    for p in sorted(glob.glob(REGISTRY_GLOB)):
        reg_rows.extend(_read_jsonl(Path(p)))
    fdr = _parse_registry(reg_rows)
    candidates = _parse_candidates(CANDIDATES)

    status = _systemctl_status()
    status["running"] = not LOOP_DISABLED.exists()
    last_cycle_ts = cycles[0]["ts"] if cycles else None
    status["last_cycle_ts"] = last_cycle_ts

    n_experiments = _count_glob(str(WIKI / "experiments" / "*.md"))
    n_sources = _count_glob(str(WIKI / "sources" / "*.md"))
    n_premia = _count_glob(str(WIKI / "premia" / "*.md"))
    n_patterns = _count_glob(str(WIKI / "patterns" / "*.md"))
    n_passes = sum(1 for c in cycles if c["passed_all"])
    n_ran = sum(1 for c in cycles if c["ran"])

    counts = {
        "cycles": len(cycles),
        "ran": n_ran,
        "passes": n_passes,
        "experiments": n_experiments,
        "sources": n_sources,
        "candidates": len(candidates),
        "families": fdr["n_families"],
        "wiki_pages": n_experiments + n_sources + n_premia + n_patterns,
    }

    # pipeline stages (Variant A) — counts that flow through the loop
    pipeline = [
        {"key": "scout", "label": "Scout", "icon": "telescope", "count": n_sources,
         "sub": "web research runs"},
        {"key": "propose", "label": "Propose", "icon": "lightbulb", "count": len(candidates),
         "sub": "candidates queued"},
        {"key": "codegen", "label": "Codegen", "icon": "code", "count": n_ran,
         "sub": "strategies written"},
        {"key": "run", "label": "Rails", "icon": "shield", "count": len(cycles),
         "sub": "cycles tested"},
        {"key": "record", "label": "Record", "icon": "book", "count": n_experiments,
         "sub": "experiments logged"},
        {"key": "alert", "label": "Alert", "icon": "bell", "count": n_passes,
         "sub": "full-gate passes"},
    ]

    # log tail
    log_tail: list[str] = []
    try:
        if CYCLE_LOG.exists():
            log_tail = CYCLE_LOG.read_text(errors="replace").splitlines()[-40:]
    except Exception:
        pass

    return {
        "generated_at": datetime.now().isoformat(),
        "status": status,
        "counts": counts,
        "fdr": fdr,
        "pipeline": pipeline,
        "cycles": cycles[:40],
        "candidates": candidates[:14],
        "log_tail": log_tail,
    }
