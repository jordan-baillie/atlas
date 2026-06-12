"""Research Map API — the wiki rendered as a lineage graph for the dashboard.

Single read endpoint GET /api/forge/map. Best-effort (never 500s), 60s cache —
the underlying data changes once nightly. Read-only over:
  - /root/research-wiki/experiments/*.md        (frontmatter + verdict metrics)
  - /root/research-wiki/.registry/hypothesis_registry.jsonl  (tier/DSR/rising FDR bar)
  - /root/research-wiki/.queue/queue.jsonl      (queued ghosts + pairs_with + parent_ids)
  - /root/research-wiki/.elite/pool.jsonl       (MAP-Elites grid occupants)
  - /root/crucible/agent/run_log.jsonl          (arm/agent/parent_ids -> strategy-id join)
  - /root/research-wiki/premia/*.md             (premium concept lane status)

Graph contract returned to the UI:
  nodes[]  one per experiment page + one ghost per queued item
  edges[]  lineage (refine/orthogonal/crossover; explicit or inferred) + pairs_with relations
  lanes[]  normalized premium-family swimlanes, ordered by activity
  elite_grid[]  current MAP-Elites occupants (cell -> champion)
  stats{}  headline counts for the strip
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.security import HTTPBasicCredentials

from atlas.dashboard.auth import check_auth

router = APIRouter(prefix="/api/forge", tags=["forge"])

WIKI = Path("/root/research-wiki")
RUN_LOG = Path("/root/crucible/agent/run_log.jsonl")
EXPERIMENTS = WIKI / "experiments"
REGISTRY = WIKI / ".registry" / "hypothesis_registry.jsonl"
QUEUE = WIKI / ".queue" / "queue.jsonl"
ELITE = WIKI / ".elite" / "pool.jsonl"
PREMIA = WIKI / "premia"

_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 60.0


# ---------------------------------------------------------------- family lanes
# Port of crucible agent/families.py bucketing (kept in sync by tests there) —
# collapses the ~50 raw family strings into ~15 clean swimlanes.
_LOW_RISK = ("betting-against", "bab", "low-beta", "low beta", "low-vol", "low vol",
             "low volatility", "leverage-aversion", "defensive")
_LANE_PAIRS = [
    ("illiquidity", ("amihud", "illiquidity", "illiquid", "liquidity premium",
                     "liquidity risk", "liquidity_premium", "liquidity_provision", "liquidity")),
    ("carry", ("carry", "funding rate", "funding-carry", "roll yield")),
    ("event", ("pead", "post-earnings", "post_earnings", "earnings-surprise", "earnings surprise",
               "sue", "drift", "event", "announcement", "fomc", "auction", "dividend",
               "index_flow", "recon", "deletion")),
    ("skew", ("skew", "lottery", "idiosyncratic vol", "max return", "higher_moment")),
    ("quality", ("quality", "profitability", "gross profit", "accrual")),
    ("reversal", ("reversal", "mean-reversion", "mean reversion", "short-term reversal")),
    ("vrp", ("vrp", "volatility_risk", "volatility risk", "short-vol", "variance")),
    ("trend", ("trend", "tsmom", "time-series momentum", "time series momentum", "managed futures")),
    ("value_momentum", ("value_momentum", "value+momentum", "valmom", "val_mom", "value mom")),
    ("momentum", ("momentum", "12-1")),
    ("value", ("value", "book-to", "earnings yield", "b/m")),
    ("size", ("size premium", "small-cap premium", "smb")),
    ("seasonal", ("seasonal", "calendar", "turn-of-month", "month-of-year")),
    ("credit", ("credit", "default spread", "duration")),
    ("share_issuance", ("issuance", "repurchas", "buyback")),
    ("sports", ("pitcher", "sports", "mlb", "nrl")),
]

LANE_LABELS = {
    "illiquidity": "Illiquidity", "carry": "Carry", "event": "Event / Announcement",
    "skew": "Skew / Lottery", "quality": "Quality / Accruals", "reversal": "Reversal",
    "vrp": "Volatility Risk Premium", "trend": "Trend / TSMOM",
    "value_momentum": "Value × Momentum", "momentum": "Momentum", "value": "Value",
    "size": "Size", "seasonal": "Seasonality", "credit": "Credit",
    "share_issuance": "Share Issuance", "sports": "Sports", "low_risk": "Low-Risk / BAB",
    "multi": "Multi-Premium Books", "other": "Other",
}


def lane_of(family: str, title: str = "") -> str:
    t = f"{family} {title}".lower()
    if any(k in t for k in _LOW_RISK):
        return "low_risk"
    has_val = any(k in t for k in ("value", "book-to", "book to", "earnings yield", "b/m", "btm"))
    has_mom = "momentum" in t or "12-1" in t or "12_1" in t
    if has_val and has_mom:
        return "value_momentum"
    if "combo" in t or "two-premium" in t or "two premium" in t or "book" in t and "trend" in t:
        # multi-premium blended books (carry×trend etc.) get their own lane —
        # they relate to several premia and would otherwise pollute single lanes
        if sum(1 for _, kws in _LANE_PAIRS if any(k in t for k in kws)) >= 2:
            return "multi"
    for name, kws in _LANE_PAIRS:
        if any(k in t for k in kws):
            return name
    return "other"


# ---------------------------------------------------------------- file readers
def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        for line in path.read_text().splitlines():
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


_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_KV_RE = re.compile(r"^(\w+):\s*(.*)$")
_METRIC_RES = {
    "dsr": re.compile(r"DSR\s+([\d.]+)"),
    "search_sharpe": re.compile(r"search Sharpe\s+(-?[\d.]+)"),
    "holdout_sharpe": re.compile(r"holdout Sharpe\s+(-?[\d.]+)"),
    "full_sharpe": re.compile(r"full Sharpe\s+(-?[\d.]+)"),
    "maxdd": re.compile(r"maxDD\s+(-?[\d.]+)"),
    "cpcv": re.compile(r"CPCV\s+(-?[\d.]+)"),
    "pbo": re.compile(r"PBO\s+([\d.]+)"),
}


def _norm_status(raw: str) -> str:
    """Collapse free-text status lines into the UI's closed enum."""
    s = (raw or "").upper()
    # terminal/withdrawn states FIRST — their prose often mentions 'validated' or
    # 'forward gate' (e.g. 'ORPHANED … carry leg lost its forward gate') and must
    # not be misread as passes.
    if "ORPHAN" in s or "TERMINATED" in s or "ARCHIVED" in s:
        return "closed"
    if "PASSED ALL GATES" in s or s.startswith("VALIDATED"):
        return "pass"
    if "NEAR-MISS" in s or "NEAR MISS" in s:
        return "near_miss"
    if "REJECTED" in s or "FAIL" in s:
        return "fail"
    if "FORWARD" in s:
        return "pass"
    return "other"


def _parse_experiment(path: Path) -> dict | None:
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return None
    fm: dict = {}
    m = _FM_RE.match(text)
    if m:
        for ln in m.group(1).splitlines():
            kv = _KV_RE.match(ln.strip())
            if kv:
                fm[kv.group(1)] = kv.group(2).strip()
    body = text[m.end():] if m else text
    title_m = re.search(r"^#\s+(.+)$", body, re.M)
    metrics = {}
    for key, rx in _METRIC_RES.items():
        mm = rx.search(body)
        if mm:
            try:
                metrics[key] = float(mm.group(1))
            except ValueError:
                pass
    raw_status = fm.get("status", "")
    family = fm.get("family", "")
    title = title_m.group(1).strip() if title_m else path.stem
    # one-paragraph pre-registration summary for the drawer (first prose block after "Pre-registration")
    prereg = None
    pm = re.search(r"## Pre-registration.*?\n(.+?)(?:\n\n|\n##)", body, re.S)
    if pm:
        prereg = pm.group(1).strip()[:700]
    markets = re.findall(r"'([^']+)'", fm.get("markets", "")) or \
        [w.strip(" []") for w in fm.get("markets", "").strip("[]").split(",") if w.strip(" []")]
    return {
        # node id = page stem (UNIQUE across the wiki — frontmatter ids collide:
        # defensive-bab.md and defensive_bab.md both declare id 'defensive_bab');
        # 'rid' keeps the declared id for registry/run_log joins.
        "id": path.stem,
        "rid": fm.get("id", path.stem),
        "page": path.stem,
        "title": title[:200],
        "status": _norm_status(raw_status),
        "status_raw": raw_status[:120],
        "family": family,
        "lane": lane_of(family, title),
        "markets": markets[:4],
        "date": fm.get("date"),
        "project": fm.get("project"),
        "metrics": metrics,
        "prereg": prereg,
    }


# ---------------------------------------------------------------- lineage edges
_VER_RE = re.compile(r"^(.*?)[_-]v(\d+)$")


def _version_chains(nodes: list[dict]) -> list[dict]:
    """Inferred refine edges from version suffixes. Mutation runs RENAME stems
    (amihud_illiq_etf_hedged_v2 -> amihud_illiq_tranched_v3), so we bucket on the
    shared 2-token prefix + lane instead of the exact stem: within a bucket each
    vN node links back to every v(N-1) node (the version cohort it iterated on).
    Exact-stem chains (foo_v1 -> foo_v2) fall out of this as a special case."""
    buckets: dict[tuple, list[tuple[int, str]]] = {}
    for n in nodes:
        m = _VER_RE.match(n["id"])
        if not m:
            continue
        prefix = "_".join(re.split(r"[_-]", m.group(1))[:2])
        buckets.setdefault((n["lane"], prefix), []).append((int(m.group(2)), n["id"]))
    edges = []
    for _, vers in buckets.items():
        byv: dict[int, list[str]] = {}
        for v, nid in vers:
            byv.setdefault(v, []).append(nid)
        for v in sorted(byv):
            if v - 1 in byv:
                for child in byv[v]:
                    for parent in byv[v - 1]:
                        edges.append({"source": parent, "target": child,
                                      "kind": "refine", "inferred": True})
    return edges


def _explicit_edges(run_rows: list[dict], elite_to_strategy: dict[str, str],
                    known: set[str], rid_map: dict[str, str]) -> list[dict]:
    """Explicit lineage from run_log parent_ids (elite-pool ids -> strategy ids)."""
    edges = []
    for r in run_rows:
        parents = r.get("parent_ids") or []
        child = (r.get("verdict") or {}).get("id")
        child = rid_map.get(child, child)
        if not child or child not in known or not parents:
            continue
        kind = {"refine": "refine", "orthogonal": "orthogonal",
                "crossover": "crossover"}.get(r.get("arm") or "", "refine")
        for p in parents:
            src = elite_to_strategy.get(str(p))
            if src and src in known and src != child:
                edges.append({"source": src, "target": child, "kind": kind, "inferred": False})
    return edges


def _pairs_with_edges(queue_rows: list[dict], run_rows: list[dict], nodes: list[dict]) -> list[dict]:
    """Soft 'pairs_with' relation edges: proposals that lean on the validated trend hedge etc.
    Heuristic keyword match of pairs_with text against node titles/lanes — rendered dotted."""
    # map queue_id -> node id via run_log (declared id remapped to unique page stem)
    rid_map = {n["rid"]: n["id"] for n in nodes
               if n["rid"] not in {m["id"] for m in nodes} or n["rid"] == n["id"]}
    qid_to_sid = {}
    for r in run_rows:
        sid = (r.get("verdict") or {}).get("id")
        if sid:
            qid_to_sid[r.get("queue_id")] = rid_map.get(sid, sid)
    trend_anchor = next((n["id"] for n in nodes if n["lane"] == "trend" and n["status"] != "fail"),
                        next((n["id"] for n in nodes if n["lane"] == "trend"), None))
    edges, seen = [], set()
    for q in queue_rows:
        sid = qid_to_sid.get(q.get("id"))
        if not sid:
            continue
        pw = str((q.get("proposal") or {}).get("pairs_with", "")).lower()
        if trend_anchor and sid != trend_anchor and ("trend" in pw or "crisis-alpha" in pw):
            key = (trend_anchor, sid)
            if key not in seen:
                seen.add(key)
                edges.append({"source": trend_anchor, "target": sid,
                              "kind": "pairs_with", "inferred": True})
    return edges


# ---------------------------------------------------------------- assembly
def _build() -> dict:
    nodes: list[dict] = []
    for p in sorted(EXPERIMENTS.glob("*.md")):
        n = _parse_experiment(p)
        if n:
            nodes.append(n)

    run_rows = _read_jsonl(RUN_LOG)
    reg_rows = _read_jsonl(REGISTRY)
    queue_rows = _read_jsonl(QUEUE)
    elite_rows = _read_jsonl(ELITE)

    # registry join: tier / dsr / bar-at-test per strategy id
    reg_by_id = {r.get("strategy"): r for r in reg_rows if r.get("strategy")}
    # run_log join: arm / agent / ts per strategy id (latest wins)
    run_by_sid: dict[str, dict] = {}
    for r in run_rows:
        sid = (r.get("verdict") or {}).get("id")
        if sid:
            run_by_sid[sid] = r
    # elite-pool id -> strategy id (for explicit lineage edges); resolved to
    # unique node ids AFTER nodes are built (see rid_to_node remap below)
    elite_to_strategy: dict[str, str] = {}
    for r in run_rows:
        sid = (r.get("verdict") or {}).get("id")
        rid = r.get("id")
        if sid and rid:
            elite_to_strategy[str(rid)] = sid

    elite_sids = set()
    elite_grid = []
    for e in elite_rows:
        sid = elite_to_strategy.get(str(e.get("id")))
        elite_sids.add(sid or str(e.get("id")))
        elite_grid.append({
            "cell": e.get("cell"),
            "fitness": _num(e.get("fitness")),
            "title": str(e.get("title", ""))[:160],
            "strategy_id": sid,
            "ts": e.get("ts"),
        })

    # registry/run_log declare the frontmatter id; map it back to unique page-stem node ids.
    # When two pages share a declared id, the join prefers the exact stem match.
    rid_to_node: dict[str, str] = {}
    for n in nodes:
        if n["rid"] not in rid_to_node or n["rid"] == n["id"]:
            rid_to_node[n["rid"]] = n["id"]

    for n in nodes:
        reg = reg_by_id.get(n["rid"]) or {}
        run = run_by_sid.get(n["rid"]) or {}
        n["tier"] = reg.get("tier")
        n["dsr"] = _num(reg.get("dsr")) or n["metrics"].get("dsr")
        n["bar_at_test"] = _num(reg.get("promote_dsr"))
        n["arm"] = run.get("arm")
        n["agent"] = run.get("agent")
        n["elite"] = n["id"] in elite_sids
        n["ts"] = run.get("ts") or n.get("date")

    # remap elite-pool joins from declared ids to unique node ids
    elite_to_strategy = {k: rid_to_node.get(v, v) for k, v in elite_to_strategy.items()}
    elite_sids = {rid_to_node.get(s, s) for s in elite_sids}
    for n in nodes:
        n["elite"] = n["id"] in elite_sids
    for g in elite_grid:
        if g["strategy_id"]:
            g["strategy_id"] = rid_to_node.get(g["strategy_id"], g["strategy_id"])

    known = {n["id"] for n in nodes}
    rid_map = {n["rid"]: n["id"] for n in nodes
               if n["rid"] not in known or n["rid"] == n["id"]}

    # queued ghosts (not yet run)
    ghosts = []
    for q in queue_rows:
        if q.get("status") not in ("queued", "claimed"):
            continue
        prop = q.get("proposal") or {}
        title = str(prop.get("title", "(untitled)"))
        ghosts.append({
            "id": f"queued-{q.get('id')}",
            "page": None,
            "title": title[:200],
            "status": "queued" if q.get("status") == "queued" else "claimed",
            "family": "", "lane": lane_of("", title),
            "markets": [str(prop.get("market", ""))[:60]] if prop.get("market") else [],
            "date": None, "ts": None, "project": "crucible",
            "metrics": {}, "tier": None, "dsr": None, "bar_at_test": None,
            "arm": q.get("arm"), "agent": q.get("claimed_by"), "elite": False,
            "prereg": str(prop.get("signal_approach", ""))[:700] or None,
            "parent_ids": [elite_to_strategy.get(str(p)) for p in (q.get("parent_ids") or [])],
        })

    edges = _explicit_edges(run_rows, elite_to_strategy, known, rid_map)
    explicit_pairs = {(e["source"], e["target"]) for e in edges}
    edges += [e for e in _version_chains(nodes)
              if (e["source"], e["target"]) not in explicit_pairs]
    edges += _pairs_with_edges(queue_rows, run_rows, nodes)
    # ghost lineage: queued exploit-arm items point back at their elite parents
    for g in ghosts:
        for src in (g.pop("parent_ids", None) or []):
            if src and src in known:
                kind = {"refine": "refine", "orthogonal": "orthogonal",
                        "crossover": "crossover"}.get(g.get("arm") or "", "refine")
                edges.append({"source": src, "target": g["id"], "kind": kind, "inferred": False})

    all_nodes = nodes + ghosts

    # lanes ordered by activity, with premium concept-page status attached
    premia_status = {}
    for p in PREMIA.glob("*.md"):
        try:
            first = p.read_text(errors="replace")[:600]
            sm = re.search(r"status[:*\s]+([^\n|]+)", first, re.I)
            premia_status[p.stem] = (sm.group(1).strip()[:80] if sm else None)
        except Exception:
            pass
    lane_counts: dict[str, dict] = {}
    for n in all_nodes:
        lc = lane_counts.setdefault(n["lane"], {"total": 0, "fail": 0, "near_miss": 0,
                                                "pass": 0, "queued": 0})
        lc["total"] += 1
        key = n["status"] if n["status"] in lc else ("queued" if n["status"] == "claimed" else None)
        if key:
            lc[key] += 1
    lanes = [{
        "id": lane, "label": LANE_LABELS.get(lane, lane.title()), **counts,
        "premia_note": premia_status.get(lane),
    } for lane, counts in sorted(lane_counts.items(), key=lambda kv: -kv[1]["total"])]

    bar = max((_num(r.get("promote_dsr")) or 0 for r in reg_rows), default=0.9)
    stats = {
        "experiments": len(nodes),
        "queued": len(ghosts),
        "fails": sum(1 for n in nodes if n["status"] == "fail"),
        "near_misses": sum(1 for n in nodes if n["status"] == "near_miss"),
        "passes": sum(1 for n in nodes if n["status"] == "pass"),
        "lanes": len(lanes),
        "families_burned": len({r.get("family") for r in reg_rows if r.get("family")}),
        "fdr_bar": round(bar, 4),
        "elite_cells": len(elite_grid),
        "edges": len(edges),
        "explicit_edges": sum(1 for e in edges if not e.get("inferred")),
    }

    return {
        "generated_at": datetime.now().isoformat(),
        "stats": stats,
        "lanes": lanes,
        "nodes": all_nodes,
        "edges": edges,
        "elite_grid": elite_grid,
    }


@router.get("/map")
def forge_map(_auth: HTTPBasicCredentials = Depends(check_auth)) -> dict:
    now = time.time()
    if _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]
    try:
        data = _build()
    except Exception as e:  # never 500 — the map is an observability surface
        data = {"generated_at": datetime.now().isoformat(), "error": f"{type(e).__name__}: {e}",
                "stats": {}, "lanes": [], "nodes": [], "edges": [], "elite_grid": []}
    _CACHE["ts"], _CACHE["data"] = now, data
    return _CACHE["data"]
