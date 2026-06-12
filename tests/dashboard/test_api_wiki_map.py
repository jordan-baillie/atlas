"""Research-map API (/api/forge/map) — builder unit tests over synthetic wiki fixtures.

The builder must: parse experiment frontmatter+verdicts, join registry/run_log/queue/elite,
normalize statuses, bucket lanes, build lineage edges (explicit > inferred), and never raise.
"""
import json

import pytest

from atlas.dashboard.api import wiki_map


# ------------------------------------------------------------------ fixtures
EXP_PASS = """---
id: amihud_x_v3
status: VALIDATED
family: illiquidity_premium
markets: ['US_smallmid_equity']
date: 2026-06-12
project: crucible
---
# Amihud tranched v3

## Pre-registration (FROZEN before running)
Kept frozen from elite parent; mutation = 3 overlapping monthly tranches.

## Verdict
- DSR 1.0 | CPCV 1.31 | PBO 0.05
- search Sharpe 1.89 -> holdout Sharpe 1.46
- full Sharpe 1.52 | maxDD -0.21 | trades 4000
"""

EXP_V2 = """---
id: amihud_x_v2
status: NEAR-MISS
family: illiquidity_premium
markets: ['US_smallmid_equity']
date: 2026-06-11
---
# Amihud hedged v2
"""

EXP_FAIL = """---
id: carry_y
status: FAIL
family: carry
markets: ['crypto']
date: 2026-06-10
---
# Crypto carry standalone
"""

# duplicate declared id across two pages (real wiki has defensive-bab.md / defensive_bab.md)
EXP_DUP_A = """---
id: dup_id
status: PASSED ALL GATES (first pass) — FORWARD-VALIDATING
family: defensive
date: 2026-06-09
---
# The real pass page
"""
EXP_DUP_B = """---
id: dup_id
status: FAIL
family: defensive
date: 2026-06-09
---
# The sibling fail page
"""


@pytest.fixture()
def wiki(tmp_path, monkeypatch):
    exp = tmp_path / "experiments"
    exp.mkdir()
    (exp / "amihud_x_v3.md").write_text(EXP_PASS)
    (exp / "amihud_x_v2.md").write_text(EXP_V2)
    (exp / "carry_y.md").write_text(EXP_FAIL)
    (exp / "dup-a.md").write_text(EXP_DUP_A)
    (exp / "dup_id.md").write_text(EXP_DUP_B)

    reg = tmp_path / ".registry"
    reg.mkdir()
    (reg / "hypothesis_registry.jsonl").write_text("\n".join([
        json.dumps({"strategy": "amihud_x_v3", "family": "illiquidity_premium",
                    "tier": "PROMOTE", "dsr": 1.0, "promote_dsr": 0.985, "passed_all": True}),
        json.dumps({"strategy": "carry_y", "family": "carry", "tier": "FAIL",
                    "dsr": 0.5, "promote_dsr": 0.95}),
    ]))

    q = tmp_path / ".queue"
    q.mkdir()
    (q / "queue.jsonl").write_text("\n".join([
        # done item whose run produced carry_y, proposal pairs with trend
        json.dumps({"id": "q1", "status": "done",
                    "proposal": {"title": "Crypto carry", "pairs_with": "the validated trend hedge"}}),
        # queued ghost from a refine arm with explicit elite parent
        json.dumps({"id": "q2", "status": "queued", "arm": "refine",
                    "parent_ids": ["elite-amihud-1"],
                    "proposal": {"title": "Amihud illiquidity tranche mutation",
                                 "market": "US equities",
                                 "signal_approach": "mutate hold period"}}),
    ]))

    el = tmp_path / ".elite"
    el.mkdir()
    (el / "pool.jsonl").write_text(json.dumps({
        "id": "elite-amihud-1", "fitness": 1.0, "title": "Amihud parent",
        "cell": "illiquidity|us_small|low"}) + "\n")

    premia = tmp_path / "premia"
    premia.mkdir()
    (premia / "carry.md").write_text("# Carry\nstatus: NEAR-MISS (crypto)\n")

    run_log = tmp_path / "run_log.jsonl"
    run_log.write_text("\n".join([
        # elite-pool id elite-amihud-1 was the run that produced amihud_x_v2
        json.dumps({"id": "elite-amihud-1", "queue_id": "q0", "arm": "explore",
                    "agent": "smith-1", "ts": "2026-06-11T03:30:00",
                    "verdict": {"id": "amihud_x_v2"}}),
        # explicit refine lineage: v2's elite entry -> v3
        json.dumps({"id": "run-v3", "queue_id": "q3", "arm": "refine",
                    "parent_ids": ["elite-amihud-1"], "agent": "smith-2",
                    "ts": "2026-06-12T03:30:00", "verdict": {"id": "amihud_x_v3"}}),
        json.dumps({"id": "run-carry", "queue_id": "q1", "arm": "explore",
                    "agent": "smith-3", "ts": "2026-06-10T03:30:00",
                    "verdict": {"id": "carry_y"}}),
    ]))

    monkeypatch.setattr(wiki_map, "WIKI", tmp_path)
    monkeypatch.setattr(wiki_map, "EXPERIMENTS", exp)
    monkeypatch.setattr(wiki_map, "REGISTRY", reg / "hypothesis_registry.jsonl")
    monkeypatch.setattr(wiki_map, "QUEUE", q / "queue.jsonl")
    monkeypatch.setattr(wiki_map, "ELITE", el / "pool.jsonl")
    monkeypatch.setattr(wiki_map, "PREMIA", premia)
    monkeypatch.setattr(wiki_map, "RUN_LOG", run_log)
    wiki_map._CACHE["data"] = None
    return tmp_path


# ------------------------------------------------------------------ tests
def test_nodes_parse_and_join(wiki):
    d = wiki_map._build()
    by_id = {n["id"]: n for n in d["nodes"]}
    n = by_id["amihud_x_v3"]
    assert n["status"] == "pass" and n["lane"] == "illiquidity"
    assert n["tier"] == "PROMOTE" and n["dsr"] == 1.0 and n["bar_at_test"] == 0.985
    assert n["metrics"]["holdout_sharpe"] == 1.46 and n["metrics"]["search_sharpe"] == 1.89
    assert n["arm"] == "refine" and n["agent"] == "smith-2"
    assert n["prereg"] and "tranches" in n["prereg"]
    assert by_id["carry_y"]["status"] == "fail" and by_id["carry_y"]["lane"] == "carry"


def test_duplicate_declared_ids_stay_unique(wiki):
    """Two pages declaring the same frontmatter id must yield two distinct nodes."""
    d = wiki_map._build()
    ids = [n["id"] for n in d["nodes"]]
    assert len(ids) == len(set(ids))
    assert "dup-a" in ids and "dup_id" in ids
    statuses = {n["id"]: n["status"] for n in d["nodes"]}
    assert statuses["dup-a"] == "pass" and statuses["dup_id"] == "fail"


def test_explicit_lineage_beats_inferred(wiki):
    d = wiki_map._build()
    refines = [e for e in d["edges"] if e["kind"] == "refine"
               and e["target"] == "amihud_x_v3" and e["source"] == "amihud_x_v2"]
    assert refines, "v2 -> v3 refine edge must exist"
    assert any(not e["inferred"] for e in refines), "explicit run_log lineage must be used"
    # no duplicate explicit+inferred edge for the same pair
    assert len(refines) == 1


def test_ghost_nodes_and_ghost_lineage(wiki):
    d = wiki_map._build()
    ghosts = [n for n in d["nodes"] if n["status"] == "queued"]
    assert len(ghosts) == 1 and ghosts[0]["arm"] == "refine"
    ge = [e for e in d["edges"] if e["target"] == ghosts[0]["id"]]
    assert ge and ge[0]["source"] == "amihud_x_v2" and not ge[0]["inferred"]


def test_pairs_with_edges(wiki):
    # carry_y's proposal pairs_with the trend hedge — but no trend node exists here,
    # so no pairs edge should be emitted (anchor missing -> graceful no-op)
    d = wiki_map._build()
    assert not [e for e in d["edges"] if e["kind"] == "pairs_with"]


def test_stats_and_lanes(wiki):
    d = wiki_map._build()
    s = d["stats"]
    assert s["experiments"] == 5 and s["queued"] == 1
    assert s["passes"] == 2 and s["fails"] == 2 and s["near_misses"] == 1
    assert s["fdr_bar"] == 0.985 and s["elite_cells"] == 1
    lanes = {l["id"]: l for l in d["lanes"]}
    assert lanes["illiquidity"]["total"] == 3  # v2 + v3 + ghost
    assert lanes["carry"]["premia_note"] and "NEAR-MISS" in lanes["carry"]["premia_note"]


def test_elite_grid_resolved(wiki):
    d = wiki_map._build()
    assert d["elite_grid"][0]["strategy_id"] == "amihud_x_v2"
    by_id = {n["id"]: n for n in d["nodes"]}
    assert by_id["amihud_x_v2"]["elite"] is True


def test_never_raises_on_missing_everything(tmp_path, monkeypatch):
    for attr in ("WIKI", "EXPERIMENTS", "REGISTRY", "QUEUE", "ELITE", "PREMIA", "RUN_LOG"):
        monkeypatch.setattr(wiki_map, attr, tmp_path / "nope" / attr.lower())
    wiki_map._CACHE["data"] = None
    d = wiki_map._build()
    assert d["nodes"] == [] and d["edges"] == [] and d["stats"]["experiments"] == 0


def test_status_normalization():
    f = wiki_map._norm_status
    assert f("PASSED ALL GATES (first forge full-gate pass) — FORWARD-VALIDATING") == "pass"
    assert f("VALIDATED") == "pass"
    assert f("NEAR-MISS") == "near_miss"
    assert f("FAIL (edge-bound, confirmed)") == "fail"
    assert f("REJECTED-MCPT") == "fail"
    assert f("ORPHANED 2026-06-10 (structure validated; ...)") == "closed"
    assert f("TERMINATED 2026-06-10 — project killed") == "closed"
    assert f("ARCHIVED (48 real edges found, not deployed)") == "closed"


def test_lane_bucketing():
    f = wiki_map.lane_of
    assert f("illiquidity_premium", "Amihud tranched") == "illiquidity"
    assert f("equity_value_momentum", "Value x Momentum small-cap") == "value_momentum"
    assert f("defensive", "Betting-Against-Beta") == "low_risk"
    assert f("crypto_carry_trend_combo", "Funding carry x trend two-premium book") == "multi"
    assert f("macro_announcement_premium", "Pre-FOMC duration") == "event"
