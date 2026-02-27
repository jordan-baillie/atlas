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
        """Save this envelope to research/experiments/exp-{id}.json."""
        EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
        path = EXPERIMENTS_DIR / f"exp-{self.id}.json"
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)
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
# File-locked read/write helpers
# ---------------------------------------------------------------------------

def _locked_read(path: Path) -> list:
    """Read a JSON list file with shared lock."""
    if not path.exists():
        return []
    with open(path, "r") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            data = []
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return data


def _locked_write(path: Path, data: list):
    """Write a JSON list file with exclusive lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(data, f, indent=2, default=str)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _locked_append(path: Path, entry: dict):
    """Append a single entry to a JSON list file with exclusive lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Read-modify-write with exclusive lock
    if not path.exists():
        with open(path, "w") as f:
            json.dump([], f)

    with open(path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            data = json.load(f)
            data.append(entry)
            f.seek(0)
            f.truncate()
            json.dump(data, f, indent=2, default=str)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Queue Operations
# ---------------------------------------------------------------------------

def read_queue() -> List[dict]:
    """Read the full experiment queue."""
    return _locked_read(QUEUE_PATH)


def append_to_queue(entry: QueueEntry) -> str:
    """Add an experiment to the queue. Returns the entry ID."""
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
                     "failed", "rejected", "deferred"}
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
