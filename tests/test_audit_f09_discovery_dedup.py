"""
tests/test_audit_f09_discovery_dedup.py
=========================================
Regression tests for audit finding F-09: discovery pipeline deduplication bug.

Root cause: research/discovery/arxiv_api.py was calling _mark_url_seen()
(internal seen_urls.txt writer) for every paper immediately after fetching it.
discover_daily() step 3 then called dedup.is_seen() on the SAME seen_urls.txt,
which found all papers already marked → papers_found=0 every run.

Fix (commit c3bb20f7, 2026-05-11):
  - Removed the premature _mark_url_seen() call from fetch_new_papers().
  - dedup.py's mark_seen() remains the sole authority for persisting seen URLs.
  - In-run dedup (seen_this_run set) is retained for cross-query de-duplication
    within a single fetch_new_papers() call but does NOT write to disk.
  - discover_daily() step 3 (is_seen filter) now runs BEFORE step 4/5 (mark_seen).

These tests guard against:
  1. Structural: fetch_new_papers() writing to seen_urls.txt prematurely.
  2. Functional: dedup.py is_seen / mark_seen pure-function correctness.
  3. Sequential: discover_daily step 3 (dedup) precedes mark_seen calls.

Audit ref: F-09 (partial fix; primary _browse_with_pi parse bug deferred).
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARXIV_API_PATH = PROJECT_ROOT / "research" / "discovery" / "arxiv_api.py"
DISCOVERY_PATH = PROJECT_ROOT / "research" / "discovery" / "discovery.py"


# ── Test 1: structural — fetch_new_papers does NOT write to seen_urls ─────────

def test_arxiv_api_does_not_write_seen_urls_immediately() -> None:
    """F-09: fetch_new_papers() must not write URLs to seen_urls.txt.

    The premature write was the root cause of papers_found=0: the function
    appended every fetched URL to seen_urls.txt, so when discover_daily step 3
    called is_seen(), all papers appeared "already processed".

    Fix: the final block of fetch_new_papers() carries a comment
    'F-09 FIX: Do NOT write to seen_urls.txt here' confirming removal.

    This test uses inspect.getsource to verify the fix structurally, not by
    running the full discovery pipeline (which requires Pi CLI + network).
    """
    if not ARXIV_API_PATH.exists():
        pytest.skip(f"arxiv_api.py not found at {ARXIV_API_PATH}")

    import sys
    sys.path.insert(0, str(PROJECT_ROOT))
    from research.discovery.arxiv_api import fetch_new_papers

    source = inspect.getsource(fetch_new_papers)

    # The bug was a call like `_mark_url_seen(url)` inside fetch_new_papers.
    # After the fix, _mark_url_seen is defined but NOT called from this function.
    assert "_mark_url_seen(" not in source, (
        "F-09 regression: fetch_new_papers() contains a call to _mark_url_seen(). "
        "This causes seen_urls.txt to be updated before dedup.is_seen() runs in "
        "discover_daily step 3, making all papers appear 'already seen'."
    )

    # Also check: no open(seen_urls_file, 'a') or equivalent write inside
    # fetch_new_papers body.
    assert 'open(_SEEN_URLS_FILE' not in source or '"a"' not in source.split('open(_SEEN_URLS_FILE')[1][:50] if 'open(_SEEN_URLS_FILE' in source else True, (
        "F-09 regression: fetch_new_papers() contains an open(..., 'a') call on "
        "_SEEN_URLS_FILE. Seen-URL persistence must be dedup.py's exclusive responsibility."
    )

    # Verify the fix comment is present (belt-and-suspenders)
    assert "F-09 FIX" in source, (
        "fetch_new_papers() is missing the 'F-09 FIX' comment. "
        "Was the fix accidentally reverted?"
    )


def test_arxiv_api_fetch_does_not_contain_file_write() -> None:
    """F-09 structural: arxiv_api.py source must not open seen_urls.txt for writing.

    The module defines _mark_url_seen() (which does write) but fetch_new_papers()
    must not call it. This test checks the raw module source as an additional guard.
    """
    if not ARXIV_API_PATH.exists():
        pytest.skip(f"arxiv_api.py not found at {ARXIV_API_PATH}")

    source = ARXIV_API_PATH.read_text()

    # Count occurrences of _mark_url_seen calls vs definitions
    # Definition: `def _mark_url_seen(` — there should be exactly 1
    definitions = source.count("def _mark_url_seen(")
    assert definitions >= 1, "arxiv_api.py must define _mark_url_seen() helper"

    # All call sites: `_mark_url_seen(` — must NOT appear in fetch_new_papers body.
    # Split source to get only the fetch_new_papers function body.
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))
    from research.discovery.arxiv_api import fetch_new_papers

    fn_source = inspect.getsource(fetch_new_papers)
    calls_in_fn = fn_source.count("_mark_url_seen(")
    assert calls_in_fn == 0, (
        f"F-09 regression: _mark_url_seen() is called {calls_in_fn} time(s) inside "
        f"fetch_new_papers(). After the fix, this function must NOT persist seen URLs."
    )


# ── Test 2: dedup.py pure-function correctness ─────────────────────────────────

def test_dedup_is_seen_returns_false_for_new_urls(tmp_path: Path) -> None:
    """F-09 functional: is_seen() returns False for an unknown URL.

    Tests the dedup.py pure function with a fresh seen_urls.txt in a tmp dir.
    """
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))

    # Point dedup at tmp file so tests don't touch production seen_urls.txt
    import research.discovery.dedup as dedup_mod
    original_seen_file = dedup_mod.SEEN_FILE
    dedup_mod.SEEN_FILE = tmp_path / "seen_urls.txt"

    try:
        url = "https://arxiv.org/abs/2401.99999"
        assert not dedup_mod.is_seen(url), (
            f"is_seen('{url}') returned True for a URL that has never been marked. "
            f"Check that SEEN_FILE path is correctly isolated in the test."
        )
    finally:
        dedup_mod.SEEN_FILE = original_seen_file


def test_dedup_mark_seen_then_is_seen_returns_true(tmp_path: Path) -> None:
    """F-09 functional: mark_seen() then is_seen() returns True.

    This is the core dedup contract: once a URL is marked, it is recognised
    as seen in subsequent calls (even across different invocations, since the
    state is persisted to seen_urls.txt).
    """
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))

    import research.discovery.dedup as dedup_mod
    original_seen_file = dedup_mod.SEEN_FILE
    dedup_mod.SEEN_FILE = tmp_path / "seen_urls.txt"

    try:
        url = "https://arxiv.org/abs/2402.11111"
        assert not dedup_mod.is_seen(url), "URL should not be seen before mark_seen()"
        dedup_mod.mark_seen(url, "filtered")
        assert dedup_mod.is_seen(url), (
            f"is_seen('{url}') returned False after mark_seen(). "
            f"Dedup persistence broken."
        )
    finally:
        dedup_mod.SEEN_FILE = original_seen_file


def test_dedup_is_seen_separate_urls_independent(tmp_path: Path) -> None:
    """F-09 functional: marking URL A does not affect is_seen(URL B)."""
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))

    import research.discovery.dedup as dedup_mod
    original_seen_file = dedup_mod.SEEN_FILE
    dedup_mod.SEEN_FILE = tmp_path / "seen_urls.txt"

    try:
        url_a = "https://arxiv.org/abs/2403.00001"
        url_b = "https://arxiv.org/abs/2403.00002"
        dedup_mod.mark_seen(url_a, "filtered")
        assert dedup_mod.is_seen(url_a), "URL A must be seen after marking"
        assert not dedup_mod.is_seen(url_b), "URL B must NOT be seen (not marked)"
    finally:
        dedup_mod.SEEN_FILE = original_seen_file


# ── Test 3: discover_daily dedup step runs before mark_seen ──────────────────

def test_discovery_step_3_calls_dedup_before_marking() -> None:
    """F-09 sequential: discover_daily's is_seen filter (step 3) must precede mark_seen.

    The bug was that arxiv_api.py wrote to seen_urls.txt BEFORE discover_daily
    called dedup.is_seen(), making all papers appear pre-seen.  After the fix:
      - Step 3: for p in papers: if is_seen(url) → skip  (filter)
      - Mark seen: for p in filtered_papers: mark_seen(url)  (only after filter+LLM)

    This test uses inspect.getsource on discover_daily to verify ordering.
    It does NOT run the pipeline (no network, no Pi CLI).
    """
    if not DISCOVERY_PATH.exists():
        pytest.skip(f"discovery.py not found at {DISCOVERY_PATH}")

    import sys
    sys.path.insert(0, str(PROJECT_ROOT))
    from research.discovery.discovery import discover_daily

    source = inspect.getsource(discover_daily)

    # Locate the indices of the key operations in the source text
    is_seen_idx = source.find("is_seen(url)")
    mark_seen_idx = source.find("mark_seen(url")

    assert is_seen_idx != -1, (
        "discover_daily source does not contain 'is_seen(url)'. "
        "Step 3 dedup check was removed or renamed."
    )
    assert mark_seen_idx != -1, (
        "discover_daily source does not contain 'mark_seen(url'. "
        "Seen-URL persistence step was removed or renamed."
    )
    assert is_seen_idx < mark_seen_idx, (
        f"F-09 regression: in discover_daily(), mark_seen (offset {mark_seen_idx}) appears "
        f"BEFORE is_seen filter (offset {is_seen_idx}). "
        f"The dedup check must run before URLs are marked as seen. "
        f"This ordering reversal is exactly the bug that caused papers_found=0."
    )
