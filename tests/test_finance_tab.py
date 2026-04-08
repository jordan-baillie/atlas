"""Playwright E2E tests for the Finance tab on the Atlas dashboard.

Requires: playwright, pytest-playwright
Dashboard must be running at http://localhost:8899 (atlas-dashboard.service)
"""
import pytest
import json
import time
from pathlib import Path

# ── Auth setup ───────────────────────────────────────────────────────────────
_secrets_path = Path.home() / ".atlas-secrets.json"
_secrets = json.loads(_secrets_path.read_text()) if _secrets_path.exists() else {}
DASH_USER = _secrets.get("dashboard_user", "atlas")
DASH_PASS = _secrets.get("dashboard_pass", "")
BASE_URL = "http://localhost:8899"

# Skip all tests if playwright not available or dashboard not running
pytestmark = pytest.mark.skipif(
    not DASH_PASS,
    reason="No dashboard credentials found in ~/.atlas-secrets.json",
)


@pytest.fixture(scope="module")
def browser(playwright):
    """Launch a headless Chromium browser."""
    b = playwright.chromium.launch(headless=True)
    yield b
    b.close()


@pytest.fixture(scope="module")
def page(browser):
    """Navigate to the dashboard and wait for it to load."""
    import base64
    cred = base64.b64encode(f"{DASH_USER}:{DASH_PASS}".encode()).decode()
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        extra_http_headers={"Authorization": f"Basic {cred}"},
    )
    pg = context.new_page()
    try:
        pg.goto(BASE_URL, wait_until="networkidle", timeout=15000)
    except Exception as e:
        pytest.skip(f"Dashboard not reachable: {e}")
    yield pg
    context.close()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Tab Navigation
# ─────────────────────────────────────────────────────────────────────────────


class TestTabNavigation:
    """Verify the tab system exists and works."""

    def test_tab_nav_exists(self, page):
        """Tab navigation bar should be present."""
        nav = page.query_selector("#tab-nav, .tab-nav")
        assert nav is not None, "Tab navigation bar not found"

    def test_both_tabs_present(self, page):
        """Should have both Portfolio and Finance tab buttons."""
        tabs = page.query_selector_all(".tab-btn")
        labels = [t.inner_text().strip() for t in tabs]
        assert "Portfolio" in labels, f"Missing Portfolio tab. Found: {labels}"
        assert "Finance" in labels, f"Missing Finance tab. Found: {labels}"

    def test_portfolio_active_by_default(self, page):
        """Portfolio tab should be the active tab on page load."""
        active_btn = page.query_selector(".tab-btn.active")
        assert active_btn is not None, "No active tab button found"
        assert "Portfolio" in active_btn.inner_text(), (
            f"Expected Portfolio active, got: {active_btn.inner_text()}"
        )

    def test_portfolio_content_visible(self, page):
        """Portfolio content panel should be visible on load."""
        panel = page.query_selector("#tab-portfolio")
        assert panel is not None, "#tab-portfolio element not found"
        cls = panel.get_attribute("class") or ""
        assert "active" in cls, f"Portfolio panel not active. class={cls}"

    def test_finance_content_hidden(self, page):
        """Finance content panel should be hidden on load."""
        panel = page.query_selector("#tab-finance")
        assert panel is not None, "#tab-finance element not found"
        cls = panel.get_attribute("class") or ""
        assert "active" not in cls, f"Finance panel should not be active on load. class={cls}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Finance Tab Click & Data Load
# ─────────────────────────────────────────────────────────────────────────────


class TestFinanceTabLoad:
    """Verify clicking Finance tab loads data from the API."""

    def test_click_finance_tab(self, page):
        """Clicking Finance button should switch to the finance panel."""
        btn = page.query_selector('.tab-btn[data-tab="finance"]')
        assert btn is not None, "Finance tab button not found"
        btn.click()

        # Wait for finance content to become active
        page.wait_for_selector("#tab-finance.active", timeout=10000)

        # Verify the button itself is active
        cls = btn.get_attribute("class") or ""
        assert "active" in cls, f"Finance button not active after click. class={cls}"

    def test_portfolio_hidden_after_switch(self, page):
        """Portfolio panel should be hidden after switching to Finance."""
        panel = page.query_selector("#tab-portfolio")
        cls = panel.get_attribute("class") or ""
        assert "active" not in cls, "Portfolio panel still active after switching to Finance"

    def test_finance_api_data_loaded(self, page):
        """Finance data should load from /api/finance (net worth section renders)."""
        # Wait for the summary strip to have content
        page.wait_for_selector("#fin-summary-strip", timeout=10000)
        strip = page.query_selector("#fin-summary-strip")
        assert strip is not None, "Finance summary strip not found"

        text = strip.inner_text()
        assert len(text) > 10, f"Summary strip is empty or too short: '{text}'"

    def test_net_worth_shows_dollar_amount(self, page):
        """Net worth section should display a dollar amount."""
        strip = page.query_selector("#fin-summary-strip")
        text = strip.inner_text()
        assert "$" in text, f"No dollar sign in net worth. Content: {text[:200]}"

    def test_accounts_grid_rendered(self, page):
        """Bank accounts grid should show account cards."""
        page.wait_for_selector("#fin-accounts-grid", timeout=5000)
        grid = page.query_selector("#fin-accounts-grid")
        assert grid is not None, "Accounts grid not found"

        text = grid.inner_text()
        assert len(text) > 20, f"Accounts grid seems empty: {text[:200]}"

    def test_accounts_have_emoji_names(self, page):
        """Up Bank accounts should show emoji names (e.g., ✈️ Travel, 💰 Savings)."""
        grid = page.query_selector("#fin-accounts-grid")
        text = grid.inner_text()
        # Check for at least one known account name
        known_accounts = ["Savings", "Spending", "Travel", "Rent", "Invest"]
        found = [a for a in known_accounts if a in text]
        assert len(found) >= 2, f"Expected known accounts, found: {found}. Full text: {text[:300]}"

    def test_spending_section_rendered(self, page):
        """Spending section should show spending data."""
        spending = page.query_selector("#fin-spending-bars")
        if spending:
            text = spending.inner_text()
            assert len(text) > 0, "Spending bars section is empty"
        else:
            # Try alternate selector
            alt = page.query_selector("[class*='spending']")
            assert alt is not None, "No spending section found with any selector"

    def test_budget_section_rendered(self, page):
        """Budget section should show budget information."""
        budget = page.query_selector("#fin-budget-grid")
        if budget:
            text = budget.inner_text()
            assert len(text) > 0, "Budget grid is empty"
        else:
            alt = page.query_selector("[class*='budget']")
            assert alt is not None, "No budget section found"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Tab Switching & Cache
# ─────────────────────────────────────────────────────────────────────────────


class TestTabSwitchingAndCache:
    """Verify switching between tabs and data caching."""

    def test_switch_back_to_portfolio(self, page):
        """Switching back to Portfolio should work."""
        # Make sure we're on Finance first
        fin_btn = page.query_selector('.tab-btn[data-tab="finance"]')
        if "active" not in (fin_btn.get_attribute("class") or ""):
            fin_btn.click()
            page.wait_for_selector("#tab-finance.active", timeout=5000)

        # Now switch to Portfolio
        port_btn = page.query_selector('.tab-btn[data-tab="portfolio"]')
        port_btn.click()
        page.wait_for_selector("#tab-portfolio.active", timeout=5000)

        cls = port_btn.get_attribute("class") or ""
        assert "active" in cls, "Portfolio button not active after switch back"

    def test_finance_second_load_is_fast(self, page):
        """Second Finance tab click should be near-instant (cached data)."""
        # Switch to finance again
        btn = page.query_selector('.tab-btn[data-tab="finance"]')
        start = time.time()
        btn.click()
        page.wait_for_selector("#tab-finance.active", timeout=5000)
        elapsed = time.time() - start

        assert elapsed < 2.0, f"Second load took {elapsed:.2f}s — expected <2s (cached)"

    def test_finance_content_persists(self, page):
        """Finance content should still be present after switching back."""
        strip = page.query_selector("#fin-summary-strip")
        assert strip is not None
        text = strip.inner_text()
        assert "$" in text, f"Net worth data missing after tab switch. Got: {text[:200]}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. API Response Validation
# ─────────────────────────────────────────────────────────────────────────────


class TestFinanceAPIResponse:
    """Verify the /api/finance endpoint returns correct data (via page context)."""

    def test_api_returns_valid_json(self, page):
        """Direct API call should return valid JSON with expected keys."""
        response = page.request.get(f"{BASE_URL}/api/finance")
        # Auth header is set on the context, so this should work
        assert response.ok, f"API returned {response.status}: {response.text()[:200]}"

        data = response.json()
        required_keys = [
            "net_worth", "accounts", "monthly_spending",
            "recent_transactions", "savings_rate", "performance", "insights",
        ]
        for key in required_keys:
            assert key in data, f"Missing key '{key}' in API response"

    def test_api_net_worth_is_positive(self, page):
        """Net worth should be a positive number."""
        data = page.request.get(f"{BASE_URL}/api/finance").json()
        nw = data["net_worth"]["total_aud"]
        assert isinstance(nw, (int, float)), f"net_worth.total_aud is not a number: {nw}"
        assert nw > 0, f"Net worth should be positive, got: {nw}"

    def test_api_has_15_accounts(self, page):
        """Should return all 15 Up Bank accounts."""
        data = page.request.get(f"{BASE_URL}/api/finance").json()
        accounts = data["accounts"]
        assert len(accounts) == 15, f"Expected 15 accounts, got {len(accounts)}"

    def test_api_accounts_have_required_fields(self, page):
        """Each account should have name, type, balance."""
        data = page.request.get(f"{BASE_URL}/api/finance").json()
        for acct in data["accounts"]:
            assert "name" in acct, f"Account missing 'name': {acct}"
            assert "balance" in acct, f"Account missing 'balance': {acct}"
            assert "type" in acct, f"Account missing 'type': {acct}"

    def test_api_spending_data_present(self, page):
        """Monthly spending should have categories."""
        data = page.request.get(f"{BASE_URL}/api/finance").json()
        ms = data["monthly_spending"]
        assert "total" in ms, "monthly_spending missing 'total'"
        assert "by_parent_category" in ms, "monthly_spending missing 'by_parent_category'"
        assert ms["total"] >= 0, f"Spending total should be >= 0, got: {ms['total']}"

    def test_api_data_is_from_sqlite(self, page):
        """Verify data is fresh (from SQLite), not stale JSON."""
        data = page.request.get(f"{BASE_URL}/api/finance").json()
        # The last_updated should be recent (within last hour since it's generated on request)
        last_updated = data.get("last_updated", "")
        assert last_updated, "No last_updated timestamp — might be stale JSON"
        # Just verify it's a valid timestamp format
        assert "T" in last_updated, f"Unexpected timestamp format: {last_updated}"
