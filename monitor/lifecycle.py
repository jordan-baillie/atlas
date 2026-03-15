"""Strategy Lifecycle Manager — automated state machine for strategy health.

States: RAMP_UP → ACTIVE → WATCH → PROBATION → SUSPENDED
Recovery: WATCH/PROBATION → ACTIVE when performance recovers.

Transitions:
  ACTIVE    + WARNING  → WATCH       (pool_cap -= 1)
  ACTIVE    + DEGRADED → WATCH       (pool_cap -= 1)
  WATCH     + HEALTHY  → ACTIVE      (after 2 consecutive, reset cap)
  WATCH     + DEGRADED (≥3) → PROBATION (pool_cap = 1)
  PROBATION + HEALTHY  → ACTIVE      (after 2 consecutive, reset cap)
  PROBATION + DEGRADED (≥4) → SUSPENDED (pool_cap = 0)
  SUSPENDED + HEALTHY  → PROBATION   (after 2 consecutive, pool_cap = 1)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT = Path(__file__).resolve().parent.parent


# ── State enum ────────────────────────────────────────────────────────────────

class LifecycleState(str, Enum):
    RAMP_UP    = "RAMP_UP"     # First 30 days, pool_cap=1
    ACTIVE     = "ACTIVE"      # Normal operation
    WATCH      = "WATCH"       # Sharpe < 50% backtest, pool_cap -= 1
    PROBATION  = "PROBATION"   # Sharpe < 0 for 3+ weeks, pool_cap=1
    SUSPENDED  = "SUSPENDED"   # 4+ reports degraded, pool_cap=0


# ── Record dataclass ──────────────────────────────────────────────────────────

@dataclass
class LifecycleRecord:
    """Persisted lifecycle state for a single strategy."""

    strategy: str
    state: LifecycleState
    entered_at: str                        # ISO datetime of last transition
    consecutive_degraded: int = 0
    consecutive_recovered: int = 0
    pool_cap_override: Optional[int] = None
    history: List[Dict] = field(default_factory=list)


# ── Manager class ─────────────────────────────────────────────────────────────

class StrategyLifecycleManager:
    """Manages strategy lifecycle transitions based on weekly health reports.

    State is persisted to ``logs/lifecycle_state.json`` so transitions survive
    process restarts.

    Args:
        config: Active market config dict (from utils.config.get_active_config).
        market_id: Market identifier, e.g. 'sp500'.
    """

    LIFECYCLE_FILE: Path = PROJECT / "logs" / "lifecycle_state.json"

    def __init__(self, config: dict, market_id: str = "sp500") -> None:
        self.config = config
        self.market_id = market_id
        self.records: Dict[str, LifecycleRecord] = {}
        self._load_state()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_state(self) -> None:
        """Load persisted lifecycle state from JSON file.

        Strategies not yet in the file are initialized as ACTIVE so that
        newly-enabled strategies automatically enter the normal lifecycle.
        """
        if self.LIFECYCLE_FILE.exists():
            try:
                data = json.loads(self.LIFECYCLE_FILE.read_text())
                for name, rec in data.items():
                    self.records[name] = LifecycleRecord(
                        strategy=name,
                        state=LifecycleState(rec["state"]),
                        entered_at=rec["entered_at"],
                        consecutive_degraded=rec.get("consecutive_degraded", 0),
                        consecutive_recovered=rec.get("consecutive_recovered", 0),
                        pool_cap_override=rec.get("pool_cap_override"),
                        history=rec.get("history", []),
                    )
            except Exception as exc:
                logger.warning("Failed to load lifecycle state from %s: %s", self.LIFECYCLE_FILE, exc)

        # Initialize any missing enabled strategies as ACTIVE
        strategies = self.config.get("strategies", {})
        for name, cfg in strategies.items():
            if isinstance(cfg, dict) and cfg.get("enabled", False) and name not in self.records:
                self.records[name] = LifecycleRecord(
                    strategy=name,
                    state=LifecycleState.ACTIVE,
                    entered_at=datetime.now().isoformat(),
                )
                logger.debug("Initialized new strategy '%s' as ACTIVE", name)

    def _save_state(self) -> None:
        """Persist lifecycle state to JSON file (atomic-ish via write)."""
        self.LIFECYCLE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data: Dict[str, dict] = {}
        for name, rec in self.records.items():
            serialized = asdict(rec)
            # Ensure state is stored as the string value (not enum object)
            serialized["state"] = rec.state.value
            data[name] = serialized
        self.LIFECYCLE_FILE.write_text(json.dumps(data, indent=2, default=str))
        logger.debug("Lifecycle state saved (%d strategies)", len(data))

    # ── Public API ────────────────────────────────────────────────────────────

    def get_state(self, strategy: str) -> LifecycleState:
        """Get the current lifecycle state for a strategy.

        Returns ACTIVE for strategies not yet tracked (safe default).
        """
        if strategy in self.records:
            return self.records[strategy].state
        return LifecycleState.ACTIVE

    def get_effective_pool_cap(self, strategy: str) -> Optional[int]:
        """Get the lifecycle-adjusted pool cap override.

        Returns:
            int override if a lifecycle cap is active (0 = suspended),
            None if no override (use the config default).
        """
        rec = self.records.get(strategy)
        if rec is not None and rec.pool_cap_override is not None:
            return rec.pool_cap_override
        return None

    def get_all_states(self) -> Dict[str, str]:
        """Return a snapshot of current state for all tracked strategies."""
        return {name: rec.state.value for name, rec in self.records.items()}

    def process_health_report(self, health_report) -> List[Dict]:
        """Process a HealthReport and execute any required state transitions.

        Args:
            health_report: A HealthReport from StrategyHealthMonitor —
                           must have an ``assessments`` list of HealthAssessment
                           objects, each with ``strategy`` and ``status`` fields.

        Returns:
            List of transition dicts::

                [
                    {
                        "strategy": "mean_reversion",
                        "from": "ACTIVE",
                        "to": "WATCH",
                        "reason": "Health status: WARNING",
                        "timestamp": "2026-03-15T09:00:00",
                    },
                    ...
                ]
        """
        transitions: List[Dict] = []

        for assessment in health_report.assessments:
            strategy = assessment.strategy
            status = assessment.status  # HEALTHY, WARNING, DEGRADED, INSUFFICIENT_DATA

            # Skip strategies without enough data — no transition possible
            if status == "INSUFFICIENT_DATA":
                continue

            rec = self.records.get(strategy)
            if rec is None:
                logger.debug("Strategy '%s' not in lifecycle records, skipping", strategy)
                continue

            old_state = rec.state
            new_state = self._compute_transition(rec, status)

            if new_state != old_state:
                transition = {
                    "strategy": strategy,
                    "from": old_state.value,
                    "to": new_state.value,
                    "reason": f"Health status: {status}",
                    "timestamp": datetime.now().isoformat(),
                }
                transitions.append(transition)
                rec.history.append(transition)
                rec.state = new_state
                rec.entered_at = datetime.now().isoformat()
                logger.info(
                    "Lifecycle transition: %s %s → %s (reason=%s)",
                    strategy, old_state.value, new_state.value, status
                )

        self._save_state()
        return transitions

    # ── Transition logic ──────────────────────────────────────────────────────

    def _compute_transition(self, rec: LifecycleRecord, health_status: str) -> LifecycleState:
        """Compute the next lifecycle state given the current record and health status.

        Mutates the ``consecutive_degraded``, ``consecutive_recovered``, and
        ``pool_cap_override`` fields on *rec* as a side effect — the caller is
        responsible for updating ``rec.state`` if the returned state differs.
        """
        current = rec.state

        # ── HEALTHY ───────────────────────────────────────────────────────────
        if health_status == "HEALTHY":
            rec.consecutive_degraded = 0
            rec.consecutive_recovered += 1

            if current in (LifecycleState.WATCH, LifecycleState.PROBATION):
                if rec.consecutive_recovered >= 2:
                    rec.consecutive_recovered = 0
                    rec.pool_cap_override = None  # restore to config default
                    return LifecycleState.ACTIVE

            elif current == LifecycleState.SUSPENDED:
                if rec.consecutive_recovered >= 2:
                    rec.consecutive_recovered = 0
                    rec.pool_cap_override = 1     # cautious restart
                    return LifecycleState.PROBATION

            # ACTIVE + HEALTHY, or insufficient consecutive recoveries → stay
            return current

        # ── WARNING ───────────────────────────────────────────────────────────
        elif health_status == "WARNING":
            rec.consecutive_recovered = 0
            rec.consecutive_degraded += 1

            if current == LifecycleState.ACTIVE:
                default_cap = self._get_default_pool_cap(rec.strategy)
                rec.pool_cap_override = max(1, default_cap - 1)
                return LifecycleState.WATCH

            # WATCH/PROBATION/SUSPENDED + WARNING → stay (already restricted)
            return current

        # ── DEGRADED ─────────────────────────────────────────────────────────
        elif health_status == "DEGRADED":
            rec.consecutive_recovered = 0
            rec.consecutive_degraded += 1

            if current in (LifecycleState.ACTIVE, LifecycleState.WATCH):
                if rec.consecutive_degraded >= 3:
                    rec.pool_cap_override = 1
                    return LifecycleState.PROBATION
                else:
                    default_cap = self._get_default_pool_cap(rec.strategy)
                    rec.pool_cap_override = max(1, default_cap - 1)
                    return LifecycleState.WATCH

            elif current == LifecycleState.PROBATION:
                if rec.consecutive_degraded >= 4:
                    rec.pool_cap_override = 0
                    return LifecycleState.SUSPENDED

            # SUSPENDED + DEGRADED → remain suspended
            return current

        # Unknown status — no-op
        return current

    def _get_default_pool_cap(self, strategy: str) -> int:
        """Return the configured pool cap for a strategy.

        Reads ``allocation.pools.<strategy>.max_positions`` from config,
        defaulting to 3 if not set.
        """
        pools = self.config.get("allocation", {}).get("pools", {})
        return pools.get(strategy, {}).get("max_positions", 3)
