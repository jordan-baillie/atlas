#!/usr/bin/env python3
"""Deep visual inspection of Atlas Dashboard and Agent pages.

Takes screenshots, probes every UI element's computed styles,
checks animations, color values, layout geometry, and reports
issues. Designed to be run manually for human review.

Usage:
    python3 tests/test_visual_inspection.py
    
Screenshots saved to: /root/atlas/tests/screenshots/
"""

import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# ── Setup ────────────────────────────────────────────────────────────────────

ATLAS_ROOT = "/root/atlas"
SERVER_URL = "http://127.0.0.1:18899"
AUTH_USER = "atlas_test"
AUTH_PASS = "atlas_test_pass"
SCREENSHOT_DIR = Path(ATLAS_ROOT) / "tests" / "screenshots"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

# ── Server lifecycle ─────────────────────────────────────────────────────────

def start_server():
    secrets = {"dashboard_user": AUTH_USER, "dashboard_pass": AUTH_PASS}
    sf = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, dir="/tmp")
    json.dump(secrets, sf)
    sf.close()

    env = os.environ.copy()
    env["ATLAS_SECRETS_PATH"] = sf.name

    proc = subprocess.Popen(
        ["python3", "-m", "uvicorn", "services.chat_server:app",
         "--host", "127.0.0.1", "--port", "18899", "--log-level", "warning"],
        cwd=ATLAS_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    deadline = time.time() + 12
    while time.time() < deadline:
        try:
            req = urllib.request.Request(SERVER_URL + "/api/chat/sessions")
            creds = base64.b64encode(f"{AUTH_USER}:{AUTH_PASS}".encode()).decode()
            req.add_header("Authorization", f"Basic {creds}")
            urllib.request.urlopen(req, timeout=2)
            return proc, sf.name
        except Exception:
            time.sleep(0.5)

    proc.terminate()
    os.unlink(sf.name)
    print("❌ Server failed to start")
    sys.exit(1)


def stop_server(proc, secrets_path):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    os.unlink(secrets_path)


# ── Inspection helpers ───────────────────────────────────────────────────────

def inspect_element(page, selector, label):
    """Get computed styles and geometry for an element."""
    result = page.evaluate(f"""() => {{
        const el = document.querySelector('{selector}');
        if (!el) return null;
        const cs = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return {{
            exists: true,
            visible: rect.width > 0 && rect.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden',
            tag: el.tagName.toLowerCase(),
            text: el.textContent.trim().slice(0, 100),
            rect: {{ x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) }},
            styles: {{
                display: cs.display,
                position: cs.position,
                background: cs.backgroundColor,
                color: cs.color,
                fontSize: cs.fontSize,
                fontFamily: cs.fontFamily.split(',')[0].replace(/['"]/g, ''),
                borderRadius: cs.borderRadius,
                border: cs.border,
                opacity: cs.opacity,
                overflow: cs.overflow || cs.overflowY,
                flexDirection: cs.flexDirection,
                gap: cs.gap,
                animation: cs.animationName !== 'none' ? cs.animationName : null,
                transition: cs.transitionProperty !== 'all' && cs.transitionProperty !== 'none' ? cs.transitionProperty : null,
                willChange: cs.willChange !== 'auto' ? cs.willChange : null,
                backdropFilter: cs.backdropFilter !== 'none' ? cs.backdropFilter : null,
                boxShadow: cs.boxShadow !== 'none' ? cs.boxShadow.slice(0, 60) : null,
                contain: cs.contain !== 'none' ? cs.contain : null,
            }}
        }};
    }}""")
    return result


def check(condition, label, detail=""):
    status = "✅" if condition else "❌"
    print(f"  {status} {label}" + (f" — {detail}" if detail else ""))
    return condition


# ── Main inspection ──────────────────────────────────────────────────────────

def main():
    from playwright.sync_api import sync_playwright

    proc, secrets_path = start_server()
    issues = []
    total_checks = 0
    passed_checks = 0

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])

            # ═══════════════════════════════════════════════════════════════
            # DASHBOARD PAGE INSPECTION
            # ═══════════════════════════════════════════════════════════════
            print("\n" + "=" * 60)
            print("  DASHBOARD (/) — Visual Inspection")
            print("=" * 60)

            ctx = browser.new_context(
                http_credentials={"username": AUTH_USER, "password": AUTH_PASS},
                viewport={"width": 1440, "height": 900},
            )
            page = ctx.new_page()
            js_errors = []
            page.on("pageerror", lambda e: js_errors.append(str(e)))
            page.goto(SERVER_URL, wait_until="networkidle")
            page.wait_for_timeout(2000)

            # Screenshot
            page.screenshot(path=str(SCREENSHOT_DIR / "dashboard-desktop.png"), full_page=True)
            print(f"\n  📸 Screenshot: {SCREENSHOT_DIR / 'dashboard-desktop.png'}")

            # JS errors
            critical_js = [e for e in js_errors if "SyntaxError" in e or "ReferenceError" in e]
            total_checks += 1
            passed_checks += check(len(critical_js) == 0, "No critical JS errors", f"{len(js_errors)} total console errors, {len(critical_js)} critical")

            # Header
            print("\n  ── Header ──")
            for sel, label in [(".header .logo", "Logo"), ("#regime-indicator", "Regime indicator"),
                               ("#theme-btn", "Theme toggle"), (".agent-link", "Agent link")]:
                info = inspect_element(page, sel, label)
                total_checks += 1
                if info and info["visible"]:
                    passed_checks += 1
                    print(f"  ✅ {label}: {info['rect']['w']}×{info['rect']['h']}px, font={info['styles']['fontSize']}, color={info['styles']['color'][:20]}")
                else:
                    print(f"  ❌ {label}: {'not found' if not info else 'not visible'}")
                    issues.append(f"Dashboard: {label} not visible")

            # Agent link styling
            al = inspect_element(page, ".agent-link", "Agent link")
            if al:
                total_checks += 1
                passed_checks += check(al["styles"]["borderRadius"] != "0px", "Agent link has border-radius", al["styles"]["borderRadius"])

            # Summary strip
            print("\n  ── Summary Strip ──")
            strip = inspect_element(page, ".summary-strip", "Summary strip")
            total_checks += 1
            if strip and strip["visible"]:
                passed_checks += 1
                stat_count = page.evaluate("() => document.querySelectorAll('.summary-strip .stat').length")
                total_checks += 1
                passed_checks += check(stat_count == 4, f"Has 4 stats", f"found {stat_count}")
                print(f"  ✅ Summary strip: {strip['rect']['w']}×{strip['rect']['h']}px, {stat_count} stats")
            else:
                print(f"  ❌ Summary strip not visible")
                issues.append("Dashboard: summary strip not visible")

            # Two-panel layout
            print("\n  ── Layout ──")
            tp = inspect_element(page, ".two-panel", "Two-panel grid")
            total_checks += 1
            if tp and tp["visible"]:
                passed_checks += 1
                print(f"  ✅ Two-panel: {tp['rect']['w']}×{tp['rect']['h']}px, display={tp['styles']['display']}")
            else:
                print(f"  ❌ Two-panel layout not found")
                issues.append("Dashboard: two-panel layout missing")

            pp = inspect_element(page, ".panel-portfolio", "Portfolio panel")
            pc = inspect_element(page, ".panel-chat", "Chat panel")
            for panel, name in [(pp, "Portfolio"), (pc, "Chat")]:
                total_checks += 1
                if panel and panel["visible"]:
                    passed_checks += 1
                    print(f"  ✅ {name} panel: {panel['rect']['w']}×{panel['rect']['h']}px")
                else:
                    print(f"  ❌ {name} panel not visible")
                    issues.append(f"Dashboard: {name} panel not visible")

            # Chat panel elements
            print("\n  ── Chat Panel ──")
            for sel, label in [("#chat-input", "Chat input"), ("#chat-send", "Send button"),
                               ("#chat-new-session", "New session btn"), ("#chat-session-select", "Session selector"),
                               ("#chat-messages", "Messages container"), ("#chat-status", "Status bar")]:
                info = inspect_element(page, sel, label)
                total_checks += 1
                if info and info["visible"]:
                    passed_checks += 1
                    detail = f"{info['rect']['w']}×{info['rect']['h']}px"
                    if info["styles"]["background"]:
                        detail += f", bg={info['styles']['background'][:30]}"
                    print(f"  ✅ {label}: {detail}")
                else:
                    # Some chat elements may be hidden in collapsed state
                    if info:
                        print(f"  ⚠️  {label}: exists but not visible (display={info['styles']['display']})")
                    else:
                        print(f"  ❌ {label}: not found")
                        issues.append(f"Dashboard chat: {label} missing")

            # Equity chart
            print("\n  ── Charts ──")
            ec = inspect_element(page, "#equity-canvas", "Equity chart")
            total_checks += 1
            if ec and ec["visible"]:
                passed_checks += 1
                print(f"  ✅ Equity chart: {ec['rect']['w']}×{ec['rect']['h']}px")
            else:
                print(f"  ❌ Equity chart not visible")

            # Orders collapse
            oc = inspect_element(page, ".orders-collapse", "Orders collapse")
            total_checks += 1
            if oc:
                passed_checks += 1
                print(f"  ✅ Orders collapse: tag={oc['tag']}, {oc['rect']['w']}×{oc['rect']['h']}px")
            else:
                print(f"  ❌ Orders collapse not found")

            # Regime timeline
            rt = inspect_element(page, "#regime-timeline-canvas", "Regime timeline")
            total_checks += 1
            if rt and rt["visible"]:
                passed_checks += 1
                print(f"  ✅ Regime timeline: {rt['rect']['w']}×{rt['rect']['h']}px")

            # Mobile screenshot
            ctx.close()
            ctx_mobile = browser.new_context(
                http_credentials={"username": AUTH_USER, "password": AUTH_PASS},
                viewport={"width": 390, "height": 844},
            )
            mp = ctx_mobile.new_page()
            mp.goto(SERVER_URL, wait_until="domcontentloaded")
            mp.wait_for_timeout(1500)
            mp.screenshot(path=str(SCREENSHOT_DIR / "dashboard-mobile.png"), full_page=True)
            print(f"\n  📸 Mobile screenshot: {SCREENSHOT_DIR / 'dashboard-mobile.png'}")
            ctx_mobile.close()

            # ═══════════════════════════════════════════════════════════════
            # AGENT PAGE INSPECTION
            # ═══════════════════════════════════════════════════════════════
            print("\n" + "=" * 60)
            print("  AGENT PAGE (/chat) — Visual Inspection")
            print("=" * 60)

            ctx2 = browser.new_context(
                http_credentials={"username": AUTH_USER, "password": AUTH_PASS},
                viewport={"width": 1440, "height": 900},
            )
            page2 = ctx2.new_page()
            js_errors2 = []
            page2.on("pageerror", lambda e: js_errors2.append(str(e)))
            page2.goto(SERVER_URL + "/chat", wait_until="networkidle")
            page2.wait_for_timeout(2000)

            page2.screenshot(path=str(SCREENSHOT_DIR / "agent-desktop-empty.png"))
            print(f"\n  📸 Screenshot: {SCREENSHOT_DIR / 'agent-desktop-empty.png'}")

            critical_js2 = [e for e in js_errors2 if "SyntaxError" in e or "ReferenceError" in e]
            total_checks += 1
            passed_checks += check(len(critical_js2) == 0, "No critical JS errors", f"{len(js_errors2)} total, {len(critical_js2)} critical")

            # Header
            print("\n  ── Header ──")
            for sel, label in [(".agent-logo", "Logo"), ("#agent-session-select", "Session selector"),
                               ("#agent-new-session", "New session btn"), (".agent-back-link", "← Dashboard link"),
                               ("#agent-conn-dot", "Connection dot")]:
                info = inspect_element(page2, sel, label)
                total_checks += 1
                if info and info["visible"]:
                    passed_checks += 1
                    extras = []
                    if info["styles"]["animation"]:
                        extras.append(f"anim={info['styles']['animation']}")
                    if info["styles"]["transition"]:
                        extras.append(f"trans={info['styles']['transition'][:30]}")
                    print(f"  ✅ {label}: {info['rect']['w']}×{info['rect']['h']}px" + (f" [{', '.join(extras)}]" if extras else ""))
                else:
                    print(f"  ❌ {label}: {'not found' if not info else 'not visible'}")
                    issues.append(f"Agent: {label} not visible")

            # Connection dot animation
            cd = inspect_element(page2, "#agent-conn-dot", "conn dot")
            if cd:
                total_checks += 1
                has_anim = cd["styles"]["animation"] is not None or cd["styles"]["transition"] is not None
                passed_checks += check(has_anim, "Connection dot has animation/transition")

            # Main layout
            print("\n  ── Layout ──")
            main = inspect_element(page2, ".agent-main", "Main area")
            total_checks += 1
            if main and main["visible"]:
                passed_checks += 1
                print(f"  ✅ Main area: {main['rect']['w']}×{main['rect']['h']}px, display={main['styles']['display']}")

            msgs_pane = inspect_element(page2, ".agent-messages-pane", "Messages pane")
            act_pane = inspect_element(page2, ".agent-activity-pane", "Activity pane")
            for pane, name in [(msgs_pane, "Messages pane"), (act_pane, "Activity pane")]:
                total_checks += 1
                if pane and pane["visible"]:
                    passed_checks += 1
                    extras = []
                    if pane["styles"]["backdropFilter"]:
                        extras.append(f"blur={pane['styles']['backdropFilter']}")
                    if pane["styles"]["willChange"]:
                        extras.append(f"will-change={pane['styles']['willChange']}")
                    print(f"  ✅ {name}: {pane['rect']['w']}×{pane['rect']['h']}px" + (f" [{', '.join(extras)}]" if extras else ""))
                else:
                    print(f"  ❌ {name} not visible")
                    issues.append(f"Agent: {name} not visible")

            # Check pane width ratio
            if msgs_pane and act_pane and msgs_pane["visible"] and act_pane["visible"]:
                total_width = msgs_pane["rect"]["w"] + act_pane["rect"]["w"]
                msg_pct = msgs_pane["rect"]["w"] / total_width * 100 if total_width else 0
                act_pct = act_pane["rect"]["w"] / total_width * 100 if total_width else 0
                total_checks += 1
                passed_checks += check(55 < msg_pct < 75, f"Messages ~65% width", f"{msg_pct:.0f}% messages, {act_pct:.0f}% activity")

            # Empty state
            print("\n  ── Empty State ──")
            es = inspect_element(page2, ".agent-empty", "Empty state")
            total_checks += 1
            if es and es["visible"]:
                passed_checks += 1
                icon = inspect_element(page2, ".agent-empty-icon", "Empty icon")
                icon_anim = icon["styles"]["animation"] if icon else None
                total_checks += 1
                passed_checks += check(icon_anim is not None, "Empty icon has float animation", str(icon_anim))
                print(f"  ✅ Empty state: {es['rect']['w']}×{es['rect']['h']}px, text='{es['text'][:50]}...'")
            else:
                print(f"  ⚠️  Empty state not visible (may have been dismissed)")

            # Activity feed
            print("\n  ── Activity Feed ──")
            af = inspect_element(page2, "#activity-feed", "Activity feed")
            total_checks += 1
            if af and af["visible"]:
                passed_checks += 1
                print(f"  ✅ Activity feed: {af['rect']['w']}×{af['rect']['h']}px, overflow={af['styles']['overflow']}")
                # Check empty state text
                empty_text = page2.evaluate("() => { const el = document.querySelector('#activity-feed'); return el && el.childElementCount === 0; }")
                print(f"  ℹ️  Activity feed empty: {empty_text}")

            # Cost bar
            cb = inspect_element(page2, ".activity-cost-bar", "Cost bar")
            total_checks += 1
            if cb and cb["visible"]:
                passed_checks += 1
                print(f"  ✅ Cost bar: text='{cb['text'][:40]}'")

            # Input area
            print("\n  ── Input Area ──")
            for sel, label in [("#agent-input", "Textarea"), ("#agent-send", "Send button"),
                               ("#agent-cancel", "Cancel button"), ("#agent-status", "Status bar")]:
                info = inspect_element(page2, sel, label)
                total_checks += 1
                if info:
                    vis = "visible" if info["visible"] else "hidden"
                    extras = []
                    if info["styles"]["transition"]:
                        extras.append(f"transition")
                    if info["styles"]["boxShadow"]:
                        extras.append(f"shadow")
                    print(f"  ✅ {label}: {info['rect']['w']}×{info['rect']['h']}px ({vis})" + (f" [{', '.join(extras)}]" if extras else ""))
                    passed_checks += 1
                else:
                    print(f"  ❌ {label}: not found")
                    issues.append(f"Agent: {label} missing")

            # Check input focus glow
            page2.locator("#agent-input").focus()
            page2.wait_for_timeout(200)
            focus_styles = page2.evaluate("""() => {
                const el = document.querySelector('#agent-input');
                const cs = window.getComputedStyle(el);
                return { boxShadow: cs.boxShadow, borderColor: cs.borderColor };
            }""")
            total_checks += 1
            has_glow = focus_styles["boxShadow"] != "none" and "inset" not in focus_styles["boxShadow"]
            passed_checks += check(has_glow or "99, 102, 241" in (focus_styles["borderColor"] or ""),
                                   "Input has focus glow/accent border", f"shadow={focus_styles['boxShadow'][:40]}, border={focus_styles['borderColor'][:30]}")

            # Status bar content
            status_text = page2.evaluate("() => document.querySelector('#agent-status')?.textContent || ''")
            total_checks += 1
            passed_checks += check("opus" in status_text.lower() or "model" in status_text.lower() or "$" in status_text,
                                   "Status bar shows model/cost info", f"'{status_text.strip()[:60]}'")

            # Test typing in input
            print("\n  ── Input Interaction ──")
            page2.locator("#agent-input").fill("Hello, analyze the current portfolio")
            page2.wait_for_timeout(300)
            val = page2.locator("#agent-input").input_value()
            total_checks += 1
            passed_checks += check(val == "Hello, analyze the current portfolio", "Can type in input")

            # Screenshot with text
            page2.screenshot(path=str(SCREENSHOT_DIR / "agent-with-input.png"))
            print(f"  📸 Screenshot: {SCREENSHOT_DIR / 'agent-with-input.png'}")

            # Test send (will create a user message bubble)
            page2.locator("#agent-send").click()
            page2.wait_for_timeout(1500)

            page2.screenshot(path=str(SCREENSHOT_DIR / "agent-after-send.png"))
            print(f"  📸 Screenshot: {SCREENSHOT_DIR / 'agent-after-send.png'}")

            # Check user message appeared
            user_msgs = page2.evaluate("() => document.querySelectorAll('.agent-msg-user').length")
            total_checks += 1
            passed_checks += check(user_msgs >= 1, "User message bubble appeared", f"found {user_msgs}")

            if user_msgs >= 1:
                um = inspect_element(page2, ".agent-msg-user .msg-bubble", "User bubble")
                if um:
                    total_checks += 1
                    passed_checks += check(um["visible"], "User bubble is visible", f"{um['rect']['w']}×{um['rect']['h']}px")
                    total_checks += 1
                    has_radius = um["styles"]["borderRadius"] != "0px"
                    passed_checks += check(has_radius, "User bubble has border-radius", um["styles"]["borderRadius"])
                    print(f"  ✅ User bubble: bg={um['styles']['background'][:30]}, color={um['styles']['color'][:20]}")

            # Check input was cleared
            post_val = page2.locator("#agent-input").input_value()
            total_checks += 1
            passed_checks += check(post_val == "", "Input cleared after send")

            # Check if assistant message or thinking appeared
            page2.wait_for_timeout(2000)
            asst_count = page2.evaluate("() => document.querySelectorAll('.agent-msg-assistant').length")
            thinking = page2.evaluate("() => document.querySelectorAll('.thinking-indicator').length")
            print(f"  ℹ️  Assistant messages: {asst_count}, Thinking indicators: {thinking}")

            # Check activity feed got entries
            act_count = page2.evaluate("() => document.querySelectorAll('#activity-feed .act-card').length")
            print(f"  ℹ️  Activity cards: {act_count}")

            # Check scroll-to-bottom button exists
            stb = inspect_element(page2, "#scroll-bottom-btn, .scroll-bottom-btn", "Scroll-to-bottom btn")
            total_checks += 1
            if stb:
                passed_checks += 1
                print(f"  ✅ Scroll-to-bottom button: exists (opacity={stb['styles']['opacity']})")
            else:
                print(f"  ⚠️  Scroll-to-bottom button: not found (may not be in DOM)")

            # Check message timestamp elements
            msg_time = inspect_element(page2, ".msg-time", "Message timestamp")
            total_checks += 1
            if msg_time:
                passed_checks += 1
                print(f"  ✅ Message timestamps: text='{msg_time['text'][:20]}', opacity={msg_time['styles']['opacity']}")
            else:
                print(f"  ⚠️  Message timestamps: not found")

            # Check message copy button
            copy_btn = inspect_element(page2, ".msg-copy-btn", "Message copy btn")
            total_checks += 1
            if copy_btn:
                passed_checks += 1
                print(f"  ✅ Message copy button: opacity={copy_btn['styles']['opacity']}")
            else:
                print(f"  ⚠️  Message copy button: not found (may appear on hover)")

            # Font smoothing
            print("\n  ── Typography & Performance ──")
            font_smooth = page2.evaluate("""() => {
                const cs = window.getComputedStyle(document.body);
                return {
                    webkitSmoothing: cs.webkitFontSmoothing || cs['-webkit-font-smoothing'] || 'unknown',
                    fontFamily: cs.fontFamily.split(',')[0].replace(/['"]/g, ''),
                };
            }""")
            print(f"  ℹ️  Font: {font_smooth['fontFamily']}, smoothing: {font_smooth['webkitSmoothing']}")

            # Check will-change on key elements
            for sel, label in [(".agent-msg", "Messages"), (".act-card", "Activity cards")]:
                wc = page2.evaluate(f"""() => {{
                    const el = document.querySelector('{sel}');
                    return el ? window.getComputedStyle(el).willChange : null;
                }}""")
                if wc and wc != "auto":
                    print(f"  ✅ {label}: will-change={wc}")
                else:
                    print(f"  ⚠️  {label}: no will-change set")

            # Final screenshot with any responses
            page2.wait_for_timeout(3000)
            page2.screenshot(path=str(SCREENSHOT_DIR / "agent-final.png"))
            print(f"\n  📸 Final screenshot: {SCREENSHOT_DIR / 'agent-final.png'}")

            # Mobile agent
            ctx2.close()
            ctx_mob = browser.new_context(
                http_credentials={"username": AUTH_USER, "password": AUTH_PASS},
                viewport={"width": 390, "height": 844},
            )
            mp2 = ctx_mob.new_page()
            mp2.goto(SERVER_URL + "/chat", wait_until="domcontentloaded")
            mp2.wait_for_timeout(1500)
            mp2.screenshot(path=str(SCREENSHOT_DIR / "agent-mobile.png"))
            print(f"  📸 Mobile screenshot: {SCREENSHOT_DIR / 'agent-mobile.png'}")

            # Check activity feed hidden on mobile
            act_vis = mp2.evaluate("""() => {
                const el = document.querySelector('#activity-feed') || document.querySelector('.agent-activity-pane');
                return el ? window.getComputedStyle(el).display : 'not found';
            }""")
            total_checks += 1
            passed_checks += check(act_vis == "none" or act_vis == "not found", "Activity feed hidden on mobile", f"display={act_vis}")

            ctx_mob.close()
            browser.close()

    finally:
        stop_server(proc, secrets_path)

    # ═══════════════════════════════════════════════════════════════════════
    # SUMMARY
    # ═══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print(f"  INSPECTION SUMMARY: {passed_checks}/{total_checks} checks passed")
    print("=" * 60)

    if issues:
        print(f"\n  ⚠️  {len(issues)} issue(s):")
        for issue in issues:
            print(f"    → {issue}")
    else:
        print("\n  ✨ No issues found!")

    print(f"\n  📂 Screenshots: {SCREENSHOT_DIR}/")
    for f in sorted(SCREENSHOT_DIR.glob("*.png")):
        size_kb = f.stat().st_size / 1024
        print(f"    {f.name} ({size_kb:.0f} KB)")

    print()
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
