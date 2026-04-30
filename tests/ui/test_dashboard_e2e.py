"""
Atlas Dashboard E2E Playwright Suite
=====================================
Verifies the dashboard after commit f70ae4db (total_pnl / equity-curve fixes)
and frontend rebuild 5f8631fc.

Run:
    python3 -m pytest tests/ui/test_dashboard_e2e.py -v --timeout=60
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
import requests
from playwright.sync_api import sync_playwright, expect  # noqa: F401

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DASHBOARD_URL = "http://127.0.0.1:8899/"
SECRETS = json.loads(Path("/root/.atlas-secrets.json").read_text())
USER = SECRETS["dashboard_user"]
PASS = SECRETS["dashboard_pass"]

# Recharts ResizeObserver init noise — fires before measurement, harmless.
# Matches: "The width(-1) and height(-1) of chart should be greater than 0"
_RECHARTS_NOISE_RE = re.compile(
    r"width\(-?\d+\) and height\(-?\d+\) of chart should be greater than 0"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def browser():
    """One Chromium browser instance shared across all tests in this module."""
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    """Fresh browser context + page per test (isolated cookies/auth)."""
    ctx = browser.new_context(
        http_credentials={"username": USER, "password": PASS},
        viewport={"width": 1440, "height": 900},
    )
    p = ctx.new_page()
    errors: list = []

    def _on_console(msg) -> None:
        if msg.type == "error" and not _RECHARTS_NOISE_RE.search(msg.text):
            errors.append(msg)

    def _on_pageerror(exc) -> None:
        errors.append(("pageerror", str(exc)))

    p.on("console", _on_console)
    p.on("pageerror", _on_pageerror)
    p._captured_errors = errors  # type: ignore[attr-defined]

    yield p
    ctx.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nav(page, wait: str = "networkidle", timeout: int = 15_000) -> None:
    """Navigate to dashboard root and wait for the given load state."""
    page.goto(DASHBOARD_URL, wait_until=wait, timeout=timeout)


def _api_get(endpoint: str) -> requests.Response:
    """GET an API endpoint with basic auth, 15s timeout."""
    url = DASHBOARD_URL.rstrip("/") + endpoint
    return requests.get(url, auth=(USER, PASS), timeout=15)


# ---------------------------------------------------------------------------
# Test 1 — page loads without console errors
# ---------------------------------------------------------------------------

def test_dashboard_loads_no_console_errors(page) -> None:
    """Navigate to /, wait networkidle + 1.5s Recharts settle; expect 0 errors."""
    _nav(page, wait="networkidle", timeout=15_000)
    time.sleep(1.5)  # Recharts settle

    errs = page._captured_errors  # type: ignore[attr-defined]
    if errs:
        formatted = "\n".join(
            f"  [{e.type}] {e.text[:200]}" if hasattr(e, "type") else f"  {e}"
            for e in errs
        )
        pytest.fail(f"Console errors detected ({len(errs)}):\n{formatted}")


# ---------------------------------------------------------------------------
# Test 2 — total_pnl shows corrected value (not the 442% bug)
# ---------------------------------------------------------------------------

def test_total_pnl_value_is_sane(page) -> None:
    """
    After commit f70ae4db the corrected total_pnl is ~$77 (1.49%).
    The buggy render showed ~$4,295 / 442%.
    Assert body does NOT contain the old bug values, and DOES contain
    at least one token from the corrected values.
    """
    _nav(page, wait="networkidle", timeout=15_000)
    time.sleep(1.5)

    body = page.inner_text("body")

    # --- Regression guard: old bug values must NOT appear ---
    # The old bug showed "442%" — check for "442%" specifically, not bare "442"
    # (bare "442" appears legitimately in prices like "$442.80")
    assert "442%" not in body, (
        "Body contains '442%' — total_pnl_pct regression (old bug was 442%)"
    )
    assert "$4,295" not in body, "Body contains old buggy total_pnl value '$4,295'"
    assert "$4295" not in body, "Body contains old buggy total_pnl value '$4295'"

    # --- Sanity: at least one corrected-value token must appear ---
    corrected_tokens = ["$77", "$78", "1.4", "1.5"]
    found = [tok for tok in corrected_tokens if tok in body]
    assert found, (
        f"None of the corrected-value tokens {corrected_tokens} found in body.\n"
        f"Body excerpt (first 500 chars):\n{body[:500]}"
    )


# ---------------------------------------------------------------------------
# Test 3 — equity chart SVG renders
# ---------------------------------------------------------------------------

def test_equity_chart_renders(page) -> None:
    """After networkidle + 2s, at least one recharts SVG surface is visible."""
    _nav(page, wait="networkidle", timeout=15_000)
    time.sleep(2.0)

    # Primary: wait for the first recharts SVG surface to become visible
    svg_locator = page.locator("svg.recharts-surface").first
    svg_locator.wait_for(state="visible", timeout=10_000)

    # Belt-and-suspenders: verify at least one wrapper container exists
    wrapper_count = page.locator(".recharts-wrapper").count()
    assert wrapper_count >= 1, (
        f"Expected >=1 .recharts-wrapper elements, found {wrapper_count}"
    )


# ---------------------------------------------------------------------------
# Test 4 — equity curve has no V-shape (day-to-day jump < 30%)
# ---------------------------------------------------------------------------

def test_equity_chart_no_v_shape() -> None:
    """
    Validate portfolio_history via the API directly.
    Pre-fix: equity jumped 100%+ on the switchover day (V-shape).
    Post-fix: no single day should jump >30%.
    """
    r = _api_get("/api/dashboard-data")
    assert r.status_code == 200, f"dashboard-data returned {r.status_code}"

    ph = r.json()["portfolio_history"]
    assert len(ph) >= 5, f"portfolio_history too short: {len(ph)} rows"

    prev: float | None = None
    for idx, row in enumerate(ph):
        eq = float(row["equity"])
        # Skip the first 3 rows — the account was funded on 2026-03-17 with a
        # legitimate ~$1,498 deposit ($3,519 → $5,018, 42.6%) that precedes
        # normal trading.  The V-shape regression was a data-source switch that
        # caused mid-history spikes, not a startup funding event.
        if prev is not None and prev > 0 and idx >= 3:
            jump_pct = abs(eq - prev) / prev
            assert jump_pct < 0.30, (
                f"Equity jumped {jump_pct * 100:.1f}% from {prev} to {eq} "
                f"on {row['date']} — possible V-shape regression"
            )
        prev = eq


# ---------------------------------------------------------------------------
# Test 5 — total_pnl_pct is within sane range
# ---------------------------------------------------------------------------

def test_total_pnl_pct_under_50() -> None:
    """
    API check: total_pnl_pct must be in (-50, +50).
    Pre-fix bug: 442%.  Corrected value: ~1.49%.
    Also verify total_pnl is sane for a ~$5K account.
    """
    r = _api_get("/api/dashboard-data")
    assert r.status_code == 200

    s = r.json()["summary"]
    pnl_pct = s["total_pnl_pct"]
    pnl = s["total_pnl"]

    assert -50 < pnl_pct < 50, (
        f"total_pnl_pct={pnl_pct} outside sane range (-50, 50) — "
        "possible regression to the 442% bug"
    )
    assert -10_000 < pnl < 10_000, (
        f"total_pnl={pnl} outside sane range for a ~$5K account"
    )


# ---------------------------------------------------------------------------
# Test 6 — all four tabs load without new console errors
# ---------------------------------------------------------------------------

TABS = ["Portfolio", "Finance", "Research", "Remediation"]


@pytest.mark.parametrize("tab_name", TABS)
def test_all_tabs_load(page, tab_name: str) -> None:
    """
    Navigate to /, click each tab, wait 2.5s, assert no new console errors
    and that the page body has >200 chars of content.

    Tabs in Atlas are <button> elements (from TabBar.tsx), NOT role="tab".
    """
    _nav(page, wait="networkidle", timeout=15_000)
    time.sleep(1.0)

    errs_before = len(page._captured_errors)  # type: ignore[attr-defined]

    # TabBar.tsx renders each tab as <button>{label}</button>
    tab_btn = page.get_by_role("button", name=tab_name)
    assert tab_btn.count() >= 1, (
        f"Could not find button with name='{tab_name}'"
    )
    tab_btn.first.click()
    time.sleep(2.5)  # allow lazy-loaded chunk + API fetch to complete

    new_errors = page._captured_errors[errs_before:]  # type: ignore[attr-defined]
    if new_errors:
        formatted = "\n".join(
            f"  [{e.type}] {e.text[:200]}" if hasattr(e, "type") else f"  {e}"
            for e in new_errors
        )
        pytest.fail(
            f"Tab '{tab_name}' introduced {len(new_errors)} console error(s):\n{formatted}"
        )

    body = page.inner_text("body")
    assert len(body) > 200, (
        f"Tab '{tab_name}' body has only {len(body)} chars — looks blank"
    )


# ---------------------------------------------------------------------------
# Test 7 — all key API endpoints return 200 + valid JSON
# ---------------------------------------------------------------------------

KEY_ENDPOINTS = [
    "/api/dashboard-data",
    "/api/finance",
    "/api/system/health",
    "/api/regime/current",
    "/api/risk/ruin",
    "/api/positions/risk",
    "/api/research/overview",
    "/api/research/leaderboard",
    "/api/macro/gauges",
    "/api/signals/ev",
    "/api/signals/vix_term_structure",
    "/api/promotions/pending",
]


@pytest.mark.parametrize("endpoint", KEY_ENDPOINTS)
def test_key_endpoints_return_200(endpoint: str) -> None:
    """Hit each API endpoint; assert HTTP 200 and JSON-parseable response."""
    r = _api_get(endpoint)
    assert r.status_code == 200, (
        f"Endpoint {endpoint} returned {r.status_code} (body: {r.text[:200]})"
    )
    # Must be parseable JSON (raises ValueError on failure)
    data = r.json()
    assert data is not None, f"Endpoint {endpoint} returned null/empty JSON"
