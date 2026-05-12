"""Research API routes.

Phase 6 extraction from services/chat_server.py.

Routes:
  GET  /api/research/overview      — comprehensive research overview
  GET  /api/research/leaderboard   — best strategy/universe combos
  POST /api/research/prioritize    — update research priorities
  GET  /api/research/summary       — aggregated experiment stats
  GET  /api/research/experiments   — paginated experiment list
  GET  /api/research/strategies    — per-strategy stats + best params
  GET  /api/research/timeline      — daily experiment counts
  GET  /api/research/discoveries   — discovery pipeline runs
  GET  /api/research/brain         — brain knowledge entries
  GET  /api/research/coverage      — strategies × universes coverage matrix
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials

from services.auth import check_auth

router = APIRouter(prefix="/api/research", tags=["research"])
logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path("/root/atlas")


@router.get("/overview")
def research_overview(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """Comprehensive research overview: universes, engine status, daily metrics."""
    try:
        import json as _json
        from db.atlas_db import get_db

        # Load priorities config
        priorities_path = _PROJECT_ROOT / "config" / "research_priorities.json"
        priorities = {}
        if priorities_path.exists():
            with open(priorities_path) as f:
                pdata = _json.load(f)
                priorities = pdata.get("research_priorities", {})

        # Load research_best for best sharpes per strategy/universe
        with get_db() as db:
            # Per-universe stats from research_experiments
            universe_stats = {}
            for r in db.execute("""
                SELECT universe,
                       COUNT(*) as total_experiments,
                       SUM(CASE WHEN status='kept' THEN 1 ELSE 0 END) as kept,
                       MAX(sharpe) as best_sharpe,
                       MAX(created_at) as last_experiment
                FROM research_experiments
                GROUP BY universe
            """).fetchall():
                d = dict(r)
                universe_stats[d["universe"]] = d

            # Today's stats per universe
            today_stats = {}
            for r in db.execute("""
                SELECT universe,
                       COUNT(*) as experiments_today,
                       SUM(CASE WHEN status='kept' THEN 1 ELSE 0 END) as kept_today
                FROM research_experiments
                WHERE created_at >= date('now', 'localtime')
                GROUP BY universe
            """).fetchall():
                d = dict(r)
                today_stats[d["universe"]] = d

            # Best per strategy per universe from research_best
            best_by_universe = {}
            for r in db.execute(
                "SELECT strategy, universe, COALESCE(solo_sharpe, sharpe) AS sharpe, trades "
                "FROM research_best WHERE COALESCE(solo_sharpe, sharpe) > 0 ORDER BY sharpe DESC"
            ).fetchall():
                d = dict(r)
                uni = d["universe"]
                if uni not in best_by_universe:
                    best_by_universe[uni] = []
                best_by_universe[uni].append({"strategy": d["strategy"], "best_sharpe": d["sharpe"], "trades": d["trades"]})

            # Strategy breakdown per universe from experiments
            strat_breakdown = {}
            for r in db.execute("""
                SELECT universe, strategy, COUNT(*) as experiments,
                       MAX(sharpe) as best_sharpe,
                       SUM(CASE WHEN status='kept' THEN 1 ELSE 0 END) as kept
                FROM research_experiments
                GROUP BY universe, strategy
            """).fetchall():
                d = dict(r)
                uni = d["universe"]
                if uni not in strat_breakdown:
                    strat_breakdown[uni] = {}
                strat_breakdown[uni][d["strategy"]] = {
                    "best_sharpe": d["best_sharpe"],
                    "experiments": d["experiments"],
                    "kept": d["kept"]
                }

            # Build universe list
            all_universes = set(list(priorities.keys()) + list(universe_stats.keys()))
            universes = []
            for uid in sorted(all_universes):
                pri = priorities.get(uid, {})
                stats = universe_stats.get(uid, {})
                today = today_stats.get(uid, {})
                total_exp = stats.get("total_experiments", 0)
                kept_total = stats.get("kept", 0)
                exp_today = today.get("experiments_today", 0)
                kept_today_val = today.get("kept_today", 0)

                universes.append({
                    "id": uid,
                    "mode": pri.get("mode", "passive"),
                    "priority": pri.get("priority", "low"),
                    "best_sharpe": stats.get("best_sharpe", 0) or 0,
                    "total_experiments": total_exp,
                    "experiments_today": exp_today,
                    "kept_today": kept_today_val,
                    "keep_rate": round(kept_total / total_exp * 100, 1) if total_exp > 0 else 0,
                    "strategies": strat_breakdown.get(uid, {}),
                    "top_strategies": best_by_universe.get(uid, [])[:5],
                    "last_experiment": stats.get("last_experiment"),
                    "windows_per_day": pri.get("windows_per_day", 0),
                })

            # Engine status
            try:
                result = subprocess.run(
                    ["systemctl", "is-active", "atlas-research-window"],
                    capture_output=True, text=True, timeout=5,
                )
                engine_status = result.stdout.strip()
                if engine_status == "active":
                    engine_status = "running"
                elif engine_status == "inactive":
                    engine_status = "idle"
                else:
                    engine_status = "idle"
            except (subprocess.SubprocessError, OSError, ValueError) as e:  # systemctl call
                logger.debug("engine_status subprocess failed: %s", e)
                engine_status = "unknown"

            # Total all-time and daily aggregates
            totals = db.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN created_at >= date('now', 'localtime') THEN 1 ELSE 0 END) as today,
                       SUM(CASE WHEN status='kept' THEN 1 ELSE 0 END) as kept_all
                FROM research_experiments
            """).fetchone()

            # Experiments per day for last 14 days (sparkline data)
            daily_counts = [dict(r) for r in db.execute("""
                SELECT date(created_at) as date, COUNT(*) as count,
                       SUM(CASE WHEN status='kept' THEN 1 ELSE 0 END) as kept
                FROM research_experiments
                WHERE created_at >= date('now', '-14 days')
                GROUP BY date(created_at)
                ORDER BY date
            """).fetchall()]

            return JSONResponse(content={
                "universes": universes,
                "engine": {
                    "status": engine_status,
                    "total_experiments_all_time": totals["total"],
                    "experiments_today": totals["today"],
                    "kept_all_time": totals["kept_all"],
                    "daily_counts": daily_counts,
                },
            })
    except Exception as e:  # noqa: BLE001 — HTTP handler catch-all
        logger.exception("research_overview failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/leaderboard")
def research_leaderboard(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """Best strategy/universe combos ranked by Sharpe from research_best table."""
    try:
        from db.atlas_db import get_db
        with get_db() as db:
            rows = []
            for r in db.execute("""
                SELECT rb.strategy, rb.universe,
                       COALESCE(rb.solo_sharpe, rb.sharpe) AS sharpe,
                       rb.solo_sharpe, rb.portfolio_sharpe, rb.metric_type,
                       rb.trades, rb.max_dd_pct, rb.updated_at,
                       (SELECT COUNT(*) FROM research_experiments re
                        WHERE re.strategy = rb.strategy AND re.universe = rb.universe) as total_experiments
                FROM research_best rb
                WHERE COALESCE(rb.solo_sharpe, rb.sharpe) > 0
                ORDER BY COALESCE(rb.solo_sharpe, rb.sharpe) DESC
            """).fetchall():
                d = dict(r)
                rows.append(d)
            return JSONResponse(content={"leaderboard": rows})
    except Exception as e:  # noqa: BLE001 — HTTP handler catch-all
        logger.exception("research_leaderboard failed")
        raise HTTPException(status_code=500, detail=str(e))


# TODO: unused — not called by dashboard UI (admin-only endpoint)
@router.post("/prioritize")
async def research_prioritize(
    request: Request,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """Update priority for a universe in research_priorities.json."""
    import json as _json
    try:
        body = await request.json()
        universe = body.get("universe")
        priority = body.get("priority")  # high, medium, low
        action = body.get("action")  # pause, resume, or None

        if not universe:
            raise HTTPException(status_code=400, detail="universe required")

        priorities_path = _PROJECT_ROOT / "config" / "research_priorities.json"
        with open(priorities_path) as f:
            pdata = _json.load(f)

        rp = pdata.get("research_priorities", {})
        if universe not in rp:
            raise HTTPException(status_code=404, detail=f"Universe {universe} not found")

        if priority and priority in ("high", "medium", "low"):
            rp[universe]["priority"] = priority

        if action == "pause":
            rp[universe]["paused"] = True
        elif action == "resume":
            rp[universe].pop("paused", None)

        pdata["research_priorities"] = rp
        pdata["_updated"] = __import__("datetime").date.today().isoformat()

        with open(priorities_path, "w") as f:
            _json.dump(pdata, f, indent=2)

        return JSONResponse(content={"ok": True, "universe": universe, "updated": rp[universe]})
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — HTTP handler catch-all
        logger.exception("research_prioritize failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/summary")
def research_summary(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """Research overview: total experiments, keep rate, by strategy, by source."""
    try:
        from db.atlas_db import get_db
        with get_db() as db:
            # Total experiments
            total = db.execute("SELECT COUNT(*) as c FROM research_experiments").fetchone()["c"]
            kept = db.execute("SELECT COUNT(*) as c FROM research_experiments WHERE status='kept'").fetchone()["c"]

            # Last 7 days
            recent = db.execute(
                "SELECT COUNT(*) as c FROM research_experiments WHERE created_at >= datetime('now', '-7 days')"
            ).fetchone()["c"]

            # By strategy
            by_strategy = [dict(r) for r in db.execute("""
                SELECT strategy, COUNT(*) as total,
                       SUM(CASE WHEN status='kept' THEN 1 ELSE 0 END) as kept,
                       MAX(sharpe) as best_sharpe
                FROM research_experiments
                GROUP BY strategy ORDER BY total DESC
            """).fetchall()]

            # By source (experiment_type)
            by_source = [dict(r) for r in db.execute("""
                SELECT experiment_type as source, COUNT(*) as total
                FROM research_experiments
                GROUP BY experiment_type ORDER BY total DESC
            """).fetchall()]

            # Last research timestamp
            last_ts = db.execute(
                "SELECT MAX(created_at) as ts FROM research_experiments"
            ).fetchone()["ts"]

            # Distinct strategies
            strat_count = db.execute(
                "SELECT COUNT(DISTINCT strategy) as c FROM research_experiments"
            ).fetchone()["c"]

            return JSONResponse(content={
                "total_experiments": total,
                "kept_count": kept,
                "keep_rate": round(kept / total * 100, 1) if total > 0 else 0,
                "experiments_7d": recent,
                "strategies_count": strat_count,
                "last_research_ts": last_ts,
                "by_strategy": by_strategy,
                "by_source": by_source,
            })
    except Exception as e:  # noqa: BLE001 — HTTP handler catch-all
        logger.exception("research_summary failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/experiments")
def research_experiments(
    strategy: str = None,
    status: str = None,
    source: str = None,
    regime: str = None,
    limit: int = 50,
    offset: int = 0,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """Paginated experiment list with filters."""
    try:
        from db.atlas_db import get_db
        import json as _json
        with get_db() as db:
            query = "SELECT * FROM research_experiments WHERE 1=1"
            count_query = "SELECT COUNT(*) as c FROM research_experiments WHERE 1=1"
            params = []

            if strategy:
                query += " AND strategy=?"
                count_query += " AND strategy=?"
                params.append(strategy)
            if status:
                query += " AND status=?"
                count_query += " AND status=?"
                params.append(status)
            if source:
                query += " AND experiment_type=?"
                count_query += " AND experiment_type=?"
                params.append(source)
            if regime:
                query += " AND regime_state=?"
                count_query += " AND regime_state=?"
                params.append(regime)

            total = db.execute(count_query, params).fetchone()["c"]

            query += f" ORDER BY created_at DESC LIMIT {int(limit)} OFFSET {int(offset)}"
            rows = []
            for r in db.execute(query, params).fetchall():
                d = dict(r)
                if d.get("params_changed"):
                    try:
                        d["params_changed"] = _json.loads(d["params_changed"])
                    except (ValueError, TypeError):
                        pass
                rows.append(d)

            return JSONResponse(content={"experiments": rows, "total": total})
    except Exception as e:  # noqa: BLE001 — HTTP handler catch-all
        logger.exception("research_experiments failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/strategies")
def research_strategies(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """Per-strategy stats with best params from research_best."""
    try:
        from db.atlas_db import get_db
        import json as _json
        with get_db() as db:
            strategies = [dict(r) for r in db.execute("""
                SELECT
                    e.strategy,
                    COUNT(*) as total_experiments,
                    SUM(CASE WHEN e.status='kept' THEN 1 ELSE 0 END) as kept_count,
                    MAX(e.sharpe) as best_sharpe,
                    MAX(e.cagr_pct) as best_cagr,
                    MAX(CASE WHEN e.status='kept' THEN e.created_at END) as last_improvement
                FROM research_experiments e
                GROUP BY e.strategy
                ORDER BY best_sharpe DESC
            """).fetchall()]

            # Enrich with best params
            best_rows = {r["strategy"]: dict(r) for r in db.execute(
                "SELECT * FROM research_best"
            ).fetchall()}

            for s in strategies:
                best = best_rows.get(s["strategy"])
                if best and best.get("params"):
                    try:
                        s["best_params"] = _json.loads(best["params"])
                    except (ValueError, TypeError):
                        s["best_params"] = best["params"]
                else:
                    s["best_params"] = None

            # Enrich with research integrity fields (is_solo, solo_fraction, contamination_note)
            try:
                from research.integrity import check_solo
                for s in strategies:
                    _strategy = s.get("strategy", "")
                    _is_solo, _solo_frac, _note = check_solo(_strategy, universe="sp500")
                    s["is_solo"] = _is_solo
                    s["solo_fraction"] = _solo_frac
                    s["contamination_note"] = _note
            except Exception as _ie:
                logger.warning("integrity enrichment failed: %s", _ie)

            return JSONResponse(content={"strategies": strategies})
    except Exception as e:  # noqa: BLE001 — HTTP handler catch-all
        logger.exception("research_strategies failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/timeline")
def research_timeline(days: int = 30, _auth: HTTPBasicCredentials = Depends(check_auth)):
    """Daily experiment counts and running best Sharpe per strategy."""
    try:
        from db.atlas_db import get_db
        with get_db() as db:
            rows = [dict(r) for r in db.execute("""
                SELECT
                    DATE(created_at) as date,
                    strategy,
                    COUNT(*) as experiments,
                    MAX(sharpe) as best_sharpe,
                    SUM(CASE WHEN status='kept' THEN 1 ELSE 0 END) as kept
                FROM research_experiments
                WHERE created_at >= datetime('now', ?)
                GROUP BY DATE(created_at), strategy
                ORDER BY date
            """, (f"-{int(days)} days",)).fetchall()]

            # Organize into series by strategy
            series = {}
            dates = sorted(set(r["date"] for r in rows if r["date"]))
            for r in rows:
                strat = r["strategy"]
                if strat not in series:
                    series[strat] = []
                series[strat].append({
                    "date": r["date"],
                    "experiments": r["experiments"],
                    "best_sharpe": r["best_sharpe"],
                    "kept": r["kept"],
                })

            return JSONResponse(content={"dates": dates, "series": series})
    except Exception as e:  # noqa: BLE001 — HTTP handler catch-all
        logger.exception("research_timeline failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/discoveries")
def research_discoveries(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """Discovery pipeline runs."""
    try:
        from db.atlas_db import get_db
        import json as _json
        with get_db() as db:
            rows = []
            for r in db.execute("""
                SELECT * FROM research_discoveries ORDER BY created_at DESC LIMIT 50
            """).fetchall():
                d = dict(r)
                if d.get("paper_titles"):
                    try:
                        d["paper_titles"] = _json.loads(d["paper_titles"])
                    except (ValueError, TypeError):
                        pass
                rows.append(d)
            return JSONResponse(content={"discoveries": rows})
    except Exception as e:  # noqa: BLE001 — HTTP handler catch-all
        logger.exception("research_discoveries failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/brain")
def research_brain(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """Brain knowledge entries — params and patterns."""
    try:
        from db.atlas_db import get_db
        with get_db() as db:
            # Param summaries: aggregate by title (param name)
            params = [dict(r) for r in db.execute("""
                SELECT title as param_name,
                       COUNT(*) as tests,
                       COUNT(DISTINCT strategy) as strategies_tested,
                       SUM(CASE WHEN sharpe_delta > 0 THEN 1 ELSE 0 END) as improved,
                       AVG(sharpe_delta) as avg_sharpe_delta
                FROM research_brain
                WHERE entry_type='param'
                GROUP BY title
                ORDER BY tests DESC
            """).fetchall()]

            # Patterns
            patterns = [dict(r) for r in db.execute("""
                SELECT title as name, content as summary, source_file, updated_at
                FROM research_brain
                WHERE entry_type='pattern'
                ORDER BY updated_at DESC
            """).fetchall()]

            return JSONResponse(content={"params": params, "patterns": patterns})
    except Exception as e:  # noqa: BLE001 — HTTP handler catch-all
        logger.exception("research_brain failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/coverage")
def research_coverage(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """Coverage matrix: strategies x universes -> last-promotion-date + sharpe."""
    try:
        from db.atlas_db import get_db
        from datetime import datetime, timezone

        with get_db() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT strategy, universe, "
                "COALESCE(solo_sharpe, sharpe) AS sharpe, "
                "solo_sharpe, portfolio_sharpe, metric_type, trades, updated_at "
                "FROM research_best ORDER BY strategy, universe"
            ).fetchall()]

        strategies = sorted({r["strategy"] for r in rows})
        universes = sorted({r["universe"] for r in rows})

        now = datetime.now(timezone.utc)
        matrix: dict = {s: {u: None for u in universes} for s in strategies}
        for r in rows:
            updated_at_str = r.get("updated_at")
            age_days = None
            status = "never"
            if updated_at_str:
                try:
                    ts = datetime.fromisoformat(updated_at_str.replace(" ", "T"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    age_days = (now - ts).total_seconds() / 86400
                    if age_days < 7:
                        status = "fresh"
                    elif age_days < 14:
                        status = "stale"
                    else:
                        status = "very_stale"
                except (ValueError, TypeError):
                    pass
            matrix[r["strategy"]][r["universe"]] = {
                "sharpe": r["sharpe"],
                "trades": r["trades"],
                "updated_at": updated_at_str,
                "age_days": round(age_days, 1) if age_days is not None else None,
                "status": status,
            }

        return JSONResponse(content={
            "strategies": strategies,
            "universes": universes,
            "matrix": matrix,
            "generated_at": now.isoformat(),
        })
    except Exception as e:  # noqa: BLE001 — HTTP handler catch-all
        logger.exception("research_coverage failed")
        raise HTTPException(status_code=500, detail=str(e))
