"""Strategy promotion lifecycle state machine.

Tracks (strategy, universe) tuples through stages:
    RESEARCH → PAPER → LIVE → RETIRED

This is SEPARATE from monitor/lifecycle.py (which tracks operational health
of LIVE strategies via RAMP_UP / ACTIVE / WATCH / PROBATION / SUSPENDED).

Promotion lifecycle answers: "where in the activation pipeline is this combo?"
Health lifecycle answers:    "is this LIVE combo performing OK?"

Both state machines can consult each other for the dashboard view.

Persistence: db/atlas_db.py strategy_lifecycle and strategy_lifecycle_history
tables (schema in db/schema.sql).
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import List, Optional, Dict

from db.atlas_db import (
    get_lifecycle_state,
    set_lifecycle_state,
    list_lifecycle_states,
)

logger = logging.getLogger(__name__)


# ── Promotion state enum ──────────────────────────────────────────────────────

class PromotionState(str, Enum):
    """States in the strategy activation (promotion) pipeline."""
    RESEARCH = "RESEARCH"  # discovered by research engine, not yet paper-traded
    PAPER    = "PAPER"     # running on Alpaca paper account for validation
    LIVE     = "LIVE"      # trading real capital
    RETIRED  = "RETIRED"   # decommissioned; no longer trading


# ── Allowed transition graph ─────────────────────────────────────────────────
# operator='system' MUST follow this graph.
# operator='manual' or operator other than 'system' bypasses with a WARNING.

ALLOWED_TRANSITIONS: Dict[Optional[PromotionState], set] = {
    None: {PromotionState.RESEARCH, PromotionState.LIVE},     # initial seed
    PromotionState.RESEARCH: {PromotionState.PAPER, PromotionState.RETIRED},
    PromotionState.PAPER:    {
        PromotionState.LIVE,
        PromotionState.RESEARCH,   # auto-rollback: failed paper phase
        PromotionState.RETIRED,
    },
    PromotionState.LIVE: {
        PromotionState.RETIRED,
        PromotionState.PAPER,      # "soft rollback" — downgrade without full retire
    },
    PromotionState.RETIRED: {PromotionState.RESEARCH},        # revival path
}


# ── Public API ────────────────────────────────────────────────────────────────

def get_state(strategy: str, universe: str) -> Optional[PromotionState]:
    """Return the current promotion state for (strategy, universe), or None if not tracked."""
    raw = get_lifecycle_state(strategy, universe)
    if raw is None:
        return None
    try:
        return PromotionState(raw)
    except ValueError:
        logger.warning(
            "get_state: unknown promotion state %r for (%s, %s) — returning None",
            raw, strategy, universe,
        )
        return None


def transition(
    strategy: str,
    universe: str,
    new_state: PromotionState,
    reason: str = "",
    auto_promotion_id: Optional[str] = None,
    operator: str = "system",
) -> None:
    """Transition (strategy, universe) to new_state.

    For operator='system' (default): validates against ALLOWED_TRANSITIONS
    and raises ValueError on disallowed moves.

    For any other operator value (e.g. 'manual', or a username): the
    transition is allowed regardless of the graph — a WARNING is logged
    so the override is visible in alerts and audit logs.

    Args:
        strategy:          Strategy name, e.g. 'momentum_breakout'.
        universe:          Universe, e.g. 'sp500'.
        new_state:         Target PromotionState.
        reason:            Human/system-readable justification. Stored in DB.
        auto_promotion_id: Optional reference ID for the promotion run
                           (links to auto_promote audit trail — deferred).
        operator:          'system' (graph-enforced) or anything else (manual
                           override, graph bypassed with warning).

    Raises:
        ValueError: if operator='system' and the transition is not in
                    ALLOWED_TRANSITIONS.
    """
    current = get_state(strategy, universe)
    allowed = ALLOWED_TRANSITIONS.get(current, set())

    if new_state not in allowed:
        if operator == "system":
            raise ValueError(
                f"Disallowed system transition {current!r} → {new_state!r} "
                f"for ({strategy}, {universe}). "
                f"Allowed from {current!r}: {sorted(s.value for s in allowed) or 'none'}. "
                f"Use operator='manual' to override with audit trail."
            )
        else:
            logger.warning(
                "MANUAL OVERRIDE: (%s, %s) %r → %r (not in allowed graph). "
                "operator=%r reason=%r",
                strategy, universe, current, new_state, operator, reason,
            )

    set_lifecycle_state(
        strategy=strategy,
        universe=universe,
        new_state=new_state.value,
        reason=reason,
        auto_promotion_id=auto_promotion_id,
        operator=operator,
    )

    logger.info(
        "lifecycle transition: (%s, %s) %r → %r  operator=%r  reason=%r",
        strategy, universe,
        current.value if current else None,
        new_state.value,
        operator, reason,
    )


def is_live(strategy: str, universe: str) -> bool:
    """Return True iff (strategy, universe) is in LIVE promotion state."""
    return get_state(strategy, universe) == PromotionState.LIVE


def is_paper(strategy: str, universe: str) -> bool:
    """Return True iff (strategy, universe) is in PAPER promotion state."""
    return get_state(strategy, universe) == PromotionState.PAPER


def list_state(state: PromotionState) -> List[Dict]:
    """Return all (strategy, universe) rows in the given state.

    Each row is a dict with keys: strategy, universe, state, entered_state_at,
    prev_state, transition_reason, paper_start_date, paper_end_date,
    auto_promotion_id, notes.
    """
    return list_lifecycle_states(state.value)
