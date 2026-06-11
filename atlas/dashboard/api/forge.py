"""Forge monitor API — aggregates live Hephaestus autonomous-research-loop state.

Single read endpoint GET /api/forge/state. Reads (best-effort, never 500s) from:
  - /root/crucible/agent/run_log.jsonl       (per-cycle outcomes — full proposal + verdict)
  - /root/research-wiki/.registry/*.jsonl       (FDR family registry + rising bar)
  - /root/research-wiki/candidates.md           (scout candidate queue)
  - /root/research-wiki/experiments|sources/    (counts)
  - /root/crucible/LOOP_DISABLED              (killswitch state)
  - systemctl crucible-forge.timer            (next-run / last-trigger — the LIVE 3-smith pipeline)
"""
from __future__ import annotations

import glob
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.security import HTTPBasicCredentials

from atlas.dashboard.auth import check_auth  # hard import: if auth is broken, fail loudly, never serve open
from atlas.kernel.paths import CONFIG_DIR

router = APIRouter(prefix="/api/forge", tags=["forge"])

HEPH = Path("/root/crucible")
WIKI = Path("/root/research-wiki")
RUN_LOG = HEPH / "agent" / "run_log.jsonl"
REGISTRY_GLOB = str(WIKI / ".registry" / "*.jsonl")
CANDIDATES = WIKI / "candidates.md"
LOOP_DISABLED = HEPH / "LOOP_DISABLED"
# /tmp/crucible_forge.log is a symlink to the current dated log under hephaestus/logs/
CYCLE_LOG = Path("/tmp/crucible_forge.log")


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return out


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _clip(v, n=400):
    return (str(v)[:n] if v is not None else None)


def _systemctl_status() -> dict:
    info = {"enabled": False, "next_run_str": None, "last_trigger_str": None}
    try:
        en = subprocess.run(["systemctl", "is-enabled", "crucible-forge.timer"],
                            capture_output=True, text=True, timeout=4)
        info["enabled"] = en.stdout.strip() == "enabled"
        show = subprocess.run(
            ["systemctl", "show", "crucible-forge.timer",
             "-p", "NextElapseUSecRealtime", "-p", "LastTriggerUSec"],
            capture_output=True, text=True, timeout=4).stdout
        for ln in show.splitlines():
            if ln.startswith("NextElapseUSecRealtime="):
                info["next_run_str"] = ln.split("=", 1)[1].strip() or None
            elif ln.startswith("LastTriggerUSec="):
                info["last_trigger_str"] = ln.split("=", 1)[1].strip() or None
    except Exception:
        pass
    return info


def _parse_cycles(rows: list[dict]) -> list[dict]:
    cycles = []
    for o in rows:
        v = o.get("verdict") or {}
        p = o.get("proposal") or {}
        ran = bool(o.get("ran"))
        passed = bool(o.get("passed_all") or v.get("PASSED_ALL_GATES"))
        tier = v.get("tier") or ("PASS" if passed else None)
        # near-miss = cleared the FDR bar (tier PROMOTE) but failed a later gate
        # (e.g. generalization breadth) — distinct from a plain FAIL.
        if passed:
            status = "pass"
        elif ran and tier == "PROMOTE":
            status = "near_miss"
        elif ran:
            status = "fail"
        else:
            status = "error"
        ss, hs = _num(v.get("search_sharpe")), _num(v.get("holdout_sharpe"))
        degradation = None
        if ss not in (None, 0) and hs is not None:
            degradation = round((hs / ss - 1) * 100, 1)
        cycles.append({
            "ts": o.get("ts"),
            "id": o.get("id"),
            "title": o.get("title") or p.get("title") or "(untitled)",
            "status": status,
            "ran": ran,
            "tier": tier,
            "passed_all": passed,
            "family": v.get("family"),
            "premium": _clip(p.get("premium"), 240),
            "market": _clip(p.get("market"), 200),
            "hypothesis": {
                "signal_approach": _clip(p.get("signal_approach"), 600),
                "why_not_duplicate": _clip(p.get("why_not_duplicate"), 500),
                "pairs_with": _clip(p.get("pairs_with"), 300),
                "prior": _clip(p.get("prior"), 60),
            },
            "data": {
                "free_or_owned": _clip(p.get("free_or_owned"), 200),
                "data_source": _clip(p.get("data_source"), 300),
                "gate0_data_check": _clip(p.get("gate0_data_check"), 500),
            },
            "metrics": {
                "search_sharpe": ss,
                "holdout_sharpe": hs,
                "degradation_pct": degradation,
                "holdout_pass": v.get("holdout_pass"),
                "holdout_reasons": v.get("holdout_reasons") or [],
                "full_sharpe": _num(v.get("full_sharpe")),
                "full_maxdd": _num(v.get("full_maxdd")),
                "n_trades": _num(v.get("n_trades")),
                "dsr": _num(v.get("dsr")),
                "median_cpcv": _num(v.get("median_cpcv")),
                "pbo": _num(v.get("pbo")),
                "deployment_passed": v.get("deployment_passed"),
                "promote_bar": _num(v.get("promote_bar")),
                "n_families": _num(v.get("n_families")),
            },
        })
    cycles.reverse()
    return cycles


def _parse_registry(rows: list[dict]) -> dict:
    families, history, seen = [], [], set()
    bar = 0.90
    for r in rows:
        pd = _num(r.get("promote_dsr"))
        if pd is not None:
            history.append(round(pd, 4))
            bar = max(bar, pd)
        fam = r.get("family")
        if fam and fam not in seen:
            seen.add(fam)
            families.append({"family": fam, "tier": r.get("tier"),
                             "dsr": _num(r.get("dsr")), "passed_all": bool(r.get("passed_all"))})
    return {"bar": round(bar, 4), "n_families": len(seen), "families": families, "history": history}


_CAND_RE = re.compile(r"^- \*\*(.+?)\*\*\s*\((.*?)\)\s*[—-]\s*(.*)$")


def _work_queue() -> dict:
    """The ACTUAL Phase-3 multi-agent claim queue (research-wiki/.queue/queue.jsonl) — distinct from
    the scout candidate list. status: queued -> claimed -> done."""
    p = WIKI / ".queue" / "queue.jsonl"
    out = {"queued": 0, "claimed": 0, "done": 0}
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if line:
                st = json.loads(line).get("status")
                if st in out:
                    out[st] += 1
    except FileNotFoundError:
        pass
    return out


def _parse_candidates(path: Path) -> list[dict]:
    out, seen = [], set()
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return out
    for ln in reversed(lines):
        m = _CAND_RE.match(ln.strip())
        if not m:
            continue
        title, tags, rest = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        key = re.sub(r"[^a-z]", "", title.lower())[:28]
        if key in seen:
            continue
        seen.add(key)
        dm = re.search(r"\[data:\s*(.*?)\]", rest)
        data_note = (dm.group(1) if dm else "").strip()
        free = bool(re.search(r"\b(free|owned)\b", data_note, re.I))
        out.append({"title": title, "tags": tags,
                    "summary": rest.split("[data:")[0].split(" src:")[0].strip()[:320],
                    "data_note": data_note[:140], "free": free})
    return out


def _count(pattern: str) -> int:
    try:
        return len(glob.glob(pattern))
    except Exception:
        return 0


def _pct(a: int, b: int) -> str:
    return f"{round(100 * a / b)}%" if b else "—"


@router.get("/state")
def forge_state(_auth: HTTPBasicCredentials = Depends(check_auth)) -> dict:
    cycles = _parse_cycles(_read_jsonl(RUN_LOG))
    reg_rows: list[dict] = []
    for p in sorted(glob.glob(REGISTRY_GLOB)):
        reg_rows.extend(_read_jsonl(Path(p)))
    fdr = _parse_registry(reg_rows)
    candidates = _parse_candidates(CANDIDATES)
    work_q = _work_queue()  # the ACTUAL Phase-3 claim queue (DISTINCT from scout candidates)

    status = _systemctl_status()
    status["running"] = not LOOP_DISABLED.exists()
    status["last_cycle_ts"] = cycles[0]["ts"] if cycles else None

    n_exp = _count(str(WIKI / "experiments" / "*.md"))
    n_src = _count(str(WIKI / "sources" / "*.md"))
    n_premia = _count(str(WIKI / "premia" / "*.md"))
    n_pat = _count(str(WIKI / "patterns" / "*.md"))
    wiki_pages = n_exp + n_src + n_premia + n_pat

    n_cycles = len(cycles)
    n_ran = sum(1 for c in cycles if c["ran"])
    n_pass = sum(1 for c in cycles if c["passed_all"])
    n_near = sum(1 for c in cycles if c["status"] == "near_miss")
    n_err = sum(1 for c in cycles if c["status"] == "error")
    n_fail = n_cycles - n_pass - n_near - n_err

    # strategies deployed to paper (live registry) — the evidence-gate route for
    # clean near-misses; renders the "deployed on probation" middle tier visible.
    n_deployed = 0
    deployed_names: list[str] = []
    try:
        _reg = json.loads((CONFIG_DIR / "live_strategies.json").read_text())
        deployed_names = [s.get("name", "?") for s in _reg]
        n_deployed = len(_reg)
    except Exception:
        pass
    free_cand = sum(1 for c in candidates if c["free"])
    # nearest miss = best holdout sharpe across runs (how close anything got)
    best_h = max((c["metrics"]["holdout_sharpe"] for c in cycles
                  if c["metrics"]["holdout_sharpe"] is not None), default=None)

    pipeline = [
        {"key": "scout", "label": "Scout", "icon": "🔭", "count": n_src, "accent": False,
         "stats": [{"label": "research runs", "value": n_src},
                   {"label": "candidates found", "value": len(candidates)},
                   {"label": "free-data", "value": _pct(free_cand, len(candidates))}]},
        {"key": "propose", "label": "Propose", "icon": "💡", "count": len(candidates), "accent": False,
         "stats": [{"label": "scout ideas", "value": len(candidates)},
                   {"label": "free", "value": free_cand},
                   {"label": "data-gated", "value": len(candidates) - free_cand}]},
        {"key": "codegen", "label": "Codegen", "icon": "⚙️", "count": n_ran, "accent": False,
         "stats": [{"label": "work queue", "value": work_q["queued"] + work_q["claimed"]},
                   {"label": "coded", "value": n_ran},
                   {"label": "self-repair", "value": "on"}]},
        {"key": "run", "label": "Rails", "icon": "🛡️", "count": n_cycles, "accent": False,
         "stats": [{"label": "tested", "value": n_cycles},
                   {"label": "failed", "value": n_fail + n_err},
                   {"label": "near-miss", "value": n_near},
                   {"label": "passed", "value": n_pass}]},
        {"key": "record", "label": "Record", "icon": "📖", "count": n_exp, "accent": False,
         "stats": [{"label": "experiments", "value": n_exp},
                   {"label": "FDR families", "value": fdr["n_families"]},
                   {"label": "wiki pages", "value": wiki_pages}]},
        {"key": "alert", "label": "Alert", "icon": "🔔", "count": n_pass + n_deployed,
         "accent": n_pass > 0 or n_deployed > 0,
         "stats": [{"label": "passes", "value": n_pass},
                   {"label": "deployed to paper", "value": n_deployed},
                   {"label": "best holdout Sh", "value": (f"{best_h:.2f}" if best_h is not None else "—")}]},
    ]

    summary = {
        "cycles": n_cycles, "ran": n_ran, "passes": n_pass, "near_misses": n_near,
        "fails": n_fail, "errors": n_err, "deployed": n_deployed, "deployed_names": deployed_names,
        "pass_rate": _pct(n_pass, max(n_cycles, 1)),
        "experiments": n_exp, "sources": n_src, "candidates": len(candidates),
        "families": fdr["n_families"], "wiki_pages": wiki_pages,
        "fdr_bar": fdr["bar"], "best_holdout_sharpe": best_h,
        "work_queue": work_q["queued"] + work_q["claimed"], "scout_ideas": len(candidates),
    }

    log_tail: list[str] = []
    try:
        if CYCLE_LOG.exists():
            log_tail = CYCLE_LOG.read_text(errors="replace").splitlines()[-30:]
    except Exception:
        pass

    return {
        "generated_at": datetime.now().isoformat(),
        "status": status,
        "summary": summary,
        "fdr": fdr,
        "pipeline": pipeline,
        "cycles": cycles[:50],
        "candidates": candidates[:14],
        "log_tail": log_tail,
    }
