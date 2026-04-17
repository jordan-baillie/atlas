"""Central circuit breaker for all pi CLI callers.

When Claude Max subscription hits 'out of extra usage' or similar exhaustion,
any caller sets the breaker. All callers check before spending a pi invocation.
Auto-resets after 5-hour cooldown.
"""
import json
import os
import sys
import time
from pathlib import Path

BREAKER_FILE = Path(os.environ.get("CLAUDE_BREAKER_FILE", "/tmp/claude_breaker.json"))
COOLDOWN_SEC = 5 * 3600  # 5 hours

# Substrings in pi output/stderr that should trip the breaker
TRIP_MARKERS = (
    "out of extra usage",
    "rate_limit_error",
    "insufficient_quota",
    "usage_limit",
)


def is_tripped() -> bool:
    """Return True if breaker is currently tripped AND still within cooldown.
    Auto-removes the breaker file once cooldown expires."""
    try:
        if not BREAKER_FILE.exists():
            return False
        data = json.loads(BREAKER_FILE.read_text())
        tripped_at = float(data.get("tripped_at", 0))
        if tripped_at <= 0:
            return False
        elapsed = time.time() - tripped_at
        if elapsed >= COOLDOWN_SEC:
            # Cooldown expired — self-heal
            try:
                BREAKER_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            return False
        return True
    except Exception:
        return False


def trip(reason: str = "") -> None:
    """Write breaker file with current timestamp and reason. Idempotent/overwrite OK."""
    try:
        payload = {
            "tripped_at": time.time(),
            "reason": reason,
            "pid": os.getpid(),
        }
        BREAKER_FILE.write_text(json.dumps(payload))
    except Exception:
        pass


def reset() -> None:
    """Manual reset — delete breaker file if it exists."""
    try:
        BREAKER_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def remaining_cooldown_sec() -> int:
    """Seconds left on cooldown, 0 if not tripped."""
    try:
        if not BREAKER_FILE.exists():
            return 0
        data = json.loads(BREAKER_FILE.read_text())
        tripped_at = float(data.get("tripped_at", 0))
        if tripped_at <= 0:
            return 0
        elapsed = time.time() - tripped_at
        remaining = COOLDOWN_SEC - elapsed
        return max(0, int(remaining))
    except Exception:
        return 0


def check_or_skip(context: str = "pi call") -> None:
    """Raise RuntimeError with clear message if breaker tripped. Safe no-op if not."""
    if is_tripped():
        mins = remaining_cooldown_sec() // 60
        try:
            data = json.loads(BREAKER_FILE.read_text())
            reason = data.get("reason", "unknown")
        except Exception:
            reason = "unknown"
        raise RuntimeError(
            f"Claude circuit breaker tripped ({context}): {mins}m cooldown remaining. "
            f"Reason: {reason}. Reset with: python3 -m utils.claude_circuit_breaker reset"
        )


def scan_and_trip(text: str, reason_prefix: str = "") -> bool:
    """Inspect text (stdout+stderr combined) for trip markers. If found, trip and return True."""
    try:
        lower = text.lower()
        for marker in TRIP_MARKERS:
            if marker.lower() in lower:
                reason = f"{reason_prefix}: {marker}" if reason_prefix else marker
                trip(reason)
                return True
        return False
    except Exception:
        return False


# ── CLI entrypoint ────────────────────────────────────────────────────────────

def _cli_status() -> None:
    if is_tripped():
        mins = remaining_cooldown_sec() // 60
        try:
            data = json.loads(BREAKER_FILE.read_text())
            reason = data.get("reason", "unknown")
            pid = data.get("pid", "?")
        except Exception:
            reason = "unknown"
            pid = "?"
        print(f"TRIPPED — {mins}m remaining (reason={reason!r}, pid={pid})")
    else:
        print("CLEAR — circuit breaker is not active")


def _cli_trip(args: list[str]) -> None:
    reason = " ".join(args) if args else "manual_trip"
    trip(reason)
    print(f"Breaker tripped (reason={reason!r})")


def _cli_reset() -> None:
    reset()
    print("Breaker reset — /tmp/claude_breaker.json removed (if it existed)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    extra = sys.argv[2:]
    if cmd == "status":
        _cli_status()
    elif cmd == "trip":
        _cli_trip(extra)
    elif cmd == "reset":
        _cli_reset()
    else:
        print(f"Usage: python3 -m utils.claude_circuit_breaker status|trip [reason]|reset")
        sys.exit(1)
