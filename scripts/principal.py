#!/usr/bin/env python3
"""Atlas Principal — 24/7 Research Director Daemon.

Runs every 30 minutes. Gathers system state, asks the LLM (via pi + atlas-director
skill) what to do, then executes the directives: queue experiments, promote
candidates, retire stagnant strategies, restart services, rebalance partitions.

Usage:
    python3 scripts/principal.py [--once] [--dry-run] [--cycle-minutes N]

Systemd service: /etc/systemd/system/atlas-principal.service

Paths:
    Heartbeat:  /tmp/principal-heartbeat.json
    Stop file:  /tmp/principal-stop
    Directives: /tmp/directives/{agent}_{ts}.json
    Log:        logs/principal.log
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Project Setup ──────────────────────────────────────────────────────────

PROJECT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT)
sys.path.insert(0, str(PROJECT))

# ── Constants ──────────────────────────────────────────────────────────────

HEARTBEAT_PATH   = Path("/tmp/principal-heartbeat.json")
STOP_PATH        = Path("/tmp/principal-stop")
DIRECTIVES_DIR   = Path("/tmp/directives")
LOG_PATH         = PROJECT / "logs" / "principal.log"
QUEUE_PATH       = PROJECT / "research" / "queue.json"
JOURNAL_PATH     = PROJECT / "research" / "journal.json"
EXPERIMENTS_DIR  = PROJECT / "research" / "experiments"
CANDIDATES_DIR   = PROJECT / "config" / "candidates"
BEST_DIR         = PROJECT / "research" / "best"

SKILL_NAME       = "atlas-director"
MODEL            = "claude-sonnet-4-6"
DEFAULT_CYCLE_MIN = 30
PI_TIMEOUT       = 300          # 5 min max for LLM call
MAX_JOURNAL_ROWS = 40           # recent journal entries to include in context
MAX_EXP_ROWS     = 20           # recent experiment results to include

# ── Logging ────────────────────────────────────────────────────────────────

logger = logging.getLogger("principal")


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [principal] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_PATH, mode="a"),
        ],
        force=True,
    )
    for noisy in ("urllib3", "matplotlib", "numexpr"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ── Stop / Heartbeat ───────────────────────────────────────────────────────

def should_stop() -> bool:
    return STOP_PATH.exists()


def write_heartbeat(phase: str, cycle: int, **extra) -> None:
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pid":       os.getpid(),
        "phase":     phase,
        "cycle":     cycle,
        "status":    "running",
        **extra,
    }
    tmp = HEARTBEAT_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(HEARTBEAT_PATH)
    except OSError as e:
        logger.debug("Heartbeat write failed: %s", e)


# ── State Gathering ────────────────────────────────────────────────────────

def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _read_heartbeat(name: str) -> dict:
    """Read a daemon heartbeat by name (research-daemon, sage, etc.)."""
    p = Path(f"/tmp/{name}-heartbeat.json")
    hb = _read_json(p, {})
    if not hb:
        return {"status": "missing", "timestamp": None}
    ts = hb.get("timestamp")
    if ts:
        try:
            age_s = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
            hb["age_minutes"] = round(age_s / 60, 1)
            if age_s > 3600:
                hb["status"] = "stale"
        except Exception:
            pass
    return hb


def _service_status(name: str) -> str:
    """Return systemd service status string."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "error"


def _queue_summary() -> dict:
    """Summarise the research queue by status and priority."""
    queue = _read_json(QUEUE_PATH, [])
    if not isinstance(queue, list):
        queue = []

    summary = {
        "total": len(queue),
        "by_status": {},
        "by_priority": {},
        "queued_items": [],
    }
    for entry in queue:
        status   = entry.get("status", "unknown")
        priority = entry.get("priority", "?")
        summary["by_status"][status]   = summary["by_status"].get(status, 0) + 1
        summary["by_priority"][priority] = summary["by_priority"].get(priority, 0) + 1

        if status == "queued":
            summary["queued_items"].append({
                "id":            entry.get("id"),
                "strategy_name": entry.get("strategy_name"),
                "method":        entry.get("method"),
                "priority":      priority,
                "hypothesis":    (entry.get("hypothesis") or "")[:120],
            })

    return summary


def _recent_journal(n: int = MAX_JOURNAL_ROWS) -> list:
    """Return the last N journal entries (most recent first)."""
    entries = _read_json(JOURNAL_PATH, [])
    if not isinstance(entries, list):
        return []
    recent = entries[-n:]
    recent.reverse()
    return [
        {
            "experiment_id": e.get("experiment_id"),
            "strategy":      e.get("strategy"),
            "verdict":       e.get("verdict"),
            "timestamp":     e.get("timestamp"),
            "key_metrics":   {
                k: round(v, 4) if isinstance(v, float) else v
                for k, v in (e.get("key_metrics") or {}).items()
                if k in ("sharpe", "cagr_pct", "max_drawdown_pct",
                         "win_rate_pct", "profit_factor", "total_trades")
            },
        }
        for e in recent
    ]


def _best_results() -> dict:
    """Read best-known params and their sharpe for each strategy."""
    best = {}
    if BEST_DIR.exists():
        for f in sorted(BEST_DIR.glob("*.json")):
            data = _read_json(f, {})
            if data:
                best[f.stem] = {
                    "sharpe": data.get("sharpe", data.get("best_sharpe")),
                    "params": data.get("params", data.get("best_params", {})),
                    "updated": data.get("updated_at", data.get("timestamp")),
                }
    return best


def _candidates_summary() -> list:
    """List staged candidate configs waiting for promotion."""
    if not CANDIDATES_DIR.exists():
        return []
    items = []
    for f in sorted(CANDIDATES_DIR.glob("*.json")):
        data = _read_json(f, {})
        items.append({
            "file":    f.name,
            "market":  data.get("market", "unknown"),
            "created": data.get("created_at", data.get("timestamp")),
        })
    return items


def _recent_experiments(n: int = MAX_EXP_ROWS) -> list:
    """Return the N most recently modified experiment results."""
    if not EXPERIMENTS_DIR.exists():
        return []
    files = sorted(
        (f for f in EXPERIMENTS_DIR.glob("*.json") if not f.name.startswith("bak_")),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )[:n]

    results = []
    for f in files:
        data = _read_json(f, {})
        queue_entry = data.get("queue_entry", {})
        outputs     = data.get("outputs", {})
        metrics     = (
            outputs.get("metrics")
            or outputs.get("best_metrics")
            or outputs.get("combined_metrics")
            or {}
        )
        results.append({
            "id":            data.get("id") or f.stem,
            "strategy":      queue_entry.get("strategy_name") or data.get("strategy_name"),
            "method":        queue_entry.get("method") or data.get("method"),
            "status":        queue_entry.get("status") or data.get("status"),
            "verdict":       data.get("verdict"),
            "sharpe":        round(metrics.get("sharpe", 0), 4) if metrics else None,
            "max_dd_pct":    round(metrics.get("max_drawdown_pct", 0), 2) if metrics else None,
            "trades":        metrics.get("total_trades"),
            "hypothesis":    (queue_entry.get("hypothesis") or "")[:100],
        })
    return results


def gather_state() -> dict:
    """Collect full system state for the Director LLM."""
    logger.info("Gathering system state...")

    research_hb = _read_heartbeat("research-daemon")
    sage_hb     = _read_heartbeat("sage")
    principal_hb = _read_heartbeat("principal")

    state = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": {
            "atlas-research-daemon": {
                "systemd": _service_status("atlas-research-daemon"),
                "heartbeat": research_hb,
            },
            "sage": {
                "systemd": _service_status("atlas-sage"),
                "heartbeat": sage_hb,
            },
            "atlas-director": {
                "systemd": _service_status("atlas-principal"),
                "heartbeat": principal_hb,
            },
        },
        "queue": _queue_summary(),
        "best_results": _best_results(),
        "candidates": _candidates_summary(),
        "recent_experiments": _recent_experiments(),
        "recent_journal": _recent_journal(),
    }

    logger.info(
        "State gathered: queue=%d, experiments=%d, candidates=%d",
        state["queue"]["total"],
        len(state["recent_experiments"]),
        len(state["candidates"]),
    )
    return state


# ── LLM Call ──────────────────────────────────────────────────────────────

def _build_prompt(state: dict) -> str:
    """Build the prompt string passed to pi."""
    queue  = state["queue"]
    queued = queue.get("queued_items", [])[:10]

    recent_journal_text = ""
    for e in state["recent_journal"][:15]:
        metrics = e.get("key_metrics") or {}
        sharpe  = metrics.get("sharpe", "?")
        verdict = e.get("verdict", "?")
        recent_journal_text += (
            f"  - [{verdict}] {e.get('strategy','?')} / {e.get('experiment_id','?')}"
            f"  sharpe={sharpe}\n"
        )

    best_text = ""
    for name, info in state["best_results"].items():
        best_text += f"  - {name}: sharpe={info.get('sharpe','?')}\n"

    queued_text = ""
    for item in queued:
        queued_text += (
            f"  - [{item.get('priority','?')}] {item.get('strategy_name','?')}"
            f" / {item.get('method','?')}: {item.get('hypothesis','')[:80]}\n"
        )

    services_text = ""
    for svc, info in state["services"].items():
        hb  = info.get("heartbeat", {})
        age = hb.get("age_minutes", "?")
        services_text += (
            f"  - {svc}: systemd={info['systemd']}"
            f"  hb_status={hb.get('status','?')}"
            f"  age={age}min\n"
        )

    candidates_text = ""
    for c in state["candidates"]:
        candidates_text += f"  - {c['file']} (market={c['market']})\n"
    if not candidates_text:
        candidates_text = "  (none)\n"

    return f"""You are the Atlas Research Director.

Current time: {state['timestamp']}

## System Status
{services_text}

## Research Queue
Total entries: {queue['total']}
By status: {queue['by_status']}
Next queued experiments (up to 10):
{queued_text or '  (queue is empty)'}

## Best Known Results (per strategy)
{best_text or '  (no best results yet)'}

## Staged Candidates (awaiting promotion)
{candidates_text}

## Recent Experiment Journal (last 15)
{recent_journal_text or '  (no recent experiments)'}

## Recent Experiment Details (last 20)
{json.dumps(state['recent_experiments'][:20], indent=2)}

---

Review the state above and respond with a JSON directive object as described in your skill instructions.
Analyse trends, identify bottlenecks, flag risks, and issue the most impactful instructions for the next 30 minutes.
"""


def call_llm(state: dict) -> str | None:
    """Call pi with the atlas-director skill and return raw output."""
    prompt = _build_prompt(state)
    cmd = [
        "pi", "--print", "--no-session",
        "--model", MODEL,
        "--skill", SKILL_NAME,
        prompt,
    ]
    logger.info("Calling LLM (model=%s, skill=%s)...", MODEL, SKILL_NAME)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=PI_TIMEOUT,
            cwd=str(PROJECT),
            env={**os.environ, "PI_NON_INTERACTIVE": "1"},
        )
        if result.returncode != 0:
            logger.error("pi exited %d. stderr: %s", result.returncode, result.stderr[-500:])
            return None
        return result.stdout
    except subprocess.TimeoutExpired:
        logger.error("LLM call timed out after %ds", PI_TIMEOUT)
        return None
    except Exception as e:
        logger.error("LLM call failed: %s", e)
        return None


# ── Response Parsing ───────────────────────────────────────────────────────

def parse_response(raw: str) -> dict | None:
    """Extract and validate JSON directive block from pi output."""
    if not raw:
        return None

    # Try direct parse (whole output is JSON)
    stripped = raw.strip()
    if stripped.startswith("{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    # Extract from markdown code fence: ```json ... ```
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Last resort: find first { ... } block
    match = re.search(r"(\{.*\})", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    logger.error("Could not parse JSON from LLM response. Raw (first 500):\n%s", raw[:500])
    return None


# ── Action Execution ───────────────────────────────────────────────────────

def _write_directive(agent: str, action: str, experiments: list, reasoning: str) -> Path:
    """Write a directive file for an agent."""
    DIRECTIVES_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = DIRECTIVES_DIR / f"{agent}_{ts}.json"
    directive = {
        "target_agent": agent,
        "action":       action,
        "experiments":  experiments,
        "reasoning":    reasoning,
        "issued_at":    datetime.now(timezone.utc).isoformat(),
        "issued_by":    "principal",
    }
    path.write_text(json.dumps(directive, indent=2))
    logger.info("Directive written → %s (%s)", path.name, action)
    return path


def _add_queue_entry(entry: dict) -> bool:
    """Append a new experiment to queue.json."""
    import fcntl
    try:
        with open(QUEUE_PATH, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            queue = json.load(f)
            if not isinstance(queue, list):
                queue = []
            # Deduplicate by id
            existing_ids = {e.get("id") for e in queue}
            new_id = entry.get("id")
            if new_id and new_id in existing_ids:
                logger.info("Queue entry %s already exists — skipping", new_id)
                fcntl.flock(f, fcntl.LOCK_UN)
                return False
            entry.setdefault("status", "queued")
            entry.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            entry.setdefault("claimed_by", None)
            entry.setdefault("claimed_at", None)
            queue.append(entry)
            f.seek(0)
            json.dump(queue, f, indent=2)
            f.truncate()
            fcntl.flock(f, fcntl.LOCK_UN)
        logger.info("Queued experiment: %s", new_id or entry.get("strategy_name"))
        return True
    except Exception as e:
        logger.error("Failed to queue entry: %s", e)
        return False


def _restart_service(service: str, dry_run: bool) -> bool:
    """Restart a systemd service."""
    logger.info("%sRestarting service: %s", "[DRY RUN] " if dry_run else "", service)
    if dry_run:
        return True
    try:
        result = subprocess.run(
            ["systemctl", "restart", service],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            logger.info("Service %s restarted successfully", service)
            return True
        else:
            logger.error("Service restart failed: %s", result.stderr.strip())
            return False
    except Exception as e:
        logger.error("Service restart error: %s", e)
        return False


def _send_telegram(message: str) -> None:
    """Best-effort Telegram notification."""
    try:
        from utils.telegram import notify, IMPORTANT
        notify(message, level=IMPORTANT, category="principal")
    except Exception as e:
        logger.debug("Telegram notify failed: %s", e)


def execute_actions(directive: dict, dry_run: bool = False) -> dict:
    """Execute all actions in a Director directive.

    Expected directive schema:
    {
      "summary": str,
      "cycle_focus": str,
      "actions": [
        {
          "type": "queue_experiment" | "write_directive" | "restart_service"
                | "flag_dormant" | "send_alert",
          "target_agent": str,          # for write_directive
          "action":       str,          # for write_directive
          "experiments":  list[dict],   # for write_directive + queue_experiment
          "service":      str,          # for restart_service
          "strategy":     str,          # for flag_dormant
          "message":      str,          # for send_alert
          "reasoning":    str,
        }
      ],
      "promote": [                      # candidates ready for human review
        { "strategy": str, "reason": str, "candidate_file": str }
      ],
      "retire": [                       # strategies to remove from queue
        { "strategy": str, "reason": str }
      ],
      "observations": [str],
      "risks": [str],
      "next_cycle_focus": str,
    }
    """
    results = {
        "directives_written": 0,
        "experiments_queued": 0,
        "services_restarted": 0,
        "alerts_sent":        0,
        "errors":             [],
    }

    summary = directive.get("summary", "")
    if summary:
        logger.info("Director summary: %s", summary)

    observations = directive.get("observations", [])
    for obs in observations:
        logger.info("Observation: %s", obs)

    risks = directive.get("risks", [])
    for risk in risks:
        logger.warning("Risk flagged: %s", risk)

    # ── Execute actions ──────────────────────────────────────────────

    for action in directive.get("actions", []):
        atype = action.get("type", "")

        if atype == "queue_experiment":
            for exp in action.get("experiments", []):
                if _add_queue_entry(exp) and not dry_run:
                    results["experiments_queued"] += 1

        elif atype == "write_directive":
            agent      = action.get("target_agent", "atlas")
            act        = action.get("action", "review")
            exps       = action.get("experiments", [])
            reasoning  = action.get("reasoning", "")
            if not dry_run:
                _write_directive(agent, act, exps, reasoning)
                results["directives_written"] += 1
            else:
                logger.info("[DRY RUN] Would write directive → %s: %s", agent, act)

        elif atype == "restart_service":
            service = action.get("service", "")
            if service:
                ok = _restart_service(service, dry_run)
                if ok:
                    results["services_restarted"] += 1
                else:
                    results["errors"].append(f"restart_service failed: {service}")

        elif atype == "flag_dormant":
            strategy = action.get("strategy", "")
            reason   = action.get("reasoning", "")
            logger.warning(
                "DORMANT FLAG: %s — %s (requires code audit before queuing)", strategy, reason
            )

        elif atype == "send_alert":
            message = action.get("message", "")
            if message and not dry_run:
                _send_telegram(f"🎯 <b>Director Alert</b>\n\n{message}")
                results["alerts_sent"] += 1
            else:
                logger.info("[DRY RUN] Alert: %s", message)

        else:
            logger.warning("Unknown action type: %s", atype)

    # ── Promotion notifications ──────────────────────────────────────

    for promo in directive.get("promote", []):
        strategy = promo.get("strategy", "?")
        reason   = promo.get("reason", "")
        cfile    = promo.get("candidate_file", "")
        msg = (
            f"🏆 <b>Promotion Candidate</b>: {strategy}\n"
            f"Reason: {reason}\n"
            f"Candidate: {cfile}\n"
            f"<i>Human approval required before promoting.</i>"
        )
        logger.info("PROMOTE CANDIDATE: %s — %s", strategy, reason)
        if not dry_run:
            _send_telegram(msg)

    # ── Retirement notifications ─────────────────────────────────────

    for retire in directive.get("retire", []):
        strategy = retire.get("strategy", "?")
        reason   = retire.get("reason", "")
        logger.info("RETIRE RECOMMENDATION: %s — %s", strategy, reason)
        # Retirement is advisory only — human must act on it
        if not dry_run and retire.get("urgent"):
            _send_telegram(
                f"⚰️ <b>Director Retirement Recommendation</b>: {strategy}\n{reason}"
            )

    # ── Log summary ──────────────────────────────────────────────────

    logger.info(
        "Actions executed: directives=%d, queued=%d, restarts=%d, alerts=%d, errors=%d",
        results["directives_written"],
        results["experiments_queued"],
        results["services_restarted"],
        results["alerts_sent"],
        len(results["errors"]),
    )

    next_focus = directive.get("next_cycle_focus", "")
    if next_focus:
        logger.info("Next cycle focus: %s", next_focus)

    return results


# ── Journal ───────────────────────────────────────────────────────────────

def _append_director_log(cycle: int, directive: dict, exec_results: dict) -> None:
    """Append a director review record to logs/principal.log (structured JSON line)."""
    record = {
        "type":          "director_review",
        "cycle":         cycle,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "summary":       directive.get("summary", ""),
        "cycle_focus":   directive.get("cycle_focus", ""),
        "actions_count": len(directive.get("actions", [])),
        "promote_count": len(directive.get("promote", [])),
        "retire_count":  len(directive.get("retire", [])),
        "exec_results":  exec_results,
    }
    # Write structured JSON to a separate director audit file
    audit_path = PROJECT / "logs" / "principal-audit.jsonl"
    try:
        with open(audit_path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.debug("Audit log write failed: %s", e)


# ── Main Cycle ────────────────────────────────────────────────────────────

def run_cycle(cycle: int, dry_run: bool) -> bool:
    """Run one Director cycle. Returns True on success."""
    logger.info("=" * 60)
    logger.info("Director cycle %d starting", cycle)
    write_heartbeat("gathering_state", cycle)

    # 1. Gather state
    try:
        state = gather_state()
    except Exception as e:
        logger.error("State gathering failed: %s", e)
        return False

    write_heartbeat("calling_llm", cycle, queue_depth=state["queue"]["total"])

    # 2. Call LLM
    raw = call_llm(state)
    if raw is None:
        logger.error("LLM call returned nothing — skipping cycle")
        write_heartbeat("error", cycle, error="llm_call_failed")
        return False

    write_heartbeat("parsing_response", cycle)

    # 3. Parse response
    directive = parse_response(raw)
    if directive is None:
        logger.error("Could not parse directive from LLM output")
        write_heartbeat("error", cycle, error="parse_failed")
        return False

    logger.info("Directive parsed: %d actions, %d promotes, %d retires",
                len(directive.get("actions", [])),
                len(directive.get("promote", [])),
                len(directive.get("retire", [])))

    write_heartbeat("executing_actions", cycle)

    # 4. Execute actions
    try:
        exec_results = execute_actions(directive, dry_run=dry_run)
    except Exception as e:
        logger.error("Action execution failed: %s", e)
        write_heartbeat("error", cycle, error="execution_failed")
        return False

    # 5. Audit log
    try:
        _append_director_log(cycle, directive, exec_results)
    except Exception as e:
        logger.debug("Audit log failed: %s", e)

    write_heartbeat("idle", cycle,
                    last_summary=directive.get("summary", "")[:120],
                    last_cycle_at=datetime.now(timezone.utc).isoformat())
    logger.info("Cycle %d complete", cycle)
    return True


# ── Daemon Loop ───────────────────────────────────────────────────────────

def run_daemon(cycle_minutes: int, dry_run: bool) -> None:
    """Main 24/7 loop — runs until stop file appears or SIGTERM."""
    import signal

    running = True

    def _shutdown(signum, frame):
        nonlocal running
        logger.info("Signal %d received — stopping after current cycle", signum)
        running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    cycle = 0
    cycle_seconds = cycle_minutes * 60

    logger.info(
        "Principal daemon starting (cycle=%dmin, dry_run=%s)",
        cycle_minutes, dry_run,
    )

    while running:
        if should_stop():
            logger.info("Stop file found at %s — exiting", STOP_PATH)
            break

        cycle += 1
        run_cycle(cycle, dry_run)

        if not running or should_stop():
            break

        # Sleep in interruptible 5-second chunks
        logger.info("Sleeping %d minutes until next cycle...", cycle_minutes)
        slept = 0
        while slept < cycle_seconds and running and not should_stop():
            time.sleep(min(5, cycle_seconds - slept))
            slept += 5

    write_heartbeat("stopped", cycle)
    logger.info("Principal daemon stopped after %d cycles", cycle)


# ── CLI ───────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Atlas Principal — 24/7 Research Director Daemon",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run one cycle and exit",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Gather state and parse LLM response but do not write files or restart services",
    )
    parser.add_argument(
        "--cycle-minutes", type=int, default=DEFAULT_CYCLE_MIN,
        help=f"Minutes between Director review cycles (default: {DEFAULT_CYCLE_MIN})",
    )
    args = parser.parse_args()

    setup_logging()

    if args.once:
        logger.info("Running single cycle (--once)")
        success = run_cycle(cycle=1, dry_run=args.dry_run)
        sys.exit(0 if success else 1)
    else:
        run_daemon(cycle_minutes=args.cycle_minutes, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
