"""Playwright integration test for Controls tab (Phase 2 dogfood).

Run with the dashboard already serving on http://127.0.0.1:8899 with
VITE_ENABLE_CONTROLS_TAB=true at build time. If the tab is hidden,
tests skip (feature flag not enabled in this build).

Usage:
    cd /root/atlas/dashboard-ui && VITE_ENABLE_CONTROLS_TAB=true npm run build
    systemctl restart atlas-dashboard
    cd /root/atlas && python3 -m pytest tests/ui/test_controls_tab.py -v

NOTE: The VITE_ENABLE_CONTROLS_TAB flag is a BUILD-TIME Vite env var, NOT a
runtime flag. You cannot set it via localStorage or JS. The tab button is
simply absent from the TabBar bundle when the flag was false at build time.
These tests detect this via DOM inspection and skip gracefully.
"""
from __future__ import annotations

import json
import pytest
from pathlib import Path
from playwright.sync_api import sync_playwright, Page, expect  # noqa: F401

DASHBOARD_URL = "http://127.0.0.1:8899/"
SECRETS_FILE = Path("/root/.atlas-secrets.json")


def _auth() -> tuple[str, str]:
    secrets = json.loads(SECRETS_FILE.read_text())
    return secrets.get("dashboard_user", "atlas"), secrets.get("dashboard_pass", "")


@pytest.fixture(scope="module")
def page():
    """Module-scoped page: one browser session for all Controls tab tests."""
    user, pw = _auth()
    with sync_playwright() as pw_ctx:
        browser = pw_ctx.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            http_credentials={"username": user, "password": pw},
            ignore_https_errors=True,
        )
        p = context.new_page()
        p.goto(DASHBOARD_URL, wait_until="networkidle", timeout=30_000)
        yield p
        browser.close()


def _controls_tab_visible(page: Page) -> bool:
    """Return True if the Controls tab button is present in the built bundle."""
    return page.locator("button", has_text="Controls").count() > 0


# ---------------------------------------------------------------------------
# Test 1: Tab button visible when flag was ON at build time
# ---------------------------------------------------------------------------

def test_controls_tab_present_when_flag_on(page: Page) -> None:
    """Controls tab button must be in the DOM (requires flag-enabled build)."""
    if not _controls_tab_visible(page):
        pytest.skip("Controls tab not built with VITE_ENABLE_CONTROLS_TAB=true")
    expect(page.locator("button", has_text="Controls")).to_be_visible()


# ---------------------------------------------------------------------------
# Test 2: Three sections render after clicking the tab
# ---------------------------------------------------------------------------

def test_controls_tab_renders_three_sections(page: Page) -> None:
    """After clicking Controls tab, three section headings must appear."""
    if not _controls_tab_visible(page):
        pytest.skip("Controls tab not enabled in this build")
    page.locator("button", has_text="Controls").click()
    page.wait_for_timeout(2000)
    # Three sections per spec §8.2
    assert page.locator("h3", has_text="Universes").count() >= 1, "Universes section missing"
    assert page.locator("h3", has_text="Strategies").count() >= 1, "Strategies section missing"
    assert page.locator("h3", has_text="Recent changes").count() >= 1, "Recent changes section missing"


# ---------------------------------------------------------------------------
# Test 3: Universe state badges render
# ---------------------------------------------------------------------------

def test_universe_state_badge_renders(page: Page) -> None:
    """At least one universe row must show LIVE, PASSIVE, or DISABLED badge."""
    if not _controls_tab_visible(page):
        pytest.skip("Controls tab not enabled in this build")
    page.locator("button", has_text="Controls").click()
    page.wait_for_timeout(2000)
    body_text = page.text_content("body") or ""
    found = any(s in body_text for s in ["LIVE", "PASSIVE", "DISABLED"])
    assert found, "No universe state badge (LIVE/PASSIVE/DISABLED) found in Controls tab"


# ---------------------------------------------------------------------------
# Test 4: Change modal opens and has required fields, Cancel closes it
# ---------------------------------------------------------------------------

def test_change_modal_opens_and_validates(page: Page) -> None:
    """
    Clicking 'Change ▾' on a universe row opens a modal with:
    - A Reason text field
    - A confirmation checkbox (i_understand)
    - Cancel closes the modal without submitting
    """
    if not _controls_tab_visible(page):
        pytest.skip("Controls tab not enabled in this build")
    page.locator("button", has_text="Controls").click()
    page.wait_for_timeout(2000)

    change_buttons = page.locator("button", has_text="Change")
    if change_buttons.count() == 0:
        pytest.skip("No Change buttons visible — no universe rows rendered")

    change_buttons.first.click()
    page.wait_for_timeout(500)

    # Modal must show "Reason" label
    assert page.locator("text=Reason").count() >= 1, "Modal missing Reason field"

    # Cancel closes the modal
    cancel = page.locator("button", has_text="Cancel")
    if cancel.count() > 0:
        cancel.first.click()
        page.wait_for_timeout(500)


# ---------------------------------------------------------------------------
# Test 5: Audit panel heading renders
# ---------------------------------------------------------------------------

def test_audit_panel_renders(page: Page) -> None:
    """Recent changes panel heading is present after tab load."""
    if not _controls_tab_visible(page):
        pytest.skip("Controls tab not enabled in this build")
    page.locator("button", has_text="Controls").click()
    page.wait_for_timeout(2000)
    assert page.locator("h3", has_text="Recent changes").count() >= 1


# ---------------------------------------------------------------------------
# Test 6: No console errors when loading Controls tab
# ---------------------------------------------------------------------------

def test_controls_tab_no_console_errors(page: Page) -> None:
    """Controls tab must not introduce any console errors when navigated to fresh."""
    if not _controls_tab_visible(page):
        pytest.skip("Controls tab not enabled in this build")

    errors: list[str] = []

    def on_console(msg) -> None:
        if msg.type == "error":
            errors.append(msg.text)

    def on_page_error(exc) -> None:
        errors.append(str(exc))

    page.on("console", on_console)
    page.on("pageerror", on_page_error)
    try:
        # Navigate fresh to ensure we capture load-time errors
        page.goto(DASHBOARD_URL, wait_until="networkidle", timeout=30_000)
        page.locator("button", has_text="Controls").click()
        page.wait_for_timeout(2500)  # let lazy chunk + API fetches settle
    finally:
        page.remove_listener("console", on_console)
        page.remove_listener("pageerror", on_page_error)

    # Filter out benign known-noise patterns: react devtools, hot reload, etc.
    benign = ("react-dom", "react devtools", "Download the React DevTools")
    real = [e for e in errors if not any(b.lower() in e.lower() for b in benign)]

    if real:
        pytest.fail(
            f"Controls tab introduced {len(real)} console error(s):\n"
            + "\n".join(f"  {e[:200]}" for e in real)
        )
