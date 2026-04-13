#!/usr/bin/env python3
"""Claude CLI authentication checker.

Used by research scripts to verify Claude is authenticated before attempting LLM calls.
"""
import json
import os
import subprocess
import sys


def check_claude_auth() -> dict:
    """Check Claude CLI authentication status.
    
    Checks in order:
    1. ANTHROPIC_API_KEY environment variable (fastest, works in systemd)
    2. Claude CLI auth status (OAuth/token-based)
    
    Returns:
        dict with keys: logged_in (bool), method (str), error (str or None)
    """
    # Fast path: check environment variable directly
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key.startswith("sk-ant-"):
        return {
            "logged_in": True,
            "method": "api_key",
            "error": None
        }
    
    # Fallback: check Claude CLI auth status
    try:
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout.strip() or result.stderr.strip()
        try:
            data = json.loads(output)
            return {
                "logged_in": data.get("loggedIn", False),
                "method": data.get("authMethod", "none"),
                "error": None
            }
        except json.JSONDecodeError:
            return {"logged_in": False, "method": "unknown", "error": f"Parse error: {output[:100]}"}
    except FileNotFoundError:
        return {"logged_in": False, "method": "none", "error": "claude CLI not found"}
    except subprocess.TimeoutExpired:
        return {"logged_in": False, "method": "none", "error": "auth check timed out"}
    except Exception as e:
        return {"logged_in": False, "method": "none", "error": str(e)}


def require_claude_auth(context: str = "research") -> bool:
    """Check auth and print helpful message if not authenticated. Returns True if OK."""
    status = check_claude_auth()
    if status["logged_in"]:
        return True
    
    print(f"\n{'='*60}")
    print(f"⚠️  Claude CLI not authenticated — {context} LLM features disabled")
    print(f"{'='*60}")
    print(f"Auth method: {status['method']}")
    if status["error"]:
        print(f"Error: {status['error']}")
    print()
    print("To fix, run ONE of these (as root):")
    print("  1. export ANTHROPIC_API_KEY=sk-ant-...  # Environment variable (recommended for systemd)")
    print("  2. claude setup-token                    # Long-lived token")
    print("  3. claude auth login                     # OAuth browser flow")
    print()
    print("After auth, verify with: claude auth status")
    print(f"{'='*60}\n")
    return False


if __name__ == "__main__":
    ok = require_claude_auth("CLI check")
    sys.exit(0 if ok else 1)
