"""Research database helper — lightweight SQLite logging for autoresearch.

All functions fail gracefully (log warning, never crash the research runner).
Uses the existing db.atlas_db.get_db() context manager.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("research.db")


def log_experiment(
    strategy: str,
    metrics: dict,
    params_changed: str,
    status: str,        # 'keep' or 'discard'
    description: str,
    source: str = "sweeper",    # 'sweeper', 'llm_loop', 'discovery'
    market: str = "sp500",
    stage: str = "",            # 'presort', 'solo', 'combined'
) -> None:
    """Insert one experiment result into research_experiments.

    Maps to the existing schema: status keep->kept, discard->discarded.
    Generates a unique text ID. Wraps everything in try/except so research never crashes.
    """
    try:
        from db.atlas_db import get_db
        exp_id = f"ar-{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        db_status = "kept" if status == "keep" else "discarded" if status == "discard" else status
        with get_db() as db:
            db.execute("""
                INSERT INTO research_experiments
                    (id, strategy, universe, experiment_type, params_changed, description,
                     sharpe, trades, max_dd_pct, profit_factor, cagr_pct, status,
                     recommendation, agent_id, completed_at, window_coverage_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                exp_id, strategy, market, source,
                params_changed if params_changed else None,
                description,
                float(metrics.get("sharpe", 0) or 0),
                int(metrics.get("total_trades", 0) or 0),
                float(metrics.get("max_drawdown_pct", 0) or 0),
                float(metrics.get("profit_factor", 0) or 0),
                float(metrics.get("cagr_pct", 0) or 0),
                db_status, description, "autoresearch",
                datetime.now(timezone.utc).isoformat(),
                float(metrics.get("window_coverage_pct", 100.0) or 100.0),
            ))
    except Exception as exc:
        logger.warning("log_experiment failed: %s", exc)


def log_session(
    mode: str,
    strategy: Optional[str] = None,
    started_at: Optional[str] = None,
) -> Optional[int]:
    """Start a research session. Returns session_id or None on failure."""
    try:
        from db.atlas_db import get_db
        ts = started_at or datetime.now(timezone.utc).isoformat()
        with get_db() as db:
            cursor = db.execute("""
                INSERT INTO research_sessions (started_at, mode, strategy, status)
                VALUES (?, ?, ?, 'running')
            """, (ts, mode, strategy))
            return cursor.lastrowid
    except Exception as exc:
        logger.warning("log_session failed: %s", exc)
        return None


def end_session(
    session_id: int,
    experiments_run: int = 0,
    experiments_kept: int = 0,
    status: str = "completed",
) -> None:
    """Mark a research session as completed."""
    try:
        from db.atlas_db import get_db
        ended = datetime.now(timezone.utc).isoformat()
        with get_db() as db:
            # Calculate duration from started_at
            row = db.execute("SELECT started_at FROM research_sessions WHERE id=?", (session_id,)).fetchone()
            duration = None
            if row and row["started_at"]:
                try:
                    start = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
                    duration = (datetime.now(timezone.utc) - start).total_seconds() / 60
                except Exception:
                    pass
            db.execute("""
                UPDATE research_sessions
                SET ended_at=?, experiments_run=?, experiments_kept=?,
                    duration_minutes=?, status=?
                WHERE id=?
            """, (ended, experiments_run, experiments_kept, duration, status, session_id))
    except Exception as exc:
        logger.warning("end_session failed: %s", exc)


def log_discovery(
    run_date: str,
    papers_found: int = 0,
    papers_filtered: int = 0,
    specs_extracted: int = 0,
    strategies_generated: int = 0,
    paper_titles: Optional[list] = None,
    status: str = "completed",
) -> None:
    """Log a discovery pipeline run."""
    try:
        from db.atlas_db import get_db
        with get_db() as db:
            db.execute("""
                INSERT INTO research_discoveries
                    (run_date, papers_found, papers_filtered, specs_extracted,
                     strategies_generated, paper_titles, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                run_date, papers_found, papers_filtered, specs_extracted,
                strategies_generated,
                json.dumps(paper_titles) if paper_titles else None,
                status,
            ))
    except Exception as exc:
        logger.warning("log_discovery failed: %s", exc)
