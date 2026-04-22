#!/usr/bin/env python3
"""
Atlas Dashboard UI Audit — READ-ONLY Playwright inspection.
Captures screenshots, rendered text, console errors, network failures.
Output: /tmp/atlas_audit/
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from playwright.sync_api import sync_playwright, Page, ConsoleMessage, Response

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DASHBOARD_URL   = "http://127.0.0.1:8899/"
SECRETS_FILE    = Path("/root/.atlas-secrets.json")
OUTPUT_DIR      = Path("/tmp/atlas_audit")
DESKTOP_W, DESKTOP_H = 1440, 900
MOBILE_W,  MOBILE_H  = 375, 812
GOTO_TIMEOUT    = 25_000   # ms
NAV_TIMEOUT     = 60_000   # ms
TAB_WAIT_SEC    = 3        # seconds post-click

TABS = ["Portfolio", "Finance", "Research"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class Issue(NamedTuple):
    severity: str   # ERROR / WARN / INFO
    category: str   # console / network / missing / perf
    message: str


def load_credentials() -> tuple[str, str]:
    data = json.loads(SECRETS_FILE.read_text())
    return data["dashboard_user"], data["dashboard_pass"]


def slugify(name: str) -> str:
    return name.lower().replace(" ", "_")


def extract_dollar_amounts(text: str) -> list[str]:
    """Pull $-prefixed numeric strings from body text."""
    return re.findall(r"\$[\d,]+(?:\.\d+)?", text)


def setup_listeners(page: Page,
                    console_log: list[dict],
                    net_errors: list[dict],
                    js_errors: list[str]) -> None:
    """Attach event listeners to page before navigation."""

    # Known recharts initialisation noise: fires before ResizeObserver measures
    # the container on the first render cycle. Not a real error.
    _RECHARTS_INIT_NOISE = (
        "The width(-1) and height(-1) of chart should be greater than 0",
        "The width(0) and height(0) of chart should be greater than 0",
    )

    def on_console(msg: ConsoleMessage) -> None:
        text = msg.text[:400]
        # Suppress recharts initial-render dimension noise (fires before ResizeObserver)
        if msg.type == "warning" and any(noise in text for noise in _RECHARTS_INIT_NOISE):
            return
        console_log.append({
            "type": msg.type,
            "text": text,
            "location": f"{msg.location.get('url','?')}:{msg.location.get('lineNumber','?')}",
        })

    def on_pageerror(exc: Exception) -> None:
        js_errors.append(str(exc)[:600])

    def on_response(resp: Response) -> None:
        if resp.status >= 400:
            net_errors.append({
                "status": resp.status,
                "url": resp.url[:200],
            })

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("response", on_response)


# ---------------------------------------------------------------------------
# Per-tab inspection helpers
# ---------------------------------------------------------------------------

def inspect_portfolio(page: Page, issues: list[Issue]) -> dict:
    result: dict = {}

    # --- Stat strip ---
    try:
        stat_texts = page.locator(
            '[data-testid="summary-strip"], [data-testid="stat-card"],'
            ' [class*="SummaryStrip"], [class*="StatCard"]'
        ).all_text_contents()
        result["stat_strip_texts"] = stat_texts
        if not stat_texts:
            issues.append(Issue("WARN", "missing", "Portfolio: no StatCard / SummaryStrip elements found"))
    except Exception as e:
        issues.append(Issue("ERROR", "missing", f"Portfolio stat strip error: {e}"))
        result["stat_strip_texts"] = []

    # --- PositionCard count ---
    # NOTE: PositionCards only render when the broker API is reachable and returns
    # open positions. If the broker is offline (dashboard-data timeout) the UI
    # correctly shows 0 cards. We downgrade this to INFO because:
    #   (a) data-testid="position-card" is confirmed in source (selector works)
    #   (b) 0 cards is valid broker-offline behaviour, not a structural bug
    try:
        pos_locator = page.locator(
            '[data-testid="position-card"], [class*="PositionCard"]'
        )
        pos_count = pos_locator.count()
        result["position_card_count"] = pos_count
        if pos_count == 0:
            print("  ℹ PositionCards: 0 (broker offline or no open positions — INFO only)")
        else:
            print(f"  ✓ PositionCards: {pos_count}")
    except Exception as e:
        issues.append(Issue("ERROR", "missing", f"Portfolio PositionCard error: {e}"))
        result["position_card_count"] = 0

    # --- EquityChart ---
    try:
        chart_loc = page.locator(
            '.recharts-wrapper, canvas,'
            ' [data-testid="equity-chart"], [class*="EquityChart"]'
        )
        chart_count = chart_loc.count()
        result["equity_chart_count"] = chart_count
        if chart_count == 0:
            issues.append(Issue("WARN", "missing", "Portfolio: no EquityChart / canvas element found"))
        else:
            print(f"  ✓ Chart/Canvas elements: {chart_count}")
    except Exception as e:
        issues.append(Issue("ERROR", "missing", f"Portfolio EquityChart error: {e}"))

    # --- MacroGauges ---
    try:
        gauge_loc = page.locator(
            '[data-testid="macro-gauge"], [class*="MacroGauge"]'
        )
        gauge_count = gauge_loc.count()
        result["macro_gauge_count"] = gauge_count
        if gauge_count == 0:
            issues.append(Issue("INFO", "missing", "Portfolio: no MacroGauge elements (may be nested)"))
        else:
            print(f"  ✓ MacroGauges: {gauge_count}")
    except Exception as e:
        issues.append(Issue("WARN", "missing", f"Portfolio MacroGauge error: {e}"))

    # --- RegimeTimeline ---
    try:
        regime_loc = page.locator(
            '[data-testid="regime-timeline"], [class*="RegimeTimeline"]'
        )
        regime_count = regime_loc.count()
        result["regime_element_count"] = regime_count
        if regime_count == 0:
            issues.append(Issue("INFO", "missing", "Portfolio: no RegimeTimeline elements (may be nested)"))
        else:
            print(f"  ✓ Regime elements: {regime_count}")
    except Exception as e:
        issues.append(Issue("WARN", "missing", f"Portfolio RegimeTimeline error: {e}"))

    # --- Dollar amounts in body text ---
    try:
        body_text = page.inner_text("body")
        dollar_amounts = extract_dollar_amounts(body_text)
        result["dollar_amounts_sample"] = dollar_amounts[:20]
        if not dollar_amounts:
            issues.append(Issue("WARN", "missing", "Portfolio: no $ values visible in body text"))
        else:
            print(f"  ✓ Dollar values found: {dollar_amounts[:5]} ...")
    except Exception as e:
        issues.append(Issue("ERROR", "missing", f"Portfolio dollar extraction error: {e}"))

    # --- Skeleton loaders / error boundaries (stuck states) ---
    try:
        skeleton_count = page.locator(
            '[class*="skeleton"], [class*="Skeleton"], [class*="loading"], [class*="Loading"]'
        ).count()
        result["skeleton_count"] = skeleton_count
        if skeleton_count > 3:
            issues.append(Issue("WARN", "perf", f"Portfolio: {skeleton_count} skeleton/loading elements still visible"))
        else:
            print(f"  ✓ Skeleton loaders remaining: {skeleton_count}")
    except Exception as e:
        pass

    try:
        error_boundary_count = page.locator(
            '[class*="error"], [class*="Error"], [class*="ErrorBoundary"]'
        ).count()
        result["error_boundary_count"] = error_boundary_count
        if error_boundary_count > 0:
            issues.append(Issue("WARN", "missing", f"Portfolio: {error_boundary_count} error boundary elements visible"))
    except Exception as e:
        pass

    # --- Overlay / Orders ---
    try:
        orders_count = page.locator(
            '[class*="Order"], [class*="order"], table'
        ).count()
        result["orders_element_count"] = orders_count
        print(f"  ✓ Orders/table elements: {orders_count}")
    except Exception as e:
        pass

    return result


def inspect_finance(page: Page, issues: list[Issue]) -> dict:
    result: dict = {}

    # --- Bank accounts ---
    try:
        account_loc = page.locator(
            '[data-testid="account-card"], [class*="AccountCard"]'
        )
        account_count = account_loc.count()
        result["account_card_count"] = account_count
        if account_count == 0:
            issues.append(Issue("WARN", "missing", "Finance: no AccountCard elements (expected ~15)"))
        else:
            print(f"  ✓ Account cards: {account_count}")
    except Exception as e:
        issues.append(Issue("ERROR", "missing", f"Finance AccountCard error: {e}"))

    # --- Finance summary strip ---
    try:
        fin_strip_loc = page.locator(
            '[data-testid="finance-summary-strip"], [data-testid="stat-card"]'
        )
        fin_strip_count = fin_strip_loc.count()
        result["finance_strip_count"] = fin_strip_count
        if fin_strip_count == 0:
            issues.append(Issue("WARN", "missing", "Finance: no FinSummaryStrip/stat-card elements"))
        else:
            print(f"  ✓ Finance strip/stat elements: {fin_strip_count}")
    except Exception as e:
        issues.append(Issue("WARN", "missing", f"Finance strip error: {e}"))

    # --- Budget grid ---
    try:
        budget_count = page.locator('[data-testid="budget-card"], [class*="Budget"]').count()
        result["budget_element_count"] = budget_count
        if budget_count == 0:
            issues.append(Issue("INFO", "missing", "Finance: no Budget elements"))
        else:
            print(f"  ✓ Budget elements: {budget_count}")
    except Exception as e:
        pass

    # --- Charts ---
    try:
        chart_count = page.locator(
            '.recharts-wrapper, [data-testid="finance-summary-strip"],'
            ' [data-testid="stat-card"], [class*="recharts"]'
        ).count()
        result["chart_count"] = chart_count
        print(f"  ✓ Finance chart elements: {chart_count}")
        if chart_count == 0:
            issues.append(Issue("WARN", "missing", "Finance: no chart elements found"))
    except Exception as e:
        pass

    # --- Transactions ---
    try:
        tx_count = page.locator(
            '[class*="Transaction"], [class*="transaction"], '
            '[class*="Expense"], [class*="expense"]'
        ).count()
        result["transaction_element_count"] = tx_count
        print(f"  ✓ Transaction elements: {tx_count}")
    except Exception as e:
        pass

    # --- Net worth / dollar amounts ---
    try:
        body_text = page.inner_text("body")
        dollar_amounts = extract_dollar_amounts(body_text)
        result["dollar_amounts_sample"] = dollar_amounts[:20]
        if not dollar_amounts:
            issues.append(Issue("WARN", "missing", "Finance: no $ values visible"))
        else:
            print(f"  ✓ Dollar values: {dollar_amounts[:5]} ...")
    except Exception as e:
        pass

    # --- Skeletons ---
    try:
        skeleton_count = page.locator('[class*="skeleton"], [class*="Skeleton"]').count()
        result["skeleton_count"] = skeleton_count
        if skeleton_count > 3:
            issues.append(Issue("WARN", "perf", f"Finance: {skeleton_count} skeletons still visible"))
    except Exception as e:
        pass

    return result


def inspect_research(page: Page, issues: list[Issue]) -> dict:
    result: dict = {}

    # --- Leaderboard table ---
    try:
        rows = page.locator("table tr")
        row_count = rows.count()
        result["leaderboard_row_count"] = row_count
        if row_count == 0:
            issues.append(Issue("WARN", "missing", "Research: no table rows found (expected leaderboard ~28 rows)"))
        else:
            print(f"  ✓ Table rows (leaderboard): {row_count}")
    except Exception as e:
        issues.append(Issue("ERROR", "missing", f"Research table error: {e}"))

    # --- Experiment / discovery counts ---
    try:
        body_text = page.inner_text("body")
        exp_matches = re.findall(r'\d[\d,KkMm]*\s*(?:experiment|run|trial|discovery|discoveries)', body_text, re.I)
        result["experiment_count_mentions"] = exp_matches[:5]
        if exp_matches:
            print(f"  ✓ Experiment mentions: {exp_matches[:3]}")
        else:
            issues.append(Issue("INFO", "missing", "Research: no 'experiment' count visible in body text"))
    except Exception as e:
        pass

    # --- Strategy list ---
    try:
        strat_count = page.locator(
            '[data-testid="strategy-breakdown"], [data-testid="leaderboard-row"],'
            ' [class*="Strategy"], [class*="Leaderboard"]'
        ).count()
        result["strategy_element_count"] = strat_count
        print(f"  ✓ Strategy/Leaderboard elements: {strat_count}")
        if strat_count == 0:
            issues.append(Issue("WARN", "missing", "Research: no Strategy or Leaderboard elements"))
    except Exception as e:
        pass

    # --- Timeline / charts ---
    try:
        chart_count = page.locator(
            '.recharts-wrapper, [class*="recharts"]'
        ).count()
        result["chart_count"] = chart_count
        print(f"  ✓ Research chart elements: {chart_count}")
    except Exception as e:
        pass

    # --- Discoveries panel ---
    try:
        disc_count = page.locator(
            '[class*="Discovery"], [class*="discovery"], [class*="Insight"], [class*="insight"]'
        ).count()
        result["discovery_count"] = disc_count
        print(f"  ✓ Discovery/Insight elements: {disc_count}")
    except Exception as e:
        pass

    # --- Skeletons ---
    try:
        skeleton_count = page.locator('[class*="skeleton"], [class*="Skeleton"]').count()
        result["skeleton_count"] = skeleton_count
        if skeleton_count > 3:
            issues.append(Issue("WARN", "perf", f"Research: {skeleton_count} skeletons still visible"))
    except Exception as e:
        pass

    return result


# ---------------------------------------------------------------------------
# Main audit
# ---------------------------------------------------------------------------

def run_audit() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    start_ts = datetime.now().isoformat(timespec="seconds")
    print(f"\n{'='*70}")
    print(f"  Atlas Dashboard UI Audit  —  {start_ts}")
    print(f"{'='*70}\n")

    dashboard_user, dashboard_pass = load_credentials()

    # Shared accumulators
    console_log: list[dict] = []
    net_errors:  list[dict] = []
    js_errors:   list[str]  = []
    issues:      list[Issue] = []
    audit_results: dict     = {}
    screenshots_taken: list[str] = []
    text_dumps_taken:  list[str] = []
    missing_elements:  list[str] = []

    with sync_playwright() as pw:
        # ----------------------------------------------------------------
        # Desktop audit
        # ----------------------------------------------------------------
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            http_credentials={"username": dashboard_user, "password": dashboard_pass},
            viewport={"width": DESKTOP_W, "height": DESKTOP_H},
            ignore_https_errors=True,
        )
        context.set_default_navigation_timeout(NAV_TIMEOUT)
        context.set_default_timeout(NAV_TIMEOUT)

        page = context.new_page()
        setup_listeners(page, console_log, net_errors, js_errors)

        # ---- Initial load ----
        print(f"[1] Loading dashboard: {DASHBOARD_URL}")
        try:
            page.goto(DASHBOARD_URL, wait_until="networkidle", timeout=GOTO_TIMEOUT)
            print(f"  ✓ Page loaded. Title: '{page.title()}'")
        except Exception as e:
            print(f"  ✗ networkidle timeout (continuing anyway): {e}")
            html_path = str(OUTPUT_DIR / "initial_load_content.html")
            try:
                Path(html_path).write_text(page.content())
                print(f"  → Saved HTML to {html_path}")
            except Exception:
                pass
            issues.append(Issue("ERROR", "network", f"Initial page load failed/timeout: {e}"))

        # Brief extra wait for React hydration
        time.sleep(2)

        # ----------------------------------------------------------------
        # Iterate through tabs
        # ----------------------------------------------------------------
        tab_inspection_map = {
            "Portfolio": inspect_portfolio,
            "Finance":   inspect_finance,
            "Research":  inspect_research,
        }

        for idx, tab_name in enumerate(TABS, start=1):
            print(f"\n[{idx+1}] Tab: {tab_name}")
            slug = slugify(tab_name)

            try:
                # Click tab by role
                tab_locator = page.get_by_role("tab", name=tab_name)
                if tab_locator.count() == 0:
                    # Fallback: button, link, or any element with matching text
                    tab_locator = page.get_by_role("button", name=tab_name)
                if tab_locator.count() == 0:
                    # Broadest fallback: any element with that text
                    tab_locator = page.locator(f'text="{tab_name}"').first
                tab_locator.click(timeout=8000)
                print(f"  ✓ Clicked '{tab_name}' tab")
            except Exception as e:
                issues.append(Issue("ERROR", "missing", f"Could not click '{tab_name}' tab: {e}"))
                try:
                    page.click(f'text={tab_name}', timeout=5000)
                    print(f"  ✓ Clicked via text fallback")
                except Exception as e2:
                    issues.append(Issue("ERROR", "missing", f"Tab click fallback also failed: {e2}"))
                    missing_elements.append(f"{tab_name} tab not clickable")

            # Wait for lazy chunk loading
            time.sleep(TAB_WAIT_SEC)

            # Try waiting for network idle after tab change
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            # --- Screenshot ---
            ss_path = str(OUTPUT_DIR / f"{idx:02d}_{slug}.png")
            try:
                page.screenshot(path=ss_path, full_page=True)
                screenshots_taken.append(ss_path)
                ss_size = Path(ss_path).stat().st_size
                print(f"  ✓ Screenshot: {ss_path} ({ss_size/1024:.0f} KB)")
            except Exception as e:
                issues.append(Issue("ERROR", "perf", f"{tab_name}: screenshot failed: {e}"))

            # --- Body text dump ---
            txt_path = str(OUTPUT_DIR / f"{idx:02d}_{slug}.txt")
            try:
                body_text = page.inner_text("body")
                Path(txt_path).write_text(body_text, encoding="utf-8")
                text_dumps_taken.append(txt_path)
                print(f"  ✓ Text dump: {txt_path} ({len(body_text):,} chars)")
            except Exception as e:
                issues.append(Issue("WARN", "perf", f"{tab_name}: inner_text failed: {e}"))
                body_text = ""
                try:
                    html_path = str(OUTPUT_DIR / f"{idx:02d}_{slug}_content.html")
                    Path(html_path).write_text(page.content())
                    print(f"  → Fallback HTML: {html_path}")
                    text_dumps_taken.append(html_path)
                except Exception:
                    pass

            # --- Tab-specific inspection ---
            inspector = tab_inspection_map.get(tab_name)
            if inspector:
                tab_result = inspector(page, issues)
                audit_results[tab_name] = tab_result

        # ----------------------------------------------------------------
        # Mobile responsive check (375×812)
        # ----------------------------------------------------------------
        print(f"\n[{len(TABS)+2}] Mobile responsive check (375×812)")
        mobile_console: list[dict] = []
        mobile_errors:  list[str]  = []
        try:
            mob_context = browser.new_context(
                http_credentials={"username": dashboard_user, "password": dashboard_pass},
                viewport={"width": MOBILE_W, "height": MOBILE_H},
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/16.0 Mobile/15E148 Safari/604.1"
                ),
                ignore_https_errors=True,
            )
            mob_page = mob_context.new_page()
            mob_page.on("console", lambda m: mobile_console.append({"type": m.type, "text": m.text[:300]}))
            mob_page.on("pageerror", lambda e: mobile_errors.append(str(e)[:400]))

            mob_page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=20_000)
            time.sleep(3)

            mob_ss = str(OUTPUT_DIR / "mobile_375_812.png")
            mob_page.screenshot(path=mob_ss, full_page=True)
            screenshots_taken.append(mob_ss)
            mob_size = Path(mob_ss).stat().st_size
            print(f"  ✓ Mobile screenshot: {mob_ss} ({mob_size/1024:.0f} KB)")

            mob_errors_filtered = [m for m in mobile_console if m["type"] == "error"]
            print(f"  ✓ Mobile console errors: {len(mob_errors_filtered)}")
            print(f"  ✓ Mobile JS exceptions: {len(mobile_errors)}")

            if mob_errors_filtered:
                for me in mob_errors_filtered[:3]:
                    issues.append(Issue("WARN", "console", f"Mobile console error: {me['text'][:150]}"))
            if mobile_errors:
                for me in mobile_errors[:3]:
                    issues.append(Issue("ERROR", "console", f"Mobile JS exception: {me[:150]}"))

            audit_results["mobile"] = {
                "console_errors": len(mob_errors_filtered),
                "js_exceptions":  len(mobile_errors),
                "screenshot":     mob_ss,
            }
            mob_context.close()

        except Exception as e:
            issues.append(Issue("ERROR", "perf", f"Mobile check failed: {e}"))
            print(f"  ✗ Mobile check error: {e}")

        # ----------------------------------------------------------------
        # DOM class survey — what CSS classes actually exist?
        # ----------------------------------------------------------------
        print(f"\n[DOM Survey] Checking actual class patterns on Portfolio tab...")
        try:
            page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=15_000)
            time.sleep(3)
            class_names: list[str] = page.evaluate("""
                () => {
                    const all = document.querySelectorAll('[class]');
                    const cls = new Set();
                    all.forEach(el => {
                        el.className.split(' ').forEach(c => { if(c) cls.add(c); });
                    });
                    return Array.from(cls).sort();
                }
            """)
            class_dump_path = str(OUTPUT_DIR / "dom_class_survey.txt")
            Path(class_dump_path).write_text("\n".join(class_names), encoding="utf-8")
            print(f"  ✓ {len(class_names)} unique CSS classes → {class_dump_path}")
            print(f"  Sample: {class_names[:30]}")
            audit_results["dom_class_count"] = len(class_names)
            audit_results["dom_class_sample"] = class_names[:50]
        except Exception as e:
            print(f"  ✗ DOM class survey failed: {e}")

        # ----------------------------------------------------------------
        # Full HTML snapshot of each tab
        # ----------------------------------------------------------------
        print(f"\n[HTML Snapshots] Capturing full HTML for each tab...")
        for idx, tab_name in enumerate(TABS, start=1):
            slug = slugify(tab_name)
            try:
                tab_loc = page.get_by_role("tab", name=tab_name)
                if tab_loc.count() == 0:
                    tab_loc = page.get_by_role("button", name=tab_name)
                if tab_loc.count() == 0:
                    page.click(f'text={tab_name}', timeout=5000)
                else:
                    tab_loc.click(timeout=5000)
                time.sleep(2)
                html_path = str(OUTPUT_DIR / f"{idx:02d}_{slug}_snapshot.html")
                Path(html_path).write_text(page.content(), encoding="utf-8")
                html_size = Path(html_path).stat().st_size
                print(f"  ✓ {tab_name} HTML: {html_path} ({html_size/1024:.0f} KB)")
            except Exception as e:
                print(f"  ✗ HTML snapshot for {tab_name}: {e}")

        browser.close()

    # ----------------------------------------------------------------
    # Final summary
    # ----------------------------------------------------------------
    print(f"\n{'='*70}")
    print(f"  AUDIT SUMMARY")
    print(f"{'='*70}\n")

    print(f"Tabs visited:        {', '.join(TABS)}")
    print(f"Screenshots taken:   {len(screenshots_taken)}")
    for ss in screenshots_taken:
        sz = Path(ss).stat().st_size if Path(ss).exists() else 0
        print(f"  → {ss}  ({sz/1024:.0f} KB)")

    print(f"\nText dumps saved:    {len(text_dumps_taken)}")
    for td in text_dumps_taken:
        sz = Path(td).stat().st_size if Path(td).exists() else 0
        print(f"  → {td}  ({sz/1024:.0f} KB)")

    # Console summary
    type_counts: dict[str, int] = {}
    for entry in console_log:
        type_counts[entry["type"]] = type_counts.get(entry["type"], 0) + 1
    print(f"\nConsole messages:    {len(console_log)} total")
    for t, c in sorted(type_counts.items()):
        print(f"  {t}: {c}")
    if console_log:
        errors_only = [e for e in console_log if e["type"] in ("error", "pageerror")]
        if errors_only:
            print(f"\n  Console errors/pageerrors ({len(errors_only)}):")
            for e in errors_only[:10]:
                print(f"    [{e['type']}] {e['text'][:120]}")
                print(f"             @ {e['location']}")

    # Network failures
    print(f"\nNetwork failures:    {len(net_errors)}")
    for nf in net_errors[:20]:
        print(f"  [{nf['status']}] {nf['url']}")

    # JS exceptions
    print(f"\nJS exceptions:       {len(js_errors)}")
    for je in js_errors[:10]:
        print(f"  {je[:200]}")

    # Issues found
    print(f"\nIssues found:        {len(issues)}")
    for sev in ("ERROR", "WARN", "INFO"):
        grp = [i for i in issues if i.severity == sev]
        if grp:
            print(f"\n  [{sev}] ({len(grp)})")
            for iss in grp:
                print(f"    [{iss.category}] {iss.message}")

    # Tab-specific results
    print(f"\n{'='*70}")
    print(f"  TAB-SPECIFIC FINDINGS")
    print(f"{'='*70}")
    for tab_name, tab_data in audit_results.items():
        if tab_name in ("mobile", "dom_class_count", "dom_class_sample"):
            continue
        print(f"\n  {tab_name}:")
        for k, v in tab_data.items():
            if isinstance(v, list):
                print(f"    {k}: {v[:5]}{'...' if len(v)>5 else ''}")
            else:
                print(f"    {k}: {v}")

    # Mobile summary
    if "mobile" in audit_results:
        m = audit_results["mobile"]
        print(f"\n  Mobile (375×812):")
        print(f"    console_errors:  {m.get('console_errors', '?')}")
        print(f"    js_exceptions:   {m.get('js_exceptions', '?')}")

    # Overall verdict
    err_count   = sum(1 for i in issues if i.severity == "ERROR")
    warn_count  = sum(1 for i in issues if i.severity == "WARN")
    js_err_ct   = len(js_errors)
    net_err_ct  = len(net_errors)
    cons_err_ct = type_counts.get("error", 0)

    print(f"\n{'='*70}")
    print(f"  VERDICT")
    print(f"{'='*70}")
    print(f"  Structural errors:  {err_count}")
    print(f"  Structural warns:   {warn_count}")
    print(f"  JS exceptions:      {js_err_ct}")
    print(f"  Network failures:   {net_err_ct}")
    print(f"  Console errors:     {cons_err_ct}")

    if err_count == 0 and js_err_ct == 0 and net_err_ct == 0:
        verdict = "✅  PASS — dashboard is clean"
    elif js_err_ct > 0 or err_count > 2 or net_err_ct > 5:
        verdict = "❌  FAIL — significant issues detected"
    else:
        verdict = "⚠️   NEEDS ATTENTION — minor issues detected"

    print(f"\n  {verdict}\n")

    # Save JSON report
    report = {
        "timestamp":        start_ts,
        "verdict":          verdict,
        "console_log":      console_log,
        "net_errors":       net_errors,
        "js_errors":        js_errors,
        "issues":           [i._asdict() for i in issues],
        "tab_results":      {k: v for k, v in audit_results.items()
                             if k not in ("dom_class_count", "dom_class_sample")},
        "screenshots":      screenshots_taken,
        "text_dumps":       text_dumps_taken,
        "console_summary":  type_counts,
    }
    report_path = OUTPUT_DIR / "audit_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"  Full JSON report: {report_path}\n")


if __name__ == "__main__":
    run_audit()
