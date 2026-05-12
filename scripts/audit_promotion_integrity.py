#!/usr/bin/env python3
"""Audit promotion integrity and enrich research/best/*.json files.

TASK A (Deliverable 1 + 6): Adds is_solo / solo_fraction / contamination_note
to every research_best JSON file, then audits all lifecycle promotions to flag
those that cited portfolio-contaminated metrics.

Output:
  data/audit/promotion_integrity_2026-05-12.json
  (mutates all research/best/*.json in-place)

Safe to re-run: idempotent on the is_solo enrichment.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ATLAS_ROOT = Path(__file__).resolve().parent.parent
BEST_DIR = ATLAS_ROOT / "research" / "best"
AUDIT_DIR = ATLAS_ROOT / "data" / "audit"
AUDIT_FILE = AUDIT_DIR / "promotion_integrity_2026-05-12.json"
DB_PATH = ATLAS_ROOT / "data" / "atlas.db"
PROMO_LOG = ATLAS_ROOT / "data" / "promotion_log.json"


# ─── JSON loading (handles NaN) ───────────────────────────────────────────────

def _safe_load_json(path: Path) -> dict[str, Any]:
    """Load JSON, replacing Python-invalid NaN/Infinity with None."""
    text = path.read_text()
    text = text.replace(": NaN", ": null").replace(":NaN", ":null")
    text = text.replace(": Infinity", ": null").replace(":Infinity", ":null")
    text = text.replace(": -Infinity", ": null").replace(":-Infinity", ":null")
    return json.loads(text)


# ─── Deliverable 1: Enrich JSON files ────────────────────────────────────────

def _compute_integrity_fields(
    data: dict[str, Any],
) -> tuple[bool | None, float | None, str | None]:
    """Return (is_solo, solo_fraction, contamination_note) for a best file."""
    strategy = data.get("strategy", "")
    metrics = data.get("metrics", {})
    total_trades: int = metrics.get("total_trades") or 0
    bd: dict | None = metrics.get("strategy_breakdown")

    if not bd or total_trades == 0:
        return (None, None, "No trades recorded — backtest not yet run or no signals fired.")

    solo_trades: int = (bd.get(strategy) or {}).get("trades", 0)
    frac: float = solo_trades / total_trades

    if frac >= 0.50:
        return (True, round(frac, 4), None)

    # Contaminated — find dominant other strategy
    others = {k: (v.get("trades") or 0) for k, v in bd.items() if k != strategy}
    if others:
        dom_name, dom_trades = max(others.items(), key=lambda x: x[1])
        dom_pct = round((dom_trades / total_trades) * 100, 1)
    else:
        dom_name, dom_pct = "unknown", 0.0

    note = (
        f"Headline metrics are portfolio-contaminated. "
        f"Dominant strategy: {dom_name} ({dom_pct}%). "
        f"True solo performance unknown — see task #327."
    )
    return (False, round(frac, 4), note)


def enrich_all_json_files() -> dict[str, int]:
    """Add is_solo/solo_fraction/contamination_note to every research/best/*.json.

    Returns counts: {'solo': N, 'contaminated': N, 'no_breakdown': N, 'total': N}
    """
    counts = {"solo": 0, "contaminated": 0, "no_breakdown": 0, "total": 0}

    for fpath in sorted(BEST_DIR.glob("*.json")):
        counts["total"] += 1
        data = _safe_load_json(fpath)

        is_solo, solo_frac, note = _compute_integrity_fields(data)

        # Write back (always update, even if already present — idempotent)
        data["is_solo"] = is_solo
        data["solo_fraction"] = solo_frac
        if note is not None:
            data["contamination_note"] = note
        elif "contamination_note" in data:
            del data["contamination_note"]  # remove stale note for clean files

        fpath.write_text(json.dumps(data, indent=2))

        if is_solo is True:
            counts["solo"] += 1
        elif is_solo is False:
            counts["contaminated"] += 1
        else:
            counts["no_breakdown"] += 1

        cat = "SOLO" if is_solo is True else ("CONTAMINATED" if is_solo is False else "NO_BREAKDOWN")
        logger.info("  %-50s %s (solo_frac=%s)", fpath.name, cat, solo_frac)

    return counts


# ─── Deliverable 6: Audit promotions ─────────────────────────────────────────

def _get_promotions_from_db() -> list[dict[str, Any]]:
    """Load all transition-to-LIVE/PAPER events from strategy_lifecycle_history."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT strategy, universe, to_state, transitioned_at, reason, auto_promotion_id
            FROM strategy_lifecycle_history
            WHERE to_state IN ('LIVE', 'PAPER')
            ORDER BY transitioned_at
        """).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("Could not query strategy_lifecycle_history: %s", exc)
        return []


def _get_promotions_from_log() -> list[dict[str, Any]]:
    """Fallback: load from promotion_log.json."""
    if not PROMO_LOG.exists():
        return []
    try:
        raw = json.loads(PROMO_LOG.read_text())
        if isinstance(raw, list):
            return [
                {
                    "strategy": r.get("strategy", ""),
                    "universe": r.get("universe", "sp500"),
                    "to_state": r.get("to_state", "LIVE"),
                    "transitioned_at": r.get("ts", ""),
                    "reason": "",
                    "auto_promotion_id": r.get("auto_promotion_id"),
                }
                for r in raw
            ]
    except Exception as exc:
        logger.warning("Could not parse promotion_log.json: %s", exc)
    return []


def _lookup_best_file(strategy: str, universe: str) -> Path | None:
    """Find the research/best JSON file for this strategy+universe."""
    # Non-sp500 universes have {strategy}_{universe}.json
    if universe and universe != "sp500":
        candidate = BEST_DIR / f"{strategy}_{universe}.json"
        if candidate.exists():
            return candidate
    # sp500 or fallback
    candidate = BEST_DIR / f"{strategy}.json"
    if candidate.exists():
        return candidate
    # Last resort: {strategy}_{universe}.json for sp500
    candidate = BEST_DIR / f"{strategy}_{universe}.json"
    if candidate.exists():
        return candidate
    return None


def _lookup_cited_sharpe_from_db(strategy: str, universe: str) -> float | None:
    """Look up the Sharpe from research_best table (what was cited at promotion time)."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT sharpe FROM research_best WHERE strategy=? AND universe=? LIMIT 1",
            (strategy, universe),
        ).fetchone()
        conn.close()
        if row:
            return row["sharpe"]
    except Exception as exc:
        logger.debug("research_best lookup failed for %s/%s: %s", strategy, universe, exc)
    return None


def audit_promotions() -> dict[str, Any]:
    """Audit all promotion events; return the audit dict."""
    promotions = _get_promotions_from_db()
    if not promotions:
        logger.info("No DB promotions found — falling back to promotion_log.json")
        promotions = _get_promotions_from_log()

    events = []
    for promo in promotions:
        strategy = promo.get("strategy", "")
        universe = promo.get("universe", "sp500")
        promoted_at = promo.get("transitioned_at", "")
        to_state = promo.get("to_state", "LIVE")
        reason = promo.get("reason", "")

        # Look up current file integrity
        best_path = _lookup_best_file(strategy, universe)
        file_is_solo: bool | None = None
        file_solo_fraction: float | None = None
        cited_sharpe: float | None = None

        if best_path and best_path.exists():
            try:
                data = _safe_load_json(best_path)
                file_is_solo = data.get("is_solo")
                file_solo_fraction = data.get("solo_fraction")
                cited_sharpe = data.get("metrics", {}).get("sharpe")
            except Exception as exc:
                logger.warning("Could not read %s: %s", best_path, exc)

        # Also try DB for cited_sharpe (more accurate)
        db_sharpe = _lookup_cited_sharpe_from_db(strategy, universe)
        if db_sharpe is not None:
            cited_sharpe = db_sharpe

        # Verdict
        if file_is_solo is False:
            verdict = "CONTAMINATED_AT_PROMOTION"
        elif file_is_solo is True:
            verdict = "CLEAN"
        elif file_is_solo is None and best_path is None:
            verdict = "NO_FILE"
        else:
            # null (no_breakdown) — no trades, can't say
            verdict = "NO_TRADES_RECORDED"

        events.append({
            "strategy": strategy,
            "universe": universe,
            "to_state": to_state,
            "promoted_at": promoted_at,
            "reason": reason[:80] if reason else "",
            "cited_sharpe": cited_sharpe,
            "file_is_solo_now": file_is_solo,
            "file_solo_fraction": file_solo_fraction,
            "verdict": verdict,
        })

    clean = sum(1 for e in events if e["verdict"] == "CLEAN")
    contaminated = sum(1 for e in events if e["verdict"] == "CONTAMINATED_AT_PROMOTION")
    no_file = sum(1 for e in events if e["verdict"] in ("NO_FILE", "NO_TRADES_RECORDED"))

    audit = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "promotions_audited": len(events),
        "events": events,
        "summary": {
            "clean": clean,
            "contaminated": contaminated,
            "no_file_or_no_trades": no_file,
        },
    }
    return audit


# ─── CLI entry ────────────────────────────────────────────────────────────────

def main() -> int:
    logger.info("=== Research best JSON enrichment ===")
    counts = enrich_all_json_files()
    logger.info(
        "Enrichment complete: %d total | %d SOLO | %d CONTAMINATED | %d NO_BREAKDOWN",
        counts["total"], counts["solo"], counts["contaminated"], counts["no_breakdown"],
    )

    logger.info("\n=== Promotion integrity audit ===")
    audit = audit_promotions()

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_FILE.write_text(json.dumps(audit, indent=2))
    logger.info("Audit written to %s", AUDIT_FILE)

    summary = audit["summary"]
    logger.info(
        "Promotions audited: %d | CLEAN: %d | CONTAMINATED: %d | NO_FILE/NOTRADES: %d",
        audit["promotions_audited"],
        summary["clean"],
        summary["contaminated"],
        summary.get("no_file_or_no_trades", 0),
    )

    if summary["contaminated"] > 0:
        logger.warning(
            "%d promotion(s) used contaminated metrics — see %s",
            summary["contaminated"],
            AUDIT_FILE,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
