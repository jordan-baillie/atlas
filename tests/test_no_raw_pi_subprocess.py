"""Fails if any non-test, non-helper file calls pi/claude CLI as a raw subprocess
instead of going through atlas.kernel.pi_subprocess. This prevents re-introduction of
calls that bypass the Claude Max OAuth routing guard.
"""
import re
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent
ALLOWED = {
    ATLAS_ROOT / "atlas" / "kernel" / "pi_subprocess.py",
    # pi_session.py uses asyncio.create_subprocess_exec (async streaming) —
    # a different execution model that cannot use call_pi directly.
    # --system-prompt is present inline; equivalent routing guarantee.
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
        f"Found raw pi/claude subprocess calls outside atlas.kernel.pi_subprocess in: "
        f"{offenders}. Use call_pi() / call_pi_exec() from atlas.kernel.pi_subprocess instead."
    )
