"""Freshness guard for research_best writes."""
from datetime import datetime, timezone, timedelta
from typing import Optional
import logging

logger = logging.getLogger(__name__)

DEFAULT_FRESHNESS_DAYS = 14

# Module-level imports for test patchability (lazy-safe: graceful on ImportError)
try:
    from db.atlas_db import get_research_best  # noqa: F401
except Exception:  # pragma: no cover
    get_research_best = None  # type: ignore[assignment]

try:
    from utils.telegram import send_message  # noqa: F401
except Exception:  # pragma: no cover
    send_message = None  # type: ignore[assignment]


def check_freshness(
    strategy: str,
    universe: str,
    candidate_timestamp: Optional[datetime] = None,
    freshness_days: int = DEFAULT_FRESHNESS_DAYS,
    notify: bool = True,
) -> tuple[bool, str]:
    """Decide whether a research_best row should be written.

    Returns (allow, reason). Caller must respect ``allow=False`` and skip the write.

    Rejects if:
      - candidate_timestamp is older than freshness_days (stale source)
      - existing research_best row has updated_at NEWER than candidate_timestamp
        (time-monotonic write — never go backwards)
    """
    now = datetime.now(timezone.utc)
    cand_ts = candidate_timestamp or now
    if cand_ts.tzinfo is None:
        cand_ts = cand_ts.replace(tzinfo=timezone.utc)

    # Guard 1: candidate too old
    age_days = (now - cand_ts).total_seconds() / 86400
    if age_days > freshness_days:
        reason = (
            f"freshness reject: {strategy}/{universe} candidate "
            f"{age_days:.1f}d old > {freshness_days}d threshold"
        )
        logger.warning("[freshness] %s", reason)
        if notify:
            _send_alert(reason)
        return (False, reason)

    # Guard 2: existing row newer than candidate (time-monotonic)
    try:
        _grb = get_research_best  # module-level name, patchable in tests
        if _grb is not None:
            rows = _grb(strategy, universe)
            if rows:
                existing_at_str = rows[0].get("updated_at")
                if existing_at_str:
                    try:
                        existing_at = datetime.fromisoformat(
                            existing_at_str.replace("Z", "+00:00")
                        )
                        if existing_at.tzinfo is None:
                            existing_at = existing_at.replace(tzinfo=timezone.utc)
                        if existing_at > cand_ts:
                            reason = (
                                f"freshness reject: {strategy}/{universe} candidate "
                                f"({cand_ts.isoformat()}) older than existing row "
                                f"({existing_at.isoformat()})"
                            )
                            logger.warning("[freshness] %s", reason)
                            if notify:
                                _send_alert(reason)
                            return (False, reason)
                    except (ValueError, TypeError) as exc:
                        logger.debug("[freshness] existing updated_at parse failed: %s", exc)
    except Exception as exc:
        logger.debug("[freshness] existing-row lookup failed (non-fatal): %s", exc)

    return (True, "fresh")


def _send_alert(reason: str) -> None:
    """Send Telegram alert about a freshness rejection. Non-fatal."""
    try:
        _sm = send_message  # module-level name, patchable in tests
        if _sm is not None:
            _sm(f"⚠️ research_best freshness guard: {reason}", silent=True)
    except Exception as exc:
        logger.debug("[freshness] telegram alert failed (non-fatal): %s", exc)
