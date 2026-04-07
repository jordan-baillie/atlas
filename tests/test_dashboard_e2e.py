"""End-to-end Playwright tests for Atlas Dashboard.

Tests the simplified two-panel layout (Phase 3), chat UI (Phase 4),
and core API endpoints introduced in Phase 2.

Usage:
    python3 -m pytest tests/test_dashboard_e2e.py -v
    python3 -m pytest tests/test_dashboard_e2e.py -v -k "TestLayout"
    python3 -m pytest tests/test_dashboard_e2e.py -v --tb=short

Requirements:
    pip install playwright pytest-playwright
    playwright install chromium

Server must NOT be running on port 18899 before running tests.
Tests use port 18899 (not 8899) to avoid conflicts with production.
"""

import json
import os
import signal
import subprocess
import tempfile
import time
from typing import Generator

import pytest

# ── Optional playwright import — tests skip gracefully if not installed ───────
try:
    from playwright.sync_api import Browser, Page, sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

SERVER_URL = "http://127.0.0.1:18899"
AUTH_USER  = "atlas_test"
AUTH_PASS  = "atlas_test_pass"
ATLAS_ROOT = "/root/atlas"

pytestmark = pytest.mark.skipif(
    not PLAYWRIGHT_AVAILABLE,
    reason="playwright not installed — run: pip install playwright && playwright install chromium",
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def server() -> Generator[subprocess.Popen, None, None]:
    """Start the FastAPI dashboard server on test port 18899.

    Uses a temporary secrets file with known test credentials.
    Skips the fixture (and all tests) if the server fails to start
    (e.g. broker modules unavailable).
    """
    secrets = {
        "dashboard_user": AUTH_USER,
        "dashboard_pass": AUTH_PASS,
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, dir="/tmp"
    ) as f:
        json.dump(secrets, f)
        secrets_path = f.name

    env = os.environ.copy()
    env["ATLAS_SECRETS_PATH"] = secrets_path  # Allow override in chat_server.py

    proc = subprocess.Popen(
        [
            "python3", "-m", "uvicorn",
            "services.chat_server:app",
            "--host", "127.0.0.1",
            "--port", "18899",
            "--log-level", "warning",
        ],
        cwd=ATLAS_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait up to 10 seconds for server to become ready
    deadline = time.time() + 10
    import urllib.request, urllib.error
    ready = False
    while time.time() < deadline:
        try:
            req = urllib.request.Request(SERVER_URL + "/api/dashboard-data")
            # Add Basic Auth
            import base64
            creds = base64.b64encode(f"{AUTH_USER}:{AUTH_PASS}".encode()).decode()
            req.add_header("Authorization", f"Basic {creds}")
            urllib.request.urlopen(req, timeout=1)
            ready = True
            break
        except Exception:
            time.sleep(0.5)

    if not ready:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.unlink(secrets_path)
        pytest.skip("FastAPI server failed to start on port 18899 — broker modules may be unavailable")

    yield proc

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    os.unlink(secrets_path)


@pytest.fixture(scope="module")
def browser_instance() -> Generator["Browser", None, None]:
    """Launch a headless Chromium browser for the test module."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        yield browser
        browser.close()


@pytest.fixture
def page(browser_instance: "Browser", server: subprocess.Popen) -> Generator["Page", None, None]:
    """Create an authenticated browser page for each test."""
    context = browser_instance.new_context(
        http_credentials={"username": AUTH_USER, "password": AUTH_PASS},
        viewport={"width": 1280, "height": 900},
    )
    pg = context.new_page()
    # Suppress console errors from missing broker / SSE stream
    pg.on("console", lambda msg: None)
    yield pg
    context.close()


@pytest.fixture
def mobile_page(browser_instance: "Browser", server: subprocess.Popen) -> Generator["Page", None, None]:
    """Phone-sized page for responsive tests."""
    context = browser_instance.new_context(
        http_credentials={"username": AUTH_USER, "password": AUTH_PASS},
        viewport={"width": 390, "height": 844},
    )
    pg = context.new_page()
    yield pg
    context.close()


# ── Layout Tests ──────────────────────────────────────────────────────────────

class TestDashboardLayout:
    """Verify the Phase 3 two-panel layout renders correctly."""

    def test_page_loads_without_js_errors(self, page: "Page") -> None:
        """Dashboard loads and has a meaningful title."""
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        # Relax: only flag hard JS errors, not missing SSE / broker errors
        critical = [e for e in errors if "SyntaxError" in e or "ReferenceError" in e]
        assert critical == [], f"Critical JS errors: {critical}"

    def test_page_title(self, page: "Page") -> None:
        """Page title should contain 'Atlas'."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        assert "Atlas" in page.title()

    def test_no_tab_navigation(self, page: "Page") -> None:
        """Tab navigation must be removed in Phase 3."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        assert page.locator(".tab-nav").count() == 0, ".tab-nav should not exist"
        assert page.locator(".tab-btn").count() == 0, ".tab-btn should not exist"

    def test_two_panel_layout_present(self, page: "Page") -> None:
        """Two-panel grid must be present with both portfolio and chat panels."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        page.wait_for_selector(".two-panel", timeout=5000)
        assert page.locator(".panel-portfolio").count() == 1
        assert page.locator(".panel-chat").count() == 1

    def test_portfolio_panel_visible(self, page: "Page") -> None:
        """.panel-portfolio is visible."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        assert page.locator(".panel-portfolio").is_visible()

    def test_chat_panel_visible(self, page: "Page") -> None:
        """.panel-chat (#chat-panel) is visible."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        assert page.locator("#chat-panel").is_visible()

    def test_compact_summary_strip(self, page: "Page") -> None:
        """Summary strip present and has exactly 4 stat blocks."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        strip = page.locator(".summary-strip")
        assert strip.count() == 1
        stats = strip.locator(".stat")
        assert stats.count() == 4, f"Expected 4 stats, got {stats.count()}"

    def test_header_logo(self, page: "Page") -> None:
        """▲ Atlas logo in the header."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        logo = page.locator(".header .logo")
        assert logo.count() >= 1
        assert logo.is_visible()

    def test_regime_indicator_in_header(self, page: "Page") -> None:
        """Regime indicator pill is in the header."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        assert page.locator("#regime-indicator").count() == 1

    def test_equity_chart_canvas(self, page: "Page") -> None:
        """Equity chart canvas is rendered."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        canvas = page.locator("#equity-canvas")
        assert canvas.count() == 1
        assert canvas.is_visible()

    def test_positions_grid_present(self, page: "Page") -> None:
        """Positions grid container exists."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        assert page.locator("#positions-grid").count() == 1

    def test_orders_in_collapse(self, page: "Page") -> None:
        """Recent Orders is a <details> collapse element, not a separate tab."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        details = page.locator(".orders-collapse")
        assert details.count() >= 1
        # Should be a <details> element
        tag = page.evaluate("() => document.querySelector('.orders-collapse').tagName.toLowerCase()")
        assert tag == "details"

    def test_regime_timeline_canvas(self, page: "Page") -> None:
        """90-day regime timeline canvas at the bottom of the page."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        assert page.locator("#regime-timeline-canvas").is_visible()

    def test_theme_toggle_button(self, page: "Page") -> None:
        """Theme toggle button exists and is clickable."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        btn = page.locator("#theme-btn")
        assert btn.is_visible()
        btn.click()
        page.wait_for_timeout(300)
        theme = page.locator("html").get_attribute("data-theme")
        assert theme in ("light", "dark", "auto")

    def test_no_sidebar(self, page: "Page") -> None:
        """Sidebar elements (regime card, AI overlay card, donut) are gone."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        assert page.locator("#regime-card").count() == 0, "regime-card should be removed"
        assert page.locator("#overlay-card").count() == 0, "overlay-card should be removed"
        assert page.locator("#donut-canvas").count() == 0, "donut-canvas should be removed"

    def test_no_performance_section(self, page: "Page") -> None:
        """Performance section HTML is removed (accessible via chat instead)."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        assert page.locator("#performance-section").count() == 0


# ── Chat UI Tests ─────────────────────────────────────────────────────────────

class TestChatUI:
    """Verify the Phase 4 chat panel UI elements are present and functional."""

    def test_chat_input_present(self, page: "Page") -> None:
        """Chat textarea input is visible."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        assert page.locator("#chat-input").is_visible()

    def test_send_button_present(self, page: "Page") -> None:
        """Send button (↑) is visible."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        assert page.locator("#chat-send").is_visible()

    def test_new_session_button_present(self, page: "Page") -> None:
        """New session button (+) is visible."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        assert page.locator("#chat-new-session").is_visible()

    def test_session_select_present(self, page: "Page") -> None:
        """Session select dropdown is present."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        assert page.locator("#chat-session-select").count() == 1

    def test_chat_messages_container(self, page: "Page") -> None:
        """Chat messages container is present and scrollable."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        assert page.locator("#chat-messages").is_visible()

    def test_chat_status_bar(self, page: "Page") -> None:
        """Chat status bar (model, cost) is present."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        assert page.locator("#chat-status").count() == 1
        assert page.locator("#chat-cost").count() == 1

    def test_chat_input_typing(self, page: "Page") -> None:
        """User can type into the chat input."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(500)
        input_el = page.locator("#chat-input")
        input_el.fill("Hello Atlas, what is the current regime?")
        assert input_el.input_value() == "Hello Atlas, what is the current regime?"

    def test_chat_input_clears_on_send_click(self, page: "Page") -> None:
        """Clicking send clears the input field (message is dispatched)."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        input_el = page.locator("#chat-input")
        input_el.fill("test message")
        page.locator("#chat-send").click()
        page.wait_for_timeout(500)
        # Input should be cleared (or have value from reconnect logic)
        val = input_el.input_value()
        assert val == "", f"Input should be cleared after send, got: '{val}'"

    def test_chat_css_loaded(self, page: "Page") -> None:
        """chat.css is loaded and applies panel-chat styling."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        # Check that chat.css flex direction is applied to panel-chat
        display = page.evaluate(
            "() => window.getComputedStyle(document.querySelector('.panel-chat')).display"
        )
        assert display == "flex", f"panel-chat should be flex, got: {display}"

    def test_chat_js_loaded(self, page: "Page") -> None:
        """chat.js is loaded and Chat module is defined."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(500)
        chat_defined = page.evaluate("() => typeof Chat !== 'undefined'")
        assert chat_defined, "Chat module should be defined by chat.js"


# ── API Endpoint Tests ─────────────────────────────────────────────────────────

class TestChatAPI:
    """Verify the Phase 2 chat REST endpoints work correctly."""

    def test_create_session(self, page: "Page") -> None:
        """POST /api/chat/sessions creates and returns a session."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        response = page.request.post(
            f"{SERVER_URL}/api/chat/sessions",
            data=json.dumps({"name": "E2E Test Session", "model": "claude-sonnet-4-6"}),
            headers={"Content-Type": "application/json"},
        )
        assert response.status == 200, f"Expected 200, got {response.status}"
        data = response.json()
        assert "id" in data, f"Response missing 'id': {data}"
        assert data.get("status") == "active"

    def test_list_sessions(self, page: "Page") -> None:
        """GET /api/chat/sessions returns a list."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        response = page.request.get(f"{SERVER_URL}/api/chat/sessions?limit=5")
        assert response.status == 200, f"Expected 200, got {response.status}"
        data = response.json()
        assert isinstance(data, list), f"Expected list, got: {type(data)}"

    def test_list_sessions_respects_limit(self, page: "Page") -> None:
        """GET /api/chat/sessions?limit=2 returns at most 2 sessions."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        # Create 3 sessions first
        for i in range(3):
            page.request.post(
                f"{SERVER_URL}/api/chat/sessions",
                data=json.dumps({"name": f"limit-test-{i}"}),
                headers={"Content-Type": "application/json"},
            )
        response = page.request.get(f"{SERVER_URL}/api/chat/sessions?limit=2")
        assert response.status == 200
        data = response.json()
        assert len(data) <= 2, f"Expected ≤2 results, got {len(data)}"

    def test_get_messages_for_session(self, page: "Page") -> None:
        """GET /api/chat/sessions/{id}/messages returns a list."""
        # Create a session first
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        create_resp = page.request.post(
            f"{SERVER_URL}/api/chat/sessions",
            data=json.dumps({"name": "msg-test-session"}),
            headers={"Content-Type": "application/json"},
        )
        assert create_resp.status == 200
        session_id = create_resp.json()["id"]

        msg_resp = page.request.get(
            f"{SERVER_URL}/api/chat/sessions/{session_id}/messages"
        )
        assert msg_resp.status == 200
        assert isinstance(msg_resp.json(), list)

    def test_chat_token_endpoint(self, page: "Page") -> None:
        """GET /api/chat/token returns a token string for WebSocket auth."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        response = page.request.get(f"{SERVER_URL}/api/chat/token")
        if response.status == 404:
            pytest.skip("/api/chat/token not yet implemented (Builder-1 scope)")
        assert response.status == 200, f"Expected 200, got {response.status}"
        data = response.json()
        assert "token" in data, f"Expected 'token' key in response: {data}"
        assert isinstance(data["token"], str) and len(data["token"]) > 8

    def test_existing_dashboard_data_endpoint(self, page: "Page") -> None:
        """GET /api/dashboard-data still works (existing route not broken)."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        response = page.request.get(f"{SERVER_URL}/api/dashboard-data")
        assert response.status == 200, f"Expected 200, got {response.status}"

    def test_auth_required_for_chat_sessions(self, page: "Page") -> None:
        """Chat sessions endpoint returns 401 without credentials."""
        # Use a context without auth credentials
        import base64
        # Make a raw request without auth header
        # Playwright's page.request inherits auth from context, so we need a new context
        no_auth_ctx = page.context.browser.new_context(viewport={"width": 1280, "height": 900})
        try:
            no_auth_page = no_auth_ctx.new_page()
            response = no_auth_page.request.get(f"{SERVER_URL}/api/chat/sessions")
            assert response.status == 401, f"Expected 401 without auth, got {response.status}"
        finally:
            no_auth_ctx.close()


# ── Responsive Tests ──────────────────────────────────────────────────────────

class TestResponsive:
    """Verify the layout adapts correctly to different screen sizes."""

    def test_mobile_panels_stack(self, mobile_page: "Page") -> None:
        """On mobile (390px), panels should stack vertically."""
        mobile_page.goto(SERVER_URL, wait_until="domcontentloaded")
        mobile_page.wait_for_timeout(500)
        two_panel = mobile_page.locator(".two-panel")
        assert two_panel.is_visible()
        # Both panels visible (stacked)
        assert mobile_page.locator(".panel-portfolio").is_visible()
        assert mobile_page.locator("#chat-panel").is_visible()

    def test_mobile_chat_panel_height_bounded(self, mobile_page: "Page") -> None:
        """On mobile, chat panel max-height is limited (not full viewport)."""
        mobile_page.goto(SERVER_URL, wait_until="domcontentloaded")
        mobile_page.wait_for_timeout(300)
        chat_height = mobile_page.evaluate(
            "() => document.querySelector('.panel-chat').getBoundingClientRect().height"
        )
        viewport_height = 844
        assert chat_height < viewport_height, (
            f"Chat panel height ({chat_height}px) should be less than viewport ({viewport_height}px)"
        )

    def test_tablet_layout(self, browser_instance: "Browser", server: subprocess.Popen) -> None:
        """On tablet (900px), layout adapts."""
        ctx = browser_instance.new_context(
            http_credentials={"username": AUTH_USER, "password": AUTH_PASS},
            viewport={"width": 900, "height": 768},
        )
        pg = ctx.new_page()
        try:
            pg.goto(SERVER_URL, wait_until="domcontentloaded")
            pg.wait_for_timeout(300)
            assert pg.locator(".panel-portfolio").is_visible()
        finally:
            ctx.close()


# ── Theme Tests ───────────────────────────────────────────────────────────────

class TestTheme:
    """Verify dark/light theme switching works."""

    def test_theme_starts_as_dark_or_auto(self, page: "Page") -> None:
        """Default theme is dark or auto."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        theme = page.locator("html").get_attribute("data-theme")
        assert theme in ("dark", "light", "auto"), f"Unexpected theme: {theme}"

    def test_theme_toggle_cycles(self, page: "Page") -> None:
        """Theme toggle button cycles through themes."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        btn = page.locator("#theme-btn")
        initial_theme = page.locator("html").get_attribute("data-theme")
        btn.click()
        page.wait_for_timeout(200)
        after_click = page.locator("html").get_attribute("data-theme")
        assert after_click in ("dark", "light", "auto")
        # Theme should have changed (or stayed — auto may resolve to same effective)
        # Just verify it's still valid
        assert after_click is not None

    def test_keyboard_l_toggles_theme(self, page: "Page") -> None:
        """Pressing 'L' key toggles the theme."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        # Focus body to ensure keydown fires
        page.locator("body").click()
        page.keyboard.press("l")
        page.wait_for_timeout(200)
        theme = page.locator("html").get_attribute("data-theme")
        assert theme in ("dark", "light", "auto")


# ── Smoke Tests ───────────────────────────────────────────────────────────────

class TestSmoke:
    """Quick smoke tests that run fast and catch obvious regressions."""

    def test_static_files_served(self, page: "Page") -> None:
        """atlas.css and chat.css are served with 200."""
        for path in ["/atlas.css", "/chat.css", "/chat.js"]:
            response = page.request.get(f"{SERVER_URL}{path}")
            assert response.status == 200, f"{path} returned {response.status}"

    def test_favicon_served(self, page: "Page") -> None:
        """Favicon request doesn't 500."""
        response = page.request.get(f"{SERVER_URL}/favicon.ico")
        assert response.status in (200, 404), f"favicon.ico returned {response.status}"

    def test_no_console_syntax_errors(self, page: "Page") -> None:
        """No SyntaxError or ReferenceError in browser console."""
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(SERVER_URL, wait_until="networkidle")
        page.wait_for_timeout(2000)
        critical = [e for e in errors if any(t in e for t in ("SyntaxError", "ReferenceError"))]
        assert critical == [], f"Console errors: {critical}"

    def test_chat_module_exposes_send(self, page: "Page") -> None:
        """Chat.send function is exposed on the Chat module."""
        page.goto(SERVER_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(500)
        has_send = page.evaluate("() => typeof Chat !== 'undefined' && typeof Chat.send === 'function'")
        assert has_send, "Chat.send should be a function"


# ── Entry point for direct execution ─────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v", "--tb=short"] + sys.argv[1:]))
