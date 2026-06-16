"""Fails if any non-test, non-helper file calls pi/claude CLI as a raw subprocess.
pi_subprocess.py was removed 2026-06-13 (#36 fossil purge — zero importers); the RULE
survives it: any pi/claude subprocess MUST include --system-prompt (Claude Max OAuth
routing, /root/AGENTS.md). pi_session.py carries the flag inline and is allowlisted.
"""
import re
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent
ALLOWED = {
    # pi_session.py uses asyncio.create_subprocess_exec (async streaming);
    # --system-prompt is present inline — the routing guarantee the rule protects.
    ATLAS_ROOT / "atlas" / "dashboard" / "chat" / "pi_session.py",
}

PATTERN = re.compile(r'["\'](?:pi|claude)["\'],\s*["\'](?:-p|--print)["\']')


def test_no_raw_pi_subprocess():
    offenders = []
    for py in ATLAS_ROOT.rglob("*.py"):
        if py.resolve() in {p.resolve() for p in ALLOWED}:
            continue
        parts = py.parts
        if "tests" in parts or "__pycache__" in parts or py.name.startswith("test_"):
            continue
        try:
            text = py.read_text()
        except Exception:
            continue
        if PATTERN.search(text):
            offenders.append(str(py.relative_to(ATLAS_ROOT)))
    assert not offenders, (
        f"Found raw pi/claude subprocess calls outside the allowlist (pi_session.py) in: "
        f"{offenders}. Any pi/claude subprocess MUST pass --system-prompt (Max OAuth routing — see /root/AGENTS.md)."
    )
