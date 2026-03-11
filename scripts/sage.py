#!/usr/bin/env python3
"""Atlas Sage — Strategy Discovery, Creation & Promotion Agent.

Sage runs every 4 hours with three phases:
  1. CREATE  — Dispatch LLM to fix broken / create new sandbox strategies
  2. SCREEN  — Sanity-check all sandbox strategies not yet in the queue
  3. PROMOTE — Auto-promote passing candidates to the active queue

Usage:
    python3 scripts/sage.py [--once] [--market sp500]

Options:
    --once          Run one cycle and exit
    --market        Restrict promotion to this market (default: sp500)
    --dry-run       Validate but do not actually promote or create
    --cycle-hours   Hours between cycles (default: 4)
    --skip-create   Skip the LLM creation phase
"""

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Project Setup ──────────────────────────────────────────────────────────

PROJECT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT)
sys.path.insert(0, str(PROJECT))

# ── Constants ──────────────────────────────────────────────────────────────

HEARTBEAT_PATH = Path("/tmp/sage-heartbeat.json")
STOP_PATH      = Path("/tmp/sage-stop")
LOG_PATH       = PROJECT / "logs" / "sage.log"
DEFAULT_CYCLE_HOURS = 4
MAX_PROMOTIONS_PER_CYCLE = 2

# Strategy creation constants
QUEUE_PATH     = PROJECT / "research" / "strategy_queue.json"
SANDBOX_DIR    = PROJECT / "research" / "strategies"
VAULT_DIR      = PROJECT / "research" / "vault" / "Strategies"
SKILL_NAME     = "atlas-strategy-discovery"
MAX_CREATES_PER_CYCLE = 2       # LLM calls are expensive — limit per cycle
SANITY_TIMEOUT = 300            # seconds for sanity_check.py
PI_TIMEOUT     = 600            # seconds for LLM strategy creation
CREATE_LOG_DIR = Path("/tmp/sage-create-logs")

# ── Logging ────────────────────────────────────────────────────────────────

logger = logging.getLogger("sage")


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [sage] %(levelname)s: %(message)s",
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
    """Atomically write a heartbeat JSON to /tmp/sage-heartbeat.json."""
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "phase": phase,
        "cycle": cycle,
        "status": "running",
        **extra,
    }
    tmp = HEARTBEAT_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(HEARTBEAT_PATH)
    except OSError as e:
        logger.debug("Heartbeat write failed: %s", e)


def write_stopped_heartbeat(cycle: int) -> None:
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "phase": "stopped",
        "cycle": cycle,
        "status": "stopped",
    }
    try:
        HEARTBEAT_PATH.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


# ── Telegram ──────────────────────────────────────────────────────────────

def send_telegram(message: str, level=None) -> None:
    """Best-effort Telegram notification."""
    try:
        from utils.telegram import notify, IMPORTANT
        if level is None:
            level = IMPORTANT
        notify(message, level=level, category="sage")
    except Exception as e:
        logger.debug("Telegram failed: %s", e)


# ── Strategy Queue Helpers ─────────────────────────────────────────────────

def read_queue() -> dict:
    """Read strategy_queue.json with safe fallback."""
    try:
        return json.loads(QUEUE_PATH.read_text())
    except Exception:
        return {"active": [], "candidates": [], "rejected": []}


def write_queue(data: dict) -> None:
    """Atomically write strategy_queue.json."""
    tmp = QUEUE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(QUEUE_PATH)


def queue_known_names() -> set[str]:
    """All strategy names across active/candidates/rejected."""
    q = read_queue()
    names = set()
    for bucket in ("active", "candidates", "rejected"):
        for entry in q.get(bucket, []):
            if isinstance(entry, dict):
                names.add(entry.get("name", ""))
            elif isinstance(entry, str):
                names.add(entry)
    return names


# ── Strategy Creation via LLM ─────────────────────────────────────────────

def _vault_cards() -> dict[str, Path]:
    """Map snake_case name → vault .md path for all vault cards."""
    cards = {}
    if not VAULT_DIR.exists():
        return cards
    for f in sorted(VAULT_DIR.glob("*.md")):
        key = f.stem.lower().replace(" ", "_").replace("-", "_")
        cards[key] = f
    return cards


def _sandbox_strategies() -> dict[str, Path]:
    """Map snake_case name → .py path for all sandbox strategies."""
    strats = {}
    if not SANDBOX_DIR.exists():
        return strats
    for f in sorted(SANDBOX_DIR.glob("*.py")):
        if f.stem == "__init__":
            continue
        strats[f.stem] = f
    return strats


def _run_sanity_check(strategy_name: str, market: str = "sp500") -> dict:
    """Run scripts/sanity_check.py and return normalized result.

    Returns dict with keys: status ("pass"|"fail"|"error"), sharpe, trades, error.
    """
    cmd = [
        sys.executable, str(PROJECT / "scripts" / "sanity_check.py"),
        "--strategy", strategy_name, "--market", market,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=SANITY_TIMEOUT, cwd=str(PROJECT),
        )
        # Parse JSON from stdout — look for the JSON block (may have log lines)
        raw = None
        # Try full stdout first (sanity_check.py outputs a single JSON blob)
        stdout = result.stdout.strip()
        if stdout.startswith("{"):
            try:
                raw = json.loads(stdout)
            except json.JSONDecodeError:
                pass
        # Fallback: find last JSON-looking line
        if raw is None:
            for line in reversed(stdout.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        raw = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue

        if raw is None:
            return {"status": "error", "error": f"No JSON in output: {stdout[:200]}"}

        # Normalize: sanity_check.py uses "verdict", we use "status"
        metrics = raw.get("metrics", {})
        return {
            "status": raw.get("verdict", raw.get("status", "error")),
            "sharpe": metrics.get("sharpe", raw.get("sharpe", 0)),
            "trades": metrics.get("total_trades", raw.get("trades", 0)),
            "max_dd_pct": metrics.get("max_drawdown_pct", 0),
            "error": raw.get("reason") if raw.get("verdict") != "pass" else None,
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "timeout", "sharpe": 0, "trades": 0}
    except Exception as e:
        return {"status": "error", "error": str(e), "sharpe": 0, "trades": 0}


SANITY_CACHE_PATH = Path("/tmp/sage-sanity-cache.json")
_current_cycle = 0  # updated by run_cycle for heartbeat access


def _load_sanity_cache() -> dict:
    """Load cached sanity check results. Keyed by strategy name."""
    try:
        return json.loads(SANITY_CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_sanity_cache(cache: dict) -> None:
    try:
        SANITY_CACHE_PATH.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass


def _find_creation_targets(max_checks: int = 4) -> list[dict]:
    """Find strategies that need LLM creation or fixing.

    To avoid running expensive backtests on ALL sandbox strategies every cycle,
    we cache sanity results and only check `max_checks` unknowns per cycle.

    Returns list of targets sorted by priority:
      1. Sandbox strategies that FAIL sanity check (need fixing)
      2. Vault cards with NO sandbox implementation (need creation)
    """
    known = queue_known_names()
    sandbox = _sandbox_strategies()
    vault = _vault_cards()
    cache = _load_sanity_cache()

    # Production strategies — never touch these
    prod_dir = PROJECT / "strategies"
    prod_names = {f.stem for f in prod_dir.glob("*.py") if f.stem not in ("__init__", "base")}
    prod_aliases = {
        "bollinger_band_squeeze": "bb_squeeze",
        "connorsrsi2": "connors_rsi2",
        "combined_portfolio": None,
        "portfolio_filter": None,
        "sma_200_filter": None,
    }

    targets = []
    checks_run = 0

    # Priority 1: Sandbox strategies NOT in the queue
    for name, path in sandbox.items():
        if name in known or name in prod_names:
            continue

        # Use cached result if available
        cached = cache.get(name)
        if cached:
            status = cached.get("status")
            if status == "pass":
                # Already passing — ensure it's in candidates
                _add_to_candidates(name, cached)
                continue
            elif status in ("fail", "error"):
                targets.append({
                    "name": name, "action": "fix",
                    "vault_card": vault.get(name),
                    "sandbox": path,
                    "sanity_result": cached,
                })
                continue

        # No cache — run sanity check (limited per cycle)
        if checks_run >= max_checks:
            continue
        checks_run += 1
        logger.info("Sanity checking: %s (%d/%d)", name, checks_run, max_checks)
        write_heartbeat(f"checking {name}", _current_cycle,
                        strategy=name, checks=f"{checks_run}/{max_checks}")

        check = _run_sanity_check(name)
        cache[name] = check  # cache result
        _save_sanity_cache(cache)

        if check.get("status") == "pass":
            logger.info("SANITY PASS: %s (sharpe=%.2f, trades=%d) → candidates",
                        name, check.get("sharpe", 0), check.get("trades", 0))
            _add_to_candidates(name, check)
        elif check.get("status") in ("fail", "error"):
            targets.append({
                "name": name, "action": "fix",
                "vault_card": vault.get(name),
                "sandbox": path,
                "sanity_result": check,
            })

    # Priority 2: Vault cards with NO sandbox implementation
    for key, card_path in vault.items():
        if key in sandbox or key in prod_names or key in known:
            continue
        aliased = prod_aliases.get(key, key)
        if aliased is None:
            continue
        if aliased in prod_names or aliased in sandbox:
            continue
        targets.append({
            "name": key, "action": "create",
            "vault_card": card_path,
            "sandbox": None,
            "sanity_result": None,
        })

    # Sort: fixes before creates (more likely to succeed)
    targets.sort(key=lambda t: 0 if t["action"] == "fix" else 1)
    return targets


def _add_to_candidates(name: str, sanity_result: dict) -> None:
    """Add a passing strategy to candidates in the queue file."""
    q = read_queue()
    # Don't double-add
    if any(e.get("name") == name for e in q.get("candidates", [])):
        return
    q.setdefault("candidates", []).append({
        "name": name,
        "added_by": "sage",
        "since": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "sharpe": sanity_result.get("sharpe"),
        "trades": sanity_result.get("trades"),
    })
    write_queue(q)


def _build_pi_prompt(target: dict) -> str:
    """Build the prompt for the LLM strategy creation job."""
    name = target["name"]

    if target["action"] == "fix":
        current_code = target["sandbox"].read_text()
        error_info = json.dumps(target["sanity_result"], indent=2)
        vault_context = ""
        if target["vault_card"]:
            vault_context = f"\n\nVault card for reference:\n```markdown\n{target['vault_card'].read_text()}\n```"

        return f"""Fix the broken sandbox strategy '{name}' so it passes sanity check.

Current code in research/strategies/{name}.py:
```python
{current_code}
```

Sanity check result (FAILED):
```json
{error_info}
```
{vault_context}

Requirements:
- Edit ONLY the file research/strategies/{name}.py
- Must pass: python3 scripts/sanity_check.py --strategy {name}
- Pass criteria: trades >= 30 AND sharpe > -0.5
- Keep the same class name and BaseStrategy interface
- Use calc_atr, calc_rsi from utils.helpers
- Ensure PARAM_GRID dict exists at module level for sweep compatibility
- Test with: python3 scripts/sanity_check.py --strategy {name}
"""
    else:  # create
        vault_text = ""
        if target["vault_card"]:
            vault_text = target["vault_card"].read_text()

        return f"""Create a new sandbox strategy '{name}' from this vault card.

Vault card:
```markdown
{vault_text}
```

Requirements:
- Create the file research/strategies/{name}.py
- Must extend BaseStrategy from strategies.base
- Must implement generate_signals() and check_exits()
- Must include PARAM_GRID dict at module level
- Must pass: python3 scripts/sanity_check.py --strategy {name}
- Pass criteria: trades >= 30 AND sharpe > -0.5
- Use calc_atr, calc_rsi, calc_position_size from utils.helpers
- Follow the pattern from existing working strategies (e.g., stochastic_oversold, consecutive_down_days)
- The strategy.name property must return '{name}'
"""


def _dispatch_llm_create(target: dict) -> dict:
    """Dispatch pi --print to create/fix a strategy. Returns result dict."""
    name = target["name"]
    action = target["action"]
    logger.info("LLM %s: %s", action.upper(), name)

    CREATE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = CREATE_LOG_DIR / f"{name}_{action}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    prompt = _build_pi_prompt(target)

    cmd = [
        "pi", "--print", "--no-session",
        "--model", "claude-sonnet-4-6",
        "--skill", SKILL_NAME,
        prompt,
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=PI_TIMEOUT, cwd=str(PROJECT),
            env={**os.environ, "PI_NON_INTERACTIVE": "1"},
        )
        # Log output
        log_file.write_text(
            f"=== CMD ===\n{' '.join(cmd[:6])} ...\n\n"
            f"=== STDOUT ===\n{result.stdout[-3000:]}\n\n"
            f"=== STDERR ===\n{result.stderr[-1000:]}\n\n"
            f"=== EXIT CODE: {result.returncode} ===\n"
        )

        if result.returncode != 0:
            return {"status": "error", "error": f"pi exit code {result.returncode}",
                    "log": str(log_file)}

        # Verify the file was actually created/modified
        target_file = SANDBOX_DIR / f"{name}.py"
        if not target_file.exists():
            return {"status": "error", "error": "Strategy file not created",
                    "log": str(log_file)}

        # Run sanity check on the result
        check = _run_sanity_check(name)
        check["log"] = str(log_file)
        return check

    except subprocess.TimeoutExpired:
        logger.warning("LLM timeout for %s after %ds", name, PI_TIMEOUT)
        return {"status": "error", "error": f"timeout ({PI_TIMEOUT}s)",
                "log": str(log_file)}
    except Exception as e:
        logger.error("LLM dispatch error for %s: %s", name, e)
        return {"status": "error", "error": str(e)}


def run_create_phase(cycle: int, dry_run: bool) -> dict:
    """Phase 1: Find and create/fix strategies via LLM.

    Returns summary: {"targets_found", "created", "fixed", "failed"}
    """
    logger.info("── Create phase ──")
    write_heartbeat("create_scan", cycle)

    targets = _find_creation_targets()
    logger.info("Creation targets: %d (%d fix, %d create)",
                len(targets),
                sum(1 for t in targets if t["action"] == "fix"),
                sum(1 for t in targets if t["action"] == "create"))

    if not targets:
        return {"targets_found": 0, "created": 0, "fixed": 0, "failed": 0}

    created = fixed = failed = 0
    for target in targets[:MAX_CREATES_PER_CYCLE]:
        if should_stop():
            break

        name = target["name"]
        action = target["action"]
        write_heartbeat(f"creating_{name}", cycle,
                        strategy=name, action=action)

        if dry_run:
            logger.info("[DRY-RUN] Would %s: %s", action, name)
            continue

        result = _dispatch_llm_create(target)

        if result.get("status") == "pass":
            logger.info("✓ LLM %s SUCCESS: %s (sharpe=%.2f, trades=%d)",
                        action, name,
                        result.get("sharpe", 0), result.get("trades", 0))
            _add_to_candidates(name, result)
            if action == "fix":
                fixed += 1
            else:
                created += 1
            send_telegram(
                f"🧪 <b>Sage {action}ed</b> <code>{name}</code>\n"
                f"Sharpe: {result.get('sharpe', '?'):.2f}, "
                f"Trades: {result.get('trades', '?')}",
            )
        else:
            failed += 1
            err = result.get("error", "unknown")
            logger.warning("✗ LLM %s FAILED: %s — %s", action, name, err)
            # Add to rejected so we don't retry every cycle
            q = read_queue()
            q.setdefault("rejected", []).append({
                "name": name,
                "rejected_by": "sage",
                "since": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "reason": f"llm_{action}_failed: {err}",
            })
            write_queue(q)

    summary = {
        "targets_found": len(targets),
        "created": created, "fixed": fixed, "failed": failed,
    }
    logger.info("Create phase done: %s", summary)
    return summary


# ── Strategy Queue Promotion ──────────────────────────────────────────────

def run_promote_phase(cycle: int, dry_run: bool) -> dict:
    """Phase 2: Auto-promote top candidates from the queue to active.

    Criteria: sharpe > 0.0 and trades >= 30 (stricter than sanity check).
    Max 2 promotions per cycle.
    """
    logger.info("── Promote phase (queue) ──")
    q = read_queue()
    candidates = q.get("candidates", [])

    if not candidates:
        logger.info("No candidates to promote")
        return {"promoted": 0, "candidates": 0}

    # Sort by sharpe descending
    candidates.sort(key=lambda c: c.get("sharpe", -999), reverse=True)

    promoted = 0
    active_names = {e["name"] for e in q.get("active", []) if isinstance(e, dict)}

    for cand in candidates[:]:
        if promoted >= MAX_PROMOTIONS_PER_CYCLE:
            break
        name = cand.get("name", "")
        sharpe = cand.get("sharpe", -999)
        trades = cand.get("trades", 0)

        if name in active_names:
            # Already active, remove from candidates
            candidates.remove(cand)
            continue

        # Re-run sanity check for freshness
        check = _run_sanity_check(name)
        if check.get("status") != "pass":
            logger.info("SKIP promote %s: sanity re-check failed (%s)",
                        name, check.get("error", check.get("status")))
            continue

        fresh_sharpe = check.get("sharpe", -999)
        fresh_trades = check.get("trades", 0)

        # Promotion gate: stricter than sanity pass
        if fresh_sharpe < 0.0 or fresh_trades < 30:
            logger.info("SKIP promote %s: sharpe=%.2f trades=%d (below promo threshold)",
                        name, fresh_sharpe, fresh_trades)
            continue

        if dry_run:
            logger.info("[DRY-RUN] Would promote %s (sharpe=%.2f)", name, fresh_sharpe)
            promoted += 1
            continue

        # Promote: move from candidates → active
        candidates.remove(cand)
        q["active"].append({
            "name": name,
            "added_by": "sage",
            "since": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "sharpe": fresh_sharpe,
            "trades": fresh_trades,
        })
        promoted += 1
        active_names.add(name)
        logger.info("PROMOTED to active: %s (sharpe=%.2f, trades=%d)",
                     name, fresh_sharpe, fresh_trades)
        send_telegram(
            f"🚀 <b>Sage promoted to active</b>: <code>{name}</code>\n"
            f"Sharpe: {fresh_sharpe:.2f}, Trades: {fresh_trades}\n"
            f"Atlas & Nova will pick it up next cycle."
        )

    q["candidates"] = candidates
    write_queue(q)

    return {"promoted": promoted, "candidates": len(candidates)}


# ── Candidate Scanning ────────────────────────────────────────────────────

def scan_candidates(market: str | None = None) -> list[dict]:
    """Scan for promotion candidates from two sources:

    1. config/candidates/*.json — staged candidate configs from research_promote
    2. research/queue.json with status='passed' — experiments the researcher flagged

    Returns list of dicts: {experiment_id, market, source, candidate_path}
    """
    from research.models import read_queue, ExperimentStatus, CANDIDATES_DIR

    found: list[dict] = []

    # ── Source 1: Staged candidate configs ──────────────────────
    candidates_dir = PROJECT / "config" / "candidates"
    if candidates_dir.exists():
        for path in sorted(candidates_dir.glob("*.json")):
            if path.name.startswith("."):
                continue
            try:
                with open(path) as f:
                    cfg = json.load(f)
                meta = cfg.get("_promotion_metadata", {})
                exp_id = meta.get("experiment_id", path.stem)
                market_id = meta.get("market", path.stem.split("_")[0])
                if market and market_id != market:
                    continue
                # Skip if already promoted (has a version)
                if meta.get("promoted_at"):
                    continue
                found.append({
                    "experiment_id": exp_id,
                    "market": market_id,
                    "source": "candidates",
                    "candidate_path": str(path),
                })
            except (json.JSONDecodeError, OSError) as e:
                logger.debug("Skipping %s: %s", path.name, e)

    # ── Source 2: Research queue with status='passed' ────────────
    try:
        queue = read_queue()
        for entry in queue:
            if entry.get("status") not in (ExperimentStatus.PASSED, "passed"):
                continue
            exp_id = entry.get("id", "")
            market_id = entry.get("market", "sp500")
            if market and market_id != market:
                continue
            # Check if already in candidates list (avoid duplicates)
            if any(c["experiment_id"] == exp_id for c in found):
                continue
            # Check if a candidate file exists for this experiment
            candidate_path = PROJECT / "config" / "candidates" / f"{market_id}_{exp_id}.json"
            found.append({
                "experiment_id": exp_id,
                "market": market_id,
                "source": "queue",
                "candidate_path": str(candidate_path) if candidate_path.exists() else None,
                "queue_entry": entry,
            })
    except Exception as e:
        logger.warning("Queue scan failed: %s", e)

    return found


# ── Sanity Check ──────────────────────────────────────────────────────────

def sanity_check(candidate: dict) -> tuple[bool, str]:
    """Quick sanity checks before attempting full promotion validation.

    These are cheap pre-flight checks to avoid wasting time on obviously
    broken or already-promoted candidates.

    Returns (ok: bool, reason: str)
    """
    exp_id = candidate["experiment_id"]
    market_id = candidate["market"]
    candidate_path = candidate.get("candidate_path")

    # 1. Candidate config must exist for 'candidates' source
    if candidate["source"] == "candidates":
        if not candidate_path or not Path(candidate_path).exists():
            return False, f"Candidate file not found: {candidate_path}"

    # 2. Experiment must not already be promoted
    try:
        from research.models import load_experiment
        exp = load_experiment(exp_id)
        if exp and exp.get("promoted"):
            return False, f"Already promoted"
    except Exception:
        pass

    # 3. Check rate limit (at most 1 promotion per week per market)
    try:
        from research.models import get_recent_promotions
        recent = get_recent_promotions(market_id, days=7)
        if len(recent) >= 1:
            return False, f"Rate limited: {len(recent)} promotion(s) in past 7 days"
    except Exception:
        pass

    # 4. If no candidate config, try staging it from the queue entry
    if not candidate_path or not Path(candidate_path).exists():
        qe = candidate.get("queue_entry", {})
        strategy_params = qe.get("params_override")
        enable_strategy = qe.get("strategy_name")
        if not strategy_params and not enable_strategy:
            return False, "No candidate config and no params to stage"
        try:
            from scripts.research_promote import stage_candidate
            staged = stage_candidate(exp_id, market_id,
                                     strategy_params=strategy_params,
                                     enable_strategy=enable_strategy)
            candidate["candidate_path"] = str(staged)
            logger.info("Staged candidate for %s: %s", exp_id, staged)
        except Exception as e:
            return False, f"Staging failed: {e}"

    return True, "ok"


# ── Validation ────────────────────────────────────────────────────────────

def validate_candidate(candidate: dict, dry_run: bool = False) -> tuple[bool, dict]:
    """Run full OOS + regression validation on a candidate.

    Returns (passed: bool, validation_result: dict)
    """
    exp_id = candidate["experiment_id"]
    market_id = candidate["market"]
    candidate_path_str = candidate.get("candidate_path")
    if not candidate_path_str:
        return False, {"error": "No candidate_path"}

    candidate_path = Path(candidate_path_str)
    if not candidate_path.exists():
        return False, {"error": f"File not found: {candidate_path}"}

    if dry_run:
        logger.info("[DRY-RUN] Would validate %s for %s", exp_id, market_id)
        return True, {"dry_run": True, "overall_pass": True}

    try:
        from scripts.research_promote import validate_candidate as _validate
        result = _validate(exp_id, market_id,
                           candidate_path=candidate_path,
                           skip_oos=False)
        passed = result.get("overall_pass", False)
        return passed, result
    except Exception as e:
        logger.error("Validation error for %s: %s", exp_id, e, exc_info=True)
        return False, {"error": str(e)}


# ── Promotion ─────────────────────────────────────────────────────────────

def promote(candidate: dict, validation_result: dict, dry_run: bool = False) -> bool:
    """Promote a validated candidate to active config.

    Returns True on success.
    """
    exp_id = candidate["experiment_id"]
    market_id = candidate["market"]
    candidate_path_str = candidate.get("candidate_path")

    if dry_run:
        logger.info("[DRY-RUN] Would promote %s for %s", exp_id, market_id)
        # Send dry-run notification
        send_telegram(
            f"🔬 [Sage DRY-RUN] Would promote <code>{exp_id}</code> "
            f"for {market_id.upper()} — validation PASSED"
        )
        return True

    try:
        from scripts.research_promote import (
            promote_candidate, send_promotion_request
        )

        # Send Telegram promotion request (with approve/reject buttons)
        send_promotion_request(exp_id, market_id, validation_result)

        # Auto-promote (Sage is autonomous — no human gate in this mode)
        result = promote_candidate(exp_id, market_id,
                                   candidate_path=Path(candidate_path_str))
        if result.get("success"):
            version = result.get("version_path", "?")
            logger.info("PROMOTED %s → %s", exp_id, version)
            send_telegram(
                f"✅ <b>Sage promoted</b> <code>{exp_id}</code> "
                f"for {market_id.upper()}\n"
                f"Config: <code>{Path(version).name}</code>"
            )
            return True
        else:
            err = result.get("error", "unknown error")
            logger.warning("Promotion failed for %s: %s", exp_id, err)
            send_telegram(
                f"⚠️ <b>Sage promote FAILED</b> <code>{exp_id}</code> "
                f"({market_id.upper()}): {err}"
            )
            return False
    except Exception as e:
        logger.error("Promote error for %s: %s", exp_id, e, exc_info=True)
        send_telegram(
            f"❌ <b>Sage promote ERROR</b> <code>{exp_id}</code>: {e}"
        )
        return False


# ── Cycle ─────────────────────────────────────────────────────────────────

def run_cycle(cycle: int, market: str | None, dry_run: bool,
              skip_create: bool = False) -> dict:
    """Execute one full Sage cycle:
      Phase 1: CREATE  — LLM creates/fixes sandbox strategies
      Phase 2: SCREEN  — sanity-check sandbox → add to candidates
      Phase 3: PROMOTE — auto-promote from candidates → active queue
      Phase 4: LEGACY  — check config/candidates for config promotions

    Returns summary dict.
    """
    global _current_cycle
    _current_cycle = cycle
    logger.info("══ Sage cycle %d started (market=%s, dry_run=%s) ══",
                cycle, market or "all", dry_run)

    # ── Phase 1: CREATE (LLM strategy creation/fixing) ───────────
    create_summary = {"targets_found": 0, "created": 0, "fixed": 0, "failed": 0}
    if not skip_create:
        try:
            create_summary = run_create_phase(cycle, dry_run)
        except Exception as e:
            logger.error("Create phase error: %s", e, exc_info=True)
    else:
        logger.info("Skipping create phase (--skip-create)")

    if should_stop():
        return {"create": create_summary}

    # ── Phase 2: SCREEN (sanity check remaining unknowns) ────────
    # _find_creation_targets already adds passing strategies to candidates.
    # This phase is implicit — covered by _find_creation_targets scanning.

    # ── Phase 3: PROMOTE (candidates → active in strategy queue) ──
    promote_summary = {"promoted": 0, "candidates": 0}
    try:
        promote_summary = run_promote_phase(cycle, dry_run)
    except Exception as e:
        logger.error("Promote phase error: %s", e, exc_info=True)

    if should_stop():
        return {"create": create_summary, "queue_promote": promote_summary}

    # ── Phase 4: LEGACY (config candidate promotions) ─────────────
    write_heartbeat("legacy_scan", cycle)
    candidates = scan_candidates(market)
    logger.info("Legacy config candidates: %d", len(candidates))

    legacy_promoted = 0
    if candidates:
        write_heartbeat("sanity_check", cycle, candidates_found=len(candidates))
        sane = []
        for c in candidates:
            ok, reason = sanity_check(c)
            if ok:
                sane.append(c)
                logger.info("SANE: %s (%s)", c["experiment_id"], c["market"])
            else:
                logger.info("SKIP: %s — %s", c["experiment_id"], reason)

        for c in sane:
            if legacy_promoted >= MAX_PROMOTIONS_PER_CYCLE or should_stop():
                break
            exp_id = c["experiment_id"]
            write_heartbeat("validate", cycle, current_experiment=exp_id)
            passed, result = validate_candidate(c, dry_run=dry_run)
            if passed:
                ok = promote(c, result, dry_run=dry_run)
                if ok:
                    legacy_promoted += 1

    summary = {
        "create": create_summary,
        "queue_promote": promote_summary,
        "legacy_candidates": len(candidates),
        "legacy_promoted": legacy_promoted,
    }
    logger.info("Cycle %d done: %s", cycle, summary)
    return summary


# ── Main ──────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Atlas Sage — strategy discovery & promotion agent")
    p.add_argument("--once",         action="store_true",
                   help="Run one cycle then exit")
    p.add_argument("--market",       type=str, default=None,
                   help="Restrict to this market (e.g. sp500, asx)")
    p.add_argument("--dry-run",      action="store_true",
                   help="Validate but do not actually create/promote")
    p.add_argument("--cycle-hours",  type=float, default=DEFAULT_CYCLE_HOURS,
                   help=f"Hours between cycles (default: {DEFAULT_CYCLE_HOURS})")
    p.add_argument("--skip-create",  action="store_true",
                   help="Skip the LLM strategy creation phase")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging()

    logger.info("Sage starting (pid=%d, once=%s, market=%s, dry_run=%s, cycle_hours=%g)",
                os.getpid(), args.once, args.market or "all",
                args.dry_run, args.cycle_hours)

    # Remove stale stop file if present from a previous run
    if STOP_PATH.exists():
        STOP_PATH.unlink(missing_ok=True)
        logger.info("Removed stale stop file")

    cycle = 0
    cycle_sleep_s = int(args.cycle_hours * 3600)

    send_telegram(
        f"🌿 <b>Sage started</b> (pid={os.getpid()}, "
        f"cycle={args.cycle_hours}h, dry_run={args.dry_run})"
    )

    try:
        while True:
            cycle += 1
            if should_stop():
                logger.info("Stop file detected before cycle %d — exiting", cycle)
                break

            write_heartbeat("cycle_start", cycle)
            try:
                summary = run_cycle(cycle, market=args.market, dry_run=args.dry_run,
                                    skip_create=args.skip_create)
            except Exception as e:
                logger.error("Cycle %d failed: %s", cycle, e, exc_info=True)
                send_telegram(f"❌ <b>Sage cycle {cycle} ERROR</b>: {e}")
                summary = {"error": str(e)}

            write_heartbeat("sleep", cycle,
                            last_summary=summary,
                            next_cycle_in_s=cycle_sleep_s)

            if args.once:
                logger.info("--once: exiting after cycle %d", cycle)
                break
            if should_stop():
                logger.info("Stop file detected after cycle %d — exiting", cycle)
                break

            logger.info("Sleeping %g hours until next cycle…", args.cycle_hours)
            # Sleep in 60s increments so stop file is checked regularly
            slept = 0
            while slept < cycle_sleep_s:
                if should_stop():
                    logger.info("Stop file detected during sleep — exiting")
                    break
                time.sleep(min(60, cycle_sleep_s - slept))
                slept += 60
            else:
                continue  # Inner while finished normally → continue outer loop
            break          # Stop file hit during sleep

    finally:
        write_stopped_heartbeat(cycle)
        logger.info("Sage stopped (cycle=%d)", cycle)
        send_telegram(f"🛑 <b>Sage stopped</b> (last cycle={cycle})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
