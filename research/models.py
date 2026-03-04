"""
Atlas Research Data Models
==============================
Dataclasses and helpers for the research experiment system.
All inter-role communication uses JSON files — these models define the schemas.

File Ownership Boundaries:
    Role        | Reads                          | Writes
    ------------|--------------------------------|----------------------------
    Researcher  | journal.json, perf data        | queue.json (append)
    Backtester  | queue.json                     | experiments/*.json, queue.json (status)
    Analyst     | experiments/*.json             | journal.json (append), experiments/*.json (annotate)
    Risk        | experiments/*.json, journal    | config/candidates/*.json
"""

import json
import fcntl
import os
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
from enum import Enum

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESEARCH_DIR = PROJECT_ROOT / "research"
QUEUE_PATH = RESEARCH_DIR / "queue.json"
JOURNAL_PATH = RESEARCH_DIR / "journal.json"
EXPERIMENTS_DIR = RESEARCH_DIR / "experiments"
STRATEGIES_DIR = RESEARCH_DIR / "strategies"
CANDIDATES_DIR = PROJECT_ROOT / "config" / "candidates"


class ExperimentStatus(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    EVALUATING = "evaluating"
    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    DEFERRED = "deferred"
    SKIPPED = "skipped"


class ExperimentType(str, Enum):
    SINGLE_STRATEGY_TEST = "single_strategy_test"
    COMBINED_PORTFOLIO_TEST = "combined_portfolio_test"
    PARAM_SWEEP = "param_sweep"
    FULL_OPTIMIZATION = "full_optimization"
    OOS_VALIDATION = "oos_validation"
    FILTER_TEST = "filter_test"
    REOPTIMIZATION = "reoptimization"


class Priority(str, Enum):
    P1_CRITICAL = "P1"  # Degradation fixes, broken strategies
    P2_HIGH = "P2"      # Dormant strategy activation, known improvements
    P3_MEDIUM = "P3"    # Param drift correction, new filters
    P4_LOW = "P4"       # New strategies, exploratory research
    P5_BACKLOG = "P5"   # Cross-market, long-term ideas


def _get_strategy_registry() -> Dict[str, Any]:
    """Lazy-load the strategy registry to check strategy_name validity."""
    try:
        from scripts.strategy_evaluator import STRATEGY_REGISTRY, load_sandbox_strategy
        return STRATEGY_REGISTRY, load_sandbox_strategy
    except ImportError:
        return {}, None


def validate_queue_entry(entry: "QueueEntry") -> List[str]:
    """Validate a QueueEntry against what research_runner can actually execute.

    Returns a list of error strings. Empty list = valid.

    This prevents aspirational experiments from being queued with params_override
    formats that don't match what the runner expects, saving wasted compute.
    """
    errors = []
    method = entry.method if isinstance(entry.method, str) else entry.method.value
    params = entry.params_override or {}

    # ── Strategy existence check ──────────────────────────────────────────
    if entry.strategy_name:
        registry, sandbox_loader = _get_strategy_registry()
        if registry and entry.strategy_name not in registry:
            # Check sandbox
            if sandbox_loader and sandbox_loader(entry.strategy_name) is None:
                errors.append(
                    f"strategy_name='{entry.strategy_name}' not found in "
                    f"STRATEGY_REGISTRY ({list(registry.keys())}) or sandbox"
                )

    # ── Method-specific params_override validation ────────────────────────
    if method == ExperimentType.PARAM_SWEEP:
        # Runner expects: params_override.sweep_param (str) + params_override.sweep_values (list)
        if "sweep_param" not in params:
            hint = ""
            if "sweep_params" in params:
                hint = " (found 'sweep_params' — use singular 'sweep_param' + 'sweep_values')"
            errors.append(f"param_sweep requires params_override.sweep_param (str){hint}")
        elif not isinstance(params["sweep_param"], str):
            errors.append(f"params_override.sweep_param must be a str, got {type(params['sweep_param']).__name__}")
        if "sweep_values" not in params:
            errors.append("param_sweep requires params_override.sweep_values (list)")
        elif not isinstance(params["sweep_values"], list) or len(params["sweep_values"]) < 2:
            errors.append("params_override.sweep_values must be a list with >= 2 values")

        if not entry.strategy_name:
            errors.append("param_sweep requires strategy_name to be set")

    elif method == ExperimentType.FILTER_TEST:
        # Runner expects: params_override.filter_param (str)
        #   + either params_override.variants (list of {name, value})
        #   or params_override.filter_on + params_override.filter_off
        if "filter_param" not in params:
            hint = ""
            if "filter_type" in params:
                hint = " (found 'filter_type' — use 'filter_param' instead)"
            errors.append(f"filter_test requires params_override.filter_param (str){hint}")
        if "variants" in params:
            if not isinstance(params["variants"], list) or len(params["variants"]) < 2:
                errors.append("params_override.variants must be a list with >= 2 entries")
            else:
                for i, v in enumerate(params["variants"]):
                    if not isinstance(v, dict) or "value" not in v:
                        errors.append(f"variants[{i}] must be a dict with at least 'value' key")
        elif "filter_on" not in params and "filter_off" not in params:
            errors.append("filter_test requires either 'variants' list or 'filter_on'+'filter_off'")

    elif method == ExperimentType.FULL_OPTIMIZATION:
        # If strategy_name is set (dormant/new_strategy), runner uses coordinate descent
        # which requires params_override.param_grid (dict of str→list)
        if entry.strategy_name and entry.category in ("dormant", "new_strategy"):
            pg = params.get("param_grid")
            if pg is None:
                # Also accept nested: params_override.optimize_params → should be param_grid
                if "optimize_params" in params:
                    errors.append(
                        "full_optimization uses 'param_grid' not 'optimize_params' — "
                        "rename params_override.optimize_params to params_override.param_grid"
                    )
                else:
                    errors.append(
                        "full_optimization with strategy_name requires "
                        "params_override.param_grid (dict of param→[values])"
                    )
            elif not isinstance(pg, dict):
                errors.append("params_override.param_grid must be a dict")
            else:
                for k, v in pg.items():
                    if not isinstance(v, list) or len(v) < 2:
                        errors.append(f"param_grid['{k}'] must be a list with >= 2 values")

    elif method == ExperimentType.SINGLE_STRATEGY_TEST:
        if not entry.strategy_name:
            errors.append("single_strategy_test requires strategy_name to be set")

    elif method == ExperimentType.COMBINED_PORTFOLIO_TEST:
        if not entry.strategy_name:
            errors.append("combined_portfolio_test requires strategy_name to be set")

    return errors


@dataclass
class QueueEntry:
    """A single experiment in the research queue."""
    id: str
    title: str
    category: str  # degradation|dormant|param_drift|filter|new_strategy|portfolio|cross_market
    market: str
    hypothesis: str
    method: ExperimentType
    acceptance_criteria: Dict[str, Any]
    estimated_runtime_min: int
    priority: str  # P1-P5
    status: str = ExperimentStatus.QUEUED
    strategy_name: Optional[str] = None
    params_override: Optional[Dict[str, Any]] = None
    config_snapshot: Optional[Dict[str, Any]] = None
    claimed_by: Optional[str] = None
    claimed_at: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    tags: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "QueueEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ExperimentEnvelope:
    """Self-contained experiment result — any agent can read without prior context."""
    id: str
    queue_entry: Dict[str, Any]  # Snapshot of QueueEntry at start
    config_snapshot: Dict[str, Any]  # Full config used for this experiment
    inputs: Dict[str, Any]  # Strategy params, market, data range, etc.
    outputs: Optional[Dict[str, Any]] = None  # Metrics, trade list, equity curve
    metadata: Dict[str, Any] = field(default_factory=dict)  # Runtime, agent_id, timestamps
    verdict: Optional[str] = None  # pass/fail/partial/interesting
    verdict_rationale: Optional[str] = None
    learnings: List[str] = field(default_factory=list)
    promoted: bool = False
    candidate_config_path: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ExperimentEnvelope":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def save(self):
        """Save this envelope to research/experiments/exp-{id}.json (atomic)."""
        EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
        path = EXPERIMENTS_DIR / f"exp-{self.id}.json"
        atomic_json_write(path, self.to_dict())
        return path

    @classmethod
    def load(cls, exp_id: str) -> "ExperimentEnvelope":
        path = EXPERIMENTS_DIR / f"exp-{exp_id}.json"
        with open(path) as f:
            return cls.from_dict(json.load(f))


@dataclass
class JournalEntry:
    """Append-only entry in the research journal."""
    experiment_id: str
    timestamp: str
    market: str
    category: str
    strategy: Optional[str]
    hypothesis: str
    verdict: str  # pass/fail/partial/deferred
    key_metrics: Dict[str, Any]  # CAGR, Sharpe, DD, trade_count, etc.
    delta_vs_baseline: Dict[str, Any]  # How this changes the portfolio
    learnings: List[str]
    promoted: bool = False
    runtime_s: float = 0
    agent_id: str = "atlas-research"

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Atomic write utility
# ---------------------------------------------------------------------------

def atomic_json_write(path: Path, data) -> None:
    """Write *data* as JSON to *path* atomically.

    Writes to a ``.tmp`` sibling, fsync's, then ``os.replace``'s over the
    target.  The original file is never truncated until the replacement is
    fully written and flushed — so a mid-write crash/kill leaves the previous
    version intact.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


# ---------------------------------------------------------------------------
# File-locked read/write helpers
# ---------------------------------------------------------------------------

def _lock_path(path: Path) -> Path:
    """Return the sentinel ``.lock`` file used to coordinate access to *path*.

    A dedicated lock file (rather than locking the data file itself) survives
    atomic renames — so all readers/writers always coordinate on the same
    inode even after the data file is replaced.
    """
    return path.with_name(path.name + ".lock")


def _locked_read(path: Path) -> list:
    """Read a JSON list file with shared lock."""
    if not path.exists():
        return []
    lock = _lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_SH)
        try:
            if not path.exists():
                return []
            try:
                with open(path) as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return []
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def _locked_write(path: Path, data: list):
    """Write a JSON list file with exclusive lock (atomic temp-then-rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(path)
    with open(lock, "a") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            atomic_json_write(path, data)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def _locked_append(path: Path, entry: dict):
    """Append a single entry to a JSON list file with exclusive lock (atomic).

    Uses write-to-temp-then-rename so the data file is never left in a
    truncated state if the process is killed mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(path)
    with open(lock, "a") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            data: list = []
            if path.exists():
                try:
                    with open(path) as f:
                        data = json.load(f)
                except json.JSONDecodeError:
                    data = []
            data.append(entry)
            atomic_json_write(path, data)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Queue Operations
# ---------------------------------------------------------------------------

def read_queue() -> List[dict]:
    """Read the full experiment queue."""
    return _locked_read(QUEUE_PATH)


def append_to_queue(entry: QueueEntry, skip_validation: bool = False) -> str:
    """Add an experiment to the queue. Returns the entry ID.

    Validates the entry against what research_runner.py can actually execute.
    Raises ValueError if the entry has invalid params_override format.
    Pass skip_validation=True only for experimental/sandbox entries.
    """
    if not skip_validation:
        errors = validate_queue_entry(entry)
        if errors:
            raise ValueError(
                f"Queue entry '{entry.id}' failed validation:\n"
                + "\n".join(f"  • {e}" for e in errors)
                + "\n\nFix params_override to match what research_runner.py expects, "
                "or pass skip_validation=True to override."
            )
    _locked_append(QUEUE_PATH, entry.to_dict())
    return entry.id


def update_queue_entry(entry_id: str, updates: Dict[str, Any]):
    """Update fields on a queue entry (with file lock)."""
    queue = _locked_read(QUEUE_PATH)
    for item in queue:
        if item["id"] == entry_id:
            item.update(updates)
            item["updated_at"] = datetime.now(timezone.utc).isoformat()
            break
    _locked_write(QUEUE_PATH, queue)


def claim_experiment(entry_id: str, agent_id: str = "atlas-research") -> Optional[dict]:
    """Atomically claim an experiment. Returns the entry if claimed, None if already claimed."""
    queue = _locked_read(QUEUE_PATH)
    claimed = None
    for item in queue:
        if item["id"] == entry_id:
            if item["status"] not in ("queued", "claimed"):
                return None
            # Stale claim detection: if claimed > 6h ago, release it
            if item["status"] == "claimed" and item.get("claimed_at"):
                claimed_at = datetime.fromisoformat(item["claimed_at"])
                if (datetime.now(timezone.utc) - claimed_at).total_seconds() < 6 * 3600:
                    return None  # Still actively claimed
            item["status"] = ExperimentStatus.CLAIMED
            item["claimed_by"] = agent_id
            item["claimed_at"] = datetime.now(timezone.utc).isoformat()
            item["updated_at"] = datetime.now(timezone.utc).isoformat()
            claimed = item.copy()
            break
    if claimed:
        _locked_write(QUEUE_PATH, queue)
    return claimed


def get_next_queued(market: Optional[str] = None) -> Optional[dict]:
    """Get the highest priority queued experiment whose dependencies are satisfied.

    A dependency is satisfied if:
      - The depended-on experiment exists in the queue
      - AND its status is 'passed' or 'promoted'
    If a dependency has status 'failed' or 'rejected', the dependent experiment
    is automatically deferred (it can't proceed).
    """
    queue = _locked_read(QUEUE_PATH)
    status_by_id = {e["id"]: e.get("status", "queued") for e in queue}

    # Terminal failure states — if a dependency is in one of these, the
    # dependent experiment should be deferred (includes deferred for cascade)
    FAILED_STATES = {ExperimentStatus.FAILED, ExperimentStatus.REJECTED, ExperimentStatus.DEFERRED,
                     ExperimentStatus.SKIPPED,
                     "failed", "rejected", "deferred", "skipped"}
    # States that satisfy a dependency
    PASS_STATES = {ExperimentStatus.PASSED, ExperimentStatus.PROMOTED, "passed", "promoted"}

    candidates = []
    for e in queue:
        if e["status"] != ExperimentStatus.QUEUED:
            continue
        if market is not None and e["market"] != market:
            continue

        deps = e.get("depends_on", [])
        if not deps:
            candidates.append(e)
            continue

        # Check dependency satisfaction
        all_satisfied = True
        any_failed = False
        for dep_id in deps:
            dep_status = status_by_id.get(dep_id)
            if dep_status is None:
                # Dependency not in queue — treat as unsatisfied
                all_satisfied = False
                break
            if dep_status in FAILED_STATES:
                any_failed = True
                break
            if dep_status not in PASS_STATES:
                all_satisfied = False
                break

        if any_failed:
            # Auto-defer experiments whose dependencies failed
            update_queue_entry(e["id"], {
                "status": ExperimentStatus.DEFERRED,
                "notes": e.get("notes", "") + f"\n[auto-deferred] Dependency failed: {deps}",
            })
            continue

        if all_satisfied:
            candidates.append(e)

    if not candidates:
        return None
    # Priority order: P1 < P2 < P3 < P4 < P5 (string sort works)
    candidates.sort(key=lambda e: (e.get("priority", "P5"), e.get("created_at", "")))
    return candidates[0]


# ---------------------------------------------------------------------------
# Journal Operations
# ---------------------------------------------------------------------------

def read_journal() -> List[dict]:
    """Read the full experiment journal (append-only log)."""
    return _locked_read(JOURNAL_PATH)


def append_to_journal(entry: JournalEntry):
    """Append an entry to the journal (never edits existing entries)."""
    _locked_append(JOURNAL_PATH, entry.to_dict())


def get_journal_for_strategy(strategy_name: str) -> List[dict]:
    """Get all journal entries for a specific strategy."""
    return [e for e in read_journal() if e.get("strategy") == strategy_name]


def get_recent_promotions(market: str, days: int = 7) -> List[dict]:
    """Get promotions within the last N days for rate limiting."""
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    journal = read_journal()
    return [
        e for e in journal
        if e.get("promoted")
        and e.get("market") == market
        and datetime.fromisoformat(e["timestamp"]).timestamp() > cutoff
    ]


# ---------------------------------------------------------------------------
# Experiment Operations
# ---------------------------------------------------------------------------

def load_experiment(exp_id: str) -> Optional[dict]:
    """Load an experiment envelope by ID."""
    path = EXPERIMENTS_DIR / f"exp-{exp_id}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def list_experiments(status: Optional[str] = None) -> List[dict]:
    """List all experiment envelopes, optionally filtered by verdict."""
    experiments = []
    if not EXPERIMENTS_DIR.exists():
        return experiments
    for path in sorted(EXPERIMENTS_DIR.glob("exp-*.json")):
        try:
            with open(path) as f:
                exp = json.load(f)
            if status and exp.get("verdict") != status:
                continue
            experiments.append(exp)
        except (json.JSONDecodeError, OSError):
            pass
    return experiments


def generate_experiment_id() -> str:
    """Generate a unique experiment ID."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_uuid = uuid.uuid4().hex[:6]
    return f"{ts}_{short_uuid}"
