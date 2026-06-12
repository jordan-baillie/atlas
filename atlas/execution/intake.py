"""atlas.execution.intake — consume crucible's one-direction file artifacts (#34).

The 2026-06-12 review end-state: crucible PRODUCES versioned artifacts under
data/live/<book>/, atlas CONSUMES them. Nobody imports across the repo seam.

Artifacts (schema_version 1):
  deploy_request.json     {name, strategy_path, capital, broker, tif, expectation, requested_at}
  lifecycle_verdict.json  {book, asof, lifecycle, gates_all_pass, n_days, decay, watch}

Modes:
  shadow (default) — read artifacts, compute what the registry SHOULD look like,
      compare against the actual registry (which crucible still mutates directly
      during the soak), log divergence to data/live/intake_soak.jsonl. NO writes.
  apply — actually apply artifacts to the registry. Enabled only after a clean
      >=7-day soak, when crucible's direct mutation paths are deleted.

Run: python3 -m atlas.execution.intake [--apply]   (daily loop runs shadow mode)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from atlas.kernel.paths import LIVE_DATA_DIR as LIVE_DATA
from atlas.execution import registry

logger = logging.getLogger(__name__)

SUPPORTED_SCHEMA = 1
SOAK_LOG = LIVE_DATA / "intake_soak.jsonl"


def _read(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("intake: unreadable artifact %s: %s", p, e)
        return None
    sv = d.get("schema_version")
    if sv != SUPPORTED_SCHEMA:
        logger.warning("intake: %s has schema_version=%r (supported: %s) — ignored", p, sv, SUPPORTED_SCHEMA)
        return None
    return d


def check_book(book_dir: Path, reg_by_name: dict) -> list[dict]:
    """Shadow-compare one book's artifacts against the live registry. Returns divergences."""
    out = []
    name = book_dir.name
    req = _read(book_dir / "deploy_request.json")
    if req:
        s = reg_by_name.get(req["name"])
        if s is None:
            out.append({"book": name, "artifact": "deploy_request", "divergence": "not_in_registry"})
        else:
            for k in ("capital", "broker", "tif"):
                if getattr(s, k, None) != req.get(k):
                    out.append({"book": name, "artifact": "deploy_request", "divergence": f"{k}_mismatch",
                                "registry": getattr(s, k, None), "artifact_value": req.get(k)})
    ver = _read(book_dir / "lifecycle_verdict.json")
    if ver:
        s = reg_by_name.get(ver["book"])
        if s is None:
            out.append({"book": name, "artifact": "lifecycle_verdict", "divergence": "not_in_registry"})
        elif (getattr(s, "lifecycle", "shadow") or "shadow") != ver["lifecycle"]:
            out.append({"book": name, "artifact": "lifecycle_verdict", "divergence": "lifecycle_mismatch",
                        "registry": getattr(s, "lifecycle", None), "artifact_value": ver["lifecycle"]})
    return out


def apply_book(book_dir: Path, reg_by_name: dict) -> list[str]:
    """APPLY mode: artifacts drive the registry (post-soak, crucible's direct paths deleted)."""
    actions = []
    name = book_dir.name
    req = _read(book_dir / "deploy_request.json")
    if req and req["name"] not in reg_by_name:
        from atlas.execution.providers import deploy_pass
        deploy_pass(req["name"], capital=req["capital"], broker=req["broker"],
                    expectation=req.get("expectation") or {},
                    strategy_path=req.get("strategy_path", ""), tif=req.get("tif", "opg"))
        actions.append(f"deployed {req['name']}")
    ver = _read(book_dir / "lifecycle_verdict.json")
    if ver and ver["book"] in reg_by_name:
        cur = getattr(reg_by_name[ver["book"]], "lifecycle", "shadow") or "shadow"
        if cur == "retired" and ver["lifecycle"] != "retired":
            logger.warning("intake: refusing to un-retire %s from artifact", ver["book"])
        elif cur != ver["lifecycle"]:
            registry.update(ver["book"], lifecycle=ver["lifecycle"])
            actions.append(f"{ver['book']} lifecycle {cur} -> {ver['lifecycle']}")
    return actions


def main(apply: bool = False) -> int:
    if not LIVE_DATA.exists():
        return 0
    reg_by_name = {s.name: s for s in registry.load()}
    books = [p for p in LIVE_DATA.iterdir() if p.is_dir() and p.name != "daily"]
    if apply:
        for b in books:
            for a in apply_book(b, reg_by_name):
                print(f"[intake] {a}")
        return 0
    divergences = []
    for b in books:
        divergences += check_book(b, reg_by_name)
    row = {"ts": datetime.now().isoformat(timespec="seconds"),
           "n_books": len(books), "divergences": divergences}
    SOAK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SOAK_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")
    if divergences:
        try:
            from atlas.kernel.notify import send_message
            send_message("⚠️ INTAKE SOAK DIVERGENCE (#34)\n"
                         + "\n".join(json.dumps(d, default=str)[:200] for d in divergences[:5])
                         + "\nArtifact cutover BLOCKED until explained.")
        except Exception:
            logger.warning("intake: divergence Telegram failed")
        print(f"[intake] {len(divergences)} divergence(s) — see {SOAK_LOG}")
        return 1
    print(f"[intake] shadow-compared {len(books)} book(s) — registry matches artifacts")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(apply="--apply" in sys.argv))
