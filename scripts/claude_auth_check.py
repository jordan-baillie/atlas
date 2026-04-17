#!/usr/bin/env python3
"""Pi CLI authentication checker.

Used by research scripts to verify Pi CLI is available and working before attempting LLM calls.
Pi uses OAuth which is configured automatically — this just verifies the CLI is callable.
"""
import subprocess
import sys
import os as _os
# Allow running from scripts/ dir — ensure atlas root is on path
_ATLAS_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ATLAS_ROOT not in sys.path:
    sys.path.insert(0, _ATLAS_ROOT)


def check_pi_auth() -> dict:
    """Check Pi CLI availability and authentication.

    Verifies that the pi CLI is installed and can run successfully.
    Pi uses OAuth which should be configured automatically.

    Returns:
        dict with keys: logged_in (bool), method (str), error (str or None)
    """
    from utils.pi_subprocess import call_pi, PiSubprocessError  # noqa: PLC0415

    try:
        # call_pi raises PiSubprocessError for non-zero exit, timeout, missing
        # binary, and known quota/auth error strings in stdout.
        call_pi("echo ok", mode=None, timeout=15, extra_args=["--no-tools"])
        return {"logged_in": True, "method": "oauth", "error": None}

    except PiSubprocessError as e:
        err_msg = str(e)
        try:
            from utils.claude_circuit_breaker import scan_and_trip
            scan_and_trip(err_msg, reason_prefix="auth_check")
        except Exception:
            pass
        if "not found on PATH" in err_msg:
            return {
                "logged_in": False,
                "method": "none",
                "error": "Pi CLI not found. Ensure pi is installed and on PATH.",
            }
        if "timed out" in err_msg:
            return {
                "logged_in": False,
                "method": "none",
                "error": "Pi CLI timed out during auth check.",
            }
        return {
            "logged_in": False,
            "method": "error",
            "error": f"Pi CLI auth/availability issue: {err_msg[:300]}",
        }
    except Exception as e:
        return {
            "logged_in": False,
            "method": "none",
            "error": f"Pi auth check error: {e}",
        }


# Keep backward compatibility alias
check_claude_auth = check_pi_auth


def require_pi_auth(context: str = "research") -> bool:
    """Check auth and print helpful message if not available. Returns True if OK."""
    status = check_pi_auth()
    if status["logged_in"]:
        return True

    print(f"\n{'='*60}")
    print(f"⚠️  Pi CLI not available — {context} LLM features disabled")
    print(f"{'='*60}")
    if status["error"]:
        print(f"Error: {status['error']}")
    print()
    print("To fix:")
    print("  1. Ensure pi is installed and on PATH")
    print("  2. Run: pi -p 'test'   to verify it works")
    print(f"{'='*60}\n")
    return False


# Keep backward compatibility alias  
require_claude_auth = require_pi_auth


if __name__ == "__main__":
    ok = require_pi_auth("CLI check")
    sys.exit(0 if ok else 1)
