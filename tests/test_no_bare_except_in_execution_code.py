"""
Tests for X4 — bare-except cleanup in execution code.

These are shape/window tests: they read the source files and verify that
the two previously-silent except blocks now log a warning instead of
swallowing the exception.
"""
import re
from pathlib import Path

ATLAS_ROOT = Path(__file__).parent.parent


def _read(rel: str) -> str:
    return (ATLAS_ROOT / rel).read_text()


# ---------------------------------------------------------------------------
# Test 1 — live_executor.py leverage-gate Telegram block
# ---------------------------------------------------------------------------

def test_live_executor_leverage_gate_telegram_logs_failure() -> None:
    """
    The try/except around the LEVERAGE GATE BLOCKED Telegram alert must
    log a warning on failure — not swallow it silently.
    """
    src = _read("brokers/live_executor.py")

    # Locate the anchor
    anchor_idx = src.index("LEVERAGE GATE BLOCKED")

    # Grab a generous window: 200 chars before + 500 chars after the anchor
    window = src[anchor_idx - 200 : anchor_idx + 500]

    # Must have logger.warning in the window
    assert "logger.warning(" in window, (
        "Expected logger.warning() near LEVERAGE GATE BLOCKED block, but not found.\n"
        f"Window:\n{window}"
    )

    # Must NOT have a bare silent pass after an except
    # Pattern: "except Exception:\n" followed (possibly with whitespace) by "pass"
    bare_silent = re.search(r"except Exception:\s*\n\s*pass", window)
    assert bare_silent is None, (
        "Found bare 'except Exception: pass' in leverage-gate window -- still silent!\n"
        f"Window:\n{window}"
    )


# ---------------------------------------------------------------------------
# Test 2 — live_portfolio.py dual_write_market_state Telegram block
# ---------------------------------------------------------------------------

def test_live_portfolio_dualwrite_telegram_logs_failure() -> None:
    """
    The try/except around the dual_write_market_state Telegram alert must
    log a warning on failure — not swallow it silently.
    """
    src = _read("brokers/live_portfolio.py")

    anchor_idx = src.index("dual_write_market_state failed for")

    # Window: 200 chars before + 500 chars after the anchor
    window = src[anchor_idx - 200 : anchor_idx + 500]

    assert "logger.warning(" in window, (
        "Expected logger.warning() near dual_write_market_state Telegram block.\n"
        f"Window:\n{window}"
    )

    bare_silent = re.search(r"except Exception:\s*\n\s*pass", window)
    assert bare_silent is None, (
        "Found bare 'except Exception: pass' in dual_write window -- still silent!\n"
        f"Window:\n{window}"
    )


# ---------------------------------------------------------------------------
# Test 3 — no silent except blocks anywhere in either file
# ---------------------------------------------------------------------------

def test_no_silent_except_in_execution_files() -> None:
    """
    Neither brokers/live_executor.py nor brokers/live_portfolio.py should
    contain any occurrence of:

        except Exception:
            pass   <- exactly one indented line, nothing else

    i.e. a truly silent swallow-and-forget pattern.
    """
    files = [
        "brokers/live_executor.py",
        "brokers/live_portfolio.py",
    ]

    violations: list[str] = []

    pattern = re.compile(r"except Exception:\s*\n(\s*)pass\b")

    for rel in files:
        src = _read(rel)
        match = pattern.search(src)
        if match:
            # Find the line number for a helpful error message
            line_no = src[: match.start()].count("\n") + 1
            violations.append(f"  {rel}  line ~{line_no}: bare 'except Exception: pass'")

    assert not violations, (
        "Silent bare-except blocks found -- failures will be swallowed:\n"
        + "\n".join(violations)
    )
