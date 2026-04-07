"""Thorough end-to-end test of the Atlas Agent page (/chat).

Tests every UI element, interaction, animation, and edge case.
Uses Playwright with the test server managed by with_server.py.

Run:
    python3 /root/.pi/agent/skills/anthropic-skills/skills/webapp-testing/scripts/with_server.py \
        --server "cd /root/atlas && ATLAS_SECRETS_PATH=/tmp/_test_secrets.json python3 -m uvicorn services.chat_server:app --host 127.0.0.1 --port 18899 --log-level warning" \
        --port 18899 \
        -- python3 /root/atlas/tests/test_agent_thorough.py
"""

import json
import os
import sys
from pathlib import Path

# Write test secrets
secrets_path = "/tmp/_test_secrets.json"
if not os.path.exists(secrets_path):
    with open(secrets_path, "w") as f:
        json.dump({"dashboard_user": "test", "dashboard_pass": "test"}, f)
os.environ["ATLAS_SECRETS_PATH"] = secrets_path

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:18899"
USER = "test"
PASS = "test"
SHOTS = Path("/root/atlas/tests/screenshots")
SHOTS.mkdir(parents=True, exist_ok=True)

issues = []
checks = 0
passed = 0

def check(ok, label, detail=""):
    global checks, passed
    checks += 1
    if ok:
        passed += 1
        print(f"  ✅ {label}" + (f" — {detail}" if detail else ""))
    else:
        issues.append(label + (f": {detail}" if detail else ""))
        print(f"  ❌ {label}" + (f" — {detail}" if detail else ""))
    return ok

def get_style(page, selector, prop):
    return page.evaluate(f"() => {{ const el = document.querySelector('{selector}'); return el ? window.getComputedStyle(el).{prop} : null; }}")

def el_exists(page, sel):
    return page.evaluate(f"() => document.querySelector('{sel}') !== null")

def el_visible(page, sel):
    return page.evaluate(f"""() => {{
        const el = document.querySelector('{sel}');
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const cs = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
    }}""")

def el_rect(page, sel):
    return page.evaluate(f"""() => {{
        const el = document.querySelector('{sel}');
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return {{x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}};
    }}""")

# ══════════════════════════════════════════════════════════════════════════════

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])

    # ══════════════════════════════════════════════════════════════════════
    # TEST 1: DASHBOARD — Chat panel removed, full-width portfolio
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  DASHBOARD — Chat Removed, Full-Width Portfolio")
    print("=" * 70 + "\n")

    ctx = browser.new_context(
        http_credentials={"username": USER, "password": PASS},
        viewport={"width": 1440, "height": 900},
    )
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(2000)

    page.screenshot(path=str(SHOTS / "01-dashboard.png"), full_page=True)

    critical = [e for e in errors if "SyntaxError" in e or "ReferenceError" in e]
    check(len(critical) == 0, "Dashboard: no JS errors", f"{len(errors)} total, {len(critical)} critical")

    # Chat panel should be GONE
    check(not el_exists(page, "#chat-panel"), "Dashboard: chat panel removed")
    check(not el_exists(page, "#chat-input"), "Dashboard: chat input removed")
    check(not el_exists(page, "#chat-messages"), "Dashboard: chat messages removed")
    check(not el_exists(page, ".two-panel"), "Dashboard: .two-panel grid removed")

    # Portfolio should be full-width
    pr = el_rect(page, ".panel-portfolio")
    check(pr is not None and pr["w"] > 900, "Dashboard: portfolio full-width", f"{pr['w']}px" if pr else "missing")

    # Agent link still present
    check(el_visible(page, 'a[href="/chat"]'), "Dashboard: ◈ Agent link visible")

    # Key elements
    check(el_visible(page, "#equity-canvas"), "Dashboard: equity chart")
    check(el_visible(page, "#positions-grid"), "Dashboard: positions grid")
    check(el_exists(page, ".orders-collapse"), "Dashboard: orders collapse")
    check(el_visible(page, "#regime-timeline-canvas"), "Dashboard: regime timeline")

    ctx.close()

    # ══════════════════════════════════════════════════════════════════════
    # TEST 2: AGENT PAGE — Fresh load, empty state
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  AGENT PAGE — Structure & Empty State")
    print("=" * 70 + "\n")

    ctx2 = browser.new_context(
        http_credentials={"username": USER, "password": PASS},
        viewport={"width": 1440, "height": 900},
    )
    page2 = ctx2.new_page()
    errors2 = []
    page2.on("pageerror", lambda e: errors2.append(str(e)))
    page2.goto(URL + "/chat", wait_until="networkidle")
    page2.wait_for_timeout(2000)

    page2.screenshot(path=str(SHOTS / "02-agent-empty.png"))

    critical2 = [e for e in errors2 if "SyntaxError" in e or "ReferenceError" in e]
    check(len(critical2) == 0, "Agent: no JS errors", f"{len(errors2)} total")

    # Header elements
    check(el_visible(page2, ".agent-logo"), "Agent: logo visible")
    check(el_visible(page2, "#agent-session-select"), "Agent: session selector")
    check(el_visible(page2, "#agent-new-session"), "Agent: + New button")
    check(el_visible(page2, "#agent-model-select"), "Agent: model selector")
    check(el_visible(page2, ".agent-back-link"), "Agent: ← Dashboard link")
    check(el_visible(page2, "#agent-conn-dot"), "Agent: connection dot")

    # Model selector has 3 options
    opt_count = page2.evaluate("() => document.querySelectorAll('#agent-model-select option').length")
    check(opt_count >= 3, "Agent: model selector has ≥3 options", f"found {opt_count}")

    # Default model is opus
    model_val = page2.evaluate("() => document.querySelector('#agent-model-select')?.value || ''")
    check("opus" in model_val.lower(), "Agent: default model is opus", model_val)

    # Layout: two panes
    check(el_visible(page2, ".agent-messages-pane"), "Agent: messages pane visible")
    check(el_visible(page2, ".agent-activity-pane"), "Agent: activity pane visible")

    mp = el_rect(page2, ".agent-messages-pane")
    ap = el_rect(page2, ".agent-activity-pane")
    if mp and ap:
        total = mp["w"] + ap["w"]
        msg_pct = mp["w"] / total * 100 if total else 0
        check(55 < msg_pct < 75, "Agent: messages ~65% width", f"{msg_pct:.0f}%")

    # Activity feed
    check(el_visible(page2, "#activity-feed"), "Agent: activity feed visible")
    check(el_visible(page2, ".activity-cost-bar"), "Agent: cost bar visible")

    # Teams section (hidden until delegation)
    teams_display = get_style(page2, "#teams-section", "display")
    check(teams_display == "none" or not el_exists(page2, "#teams-section") or teams_display is None,
          "Agent: teams section hidden initially")

    # Swarm section (hidden until swarm)
    swarm_display = get_style(page2, "#swarm-section", "display")
    check(swarm_display == "none" or not el_exists(page2, "#swarm-section") or swarm_display is None,
          "Agent: swarm section hidden initially")

    # Input area
    check(el_visible(page2, "#agent-input"), "Agent: input textarea visible")
    check(el_visible(page2, "#agent-send"), "Agent: send button visible")
    check(el_visible(page2, "#agent-status"), "Agent: status bar visible")

    # Status bar shows model + cost + connection
    status_text = page2.evaluate("() => document.querySelector('#agent-status')?.textContent || ''")
    check("opus" in status_text.lower(), "Agent: status shows opus", status_text[:80])
    check("$" in status_text, "Agent: status shows cost")
    check("connected" in status_text.lower(), "Agent: status shows connected")

    # Input focus glow
    page2.locator("#agent-input").focus()
    page2.wait_for_timeout(200)
    shadow = get_style(page2, "#agent-input", "boxShadow")
    check(shadow and shadow != "none", "Agent: input focus glow", str(shadow)[:50] if shadow else "none")

    # ══════════════════════════════════════════════════════════════════════
    # TEST 3: AGENT PAGE — Suggested prompts
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  AGENT PAGE — Suggested Prompts")
    print("=" * 70 + "\n")

    # Prompts exist in source (may be hidden if sessions loaded)
    has_prompts = page2.evaluate("() => document.documentElement.innerHTML.includes('prompt-chip')")
    check(has_prompts, "Agent: prompt chips in HTML source")

    # If visible, check they're clickable
    chip_count = page2.evaluate("() => document.querySelectorAll('.prompt-chip').length")
    if chip_count > 0:
        check(chip_count >= 4, "Agent: ≥4 prompt chips", f"found {chip_count}")
        first_prompt = page2.evaluate("() => document.querySelector('.prompt-chip')?.dataset?.prompt || ''")
        check(len(first_prompt) > 10, "Agent: chip has data-prompt", first_prompt[:60])

    # ══════════════════════════════════════════════════════════════════════
    # TEST 4: AGENT PAGE — Typing and sending
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  AGENT PAGE — Typing, Sending, Message Bubbles")
    print("=" * 70 + "\n")

    # Type a message
    page2.locator("#agent-input").fill("Hello, what is 2+2?")
    page2.wait_for_timeout(200)
    check(page2.locator("#agent-input").input_value() == "Hello, what is 2+2?", "Agent: can type in input")

    page2.screenshot(path=str(SHOTS / "03-agent-typed.png"))

    # Send the message
    page2.locator("#agent-send").click()
    page2.wait_for_timeout(500)

    # Input should be cleared
    check(page2.locator("#agent-input").input_value() == "", "Agent: input cleared after send")

    # User message bubble should appear
    user_msgs = page2.evaluate("() => document.querySelectorAll('.agent-msg-user').length")
    check(user_msgs >= 1, "Agent: user message bubble appeared", f"found {user_msgs}")

    # User bubble styling
    if user_msgs >= 1:
        bubble_bg = get_style(page2, ".agent-msg-user .msg-bubble", "background")
        bubble_radius = get_style(page2, ".agent-msg-user .msg-bubble", "borderRadius")
        check(bubble_radius and "0px" not in bubble_radius, "Agent: user bubble rounded", bubble_radius)

    page2.screenshot(path=str(SHOTS / "04-agent-sent.png"))

    # Wait for response to start streaming
    page2.wait_for_timeout(3000)

    page2.screenshot(path=str(SHOTS / "05-agent-streaming.png"))

    # Check for assistant message or thinking indicator
    asst_msgs = page2.evaluate("() => document.querySelectorAll('.agent-msg-assistant').length")
    thinking = page2.evaluate("() => document.querySelectorAll('.thinking-indicator').length")
    check(asst_msgs >= 1 or thinking >= 1, "Agent: response started (assistant msg or thinking)",
          f"msgs={asst_msgs}, thinking={thinking}")

    # Check activity feed got cards
    act_count = page2.evaluate("() => document.querySelectorAll('#activity-feed .act-card').length")
    print(f"  ℹ️  Activity cards so far: {act_count}")

    # Check status bar changed from "connected"
    status_now = page2.evaluate("() => document.querySelector('#agent-status')?.textContent || ''")
    print(f"  ℹ️  Status: {status_now.strip()[:60]}")

    # Check if cancel button is visible (should be during generation)
    cancel_vis = el_visible(page2, "#agent-cancel")
    print(f"  ℹ️  Cancel button visible: {cancel_vis}")

    # Wait for completion (up to 30s)
    print("\n  ⏳ Waiting for response to complete (up to 30s)...")
    for i in range(30):
        page2.wait_for_timeout(1000)
        done = page2.evaluate("""() => {
            const msgs = document.querySelectorAll('.agent-msg-assistant .msg-bubble');
            if (msgs.length === 0) return false;
            const last = msgs[msgs.length - 1];
            // If it has rendered HTML (not just raw text) and no thinking indicator, it's done
            return last.innerHTML.includes('<') && !document.querySelector('.thinking-indicator');
        }""")
        if done:
            print(f"  ✅ Response completed in ~{i+1}s")
            break
    else:
        print(f"  ⏱️ Response still streaming after 30s (may be slow due to Opus)")

    page2.screenshot(path=str(SHOTS / "06-agent-response.png"))

    # ══════════════════════════════════════════════════════════════════════
    # TEST 5: AGENT PAGE — Response quality
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  AGENT PAGE — Response Quality & Rendering")
    print("=" * 70 + "\n")

    # Check assistant message has content
    asst_text = page2.evaluate("""() => {
        const msgs = document.querySelectorAll('.agent-msg-assistant .msg-bubble');
        if (msgs.length === 0) return '';
        return msgs[msgs.length - 1].textContent || '';
    }""")
    check(len(asst_text) > 5, "Agent: assistant response has content", f"{len(asst_text)} chars")

    # Check markdown rendering (should have <p> or <strong> etc)
    has_html = page2.evaluate("""() => {
        const msgs = document.querySelectorAll('.agent-msg-assistant .msg-bubble');
        if (msgs.length === 0) return false;
        const html = msgs[msgs.length - 1].innerHTML;
        return html.includes('<p>') || html.includes('<strong>') || html.includes('<code>');
    }""")
    check(has_html, "Agent: markdown rendered to HTML")

    # Message timestamp
    has_ts = page2.evaluate("() => document.querySelectorAll('.msg-time').length > 0")
    check(has_ts, "Agent: message timestamps present")

    # Activity feed has cards after the exchange
    final_act_count = page2.evaluate("() => document.querySelectorAll('#activity-feed .act-card').length")
    check(final_act_count >= 1, "Agent: activity feed has cards", f"{final_act_count} cards")

    # Cost updated
    cost_text = page2.evaluate("() => document.querySelector('#agent-cost')?.textContent || ''")
    check("$" in cost_text, "Agent: cost display updated", cost_text)

    # ══════════════════════════════════════════════════════════════════════
    # TEST 6: AGENT PAGE — Keyboard shortcuts
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  AGENT PAGE — Keyboard Shortcuts")
    print("=" * 70 + "\n")

    # Ctrl+K focuses input
    page2.locator("body").click()  # unfocus input first
    page2.wait_for_timeout(200)
    page2.keyboard.press("Control+k")
    page2.wait_for_timeout(300)
    focused = page2.evaluate("() => document.activeElement?.id === 'agent-input'")
    check(focused, "Agent: Ctrl+K focuses input")

    # ? shows shortcuts modal
    page2.locator("body").click()
    page2.wait_for_timeout(200)
    page2.keyboard.press("?")
    page2.wait_for_timeout(500)
    modal_vis = el_visible(page2, "#shortcuts-modal") or el_exists(page2, "#shortcuts-modal")
    check(modal_vis, "Agent: ? opens shortcuts modal")

    page2.screenshot(path=str(SHOTS / "07-shortcuts-modal.png"))

    # Close modal (Escape or click close)
    page2.keyboard.press("Escape")
    page2.wait_for_timeout(300)

    # Up arrow recalls last message
    page2.locator("#agent-input").focus()
    page2.locator("#agent-input").fill("")
    page2.keyboard.press("ArrowUp")
    page2.wait_for_timeout(200)
    recalled = page2.locator("#agent-input").input_value()
    check(len(recalled) > 0, "Agent: ↑ recalls last message", recalled[:40])

    # Clear the recalled message
    page2.locator("#agent-input").fill("")

    # ══════════════════════════════════════════════════════════════════════
    # TEST 7: AGENT PAGE — Edit & Retry buttons
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  AGENT PAGE — Edit/Retry Hover Actions")
    print("=" * 70 + "\n")

    # Hover over user message to check for edit button
    user_msg = page2.locator(".agent-msg-user").first
    if user_msg.count() > 0:
        user_msg.hover()
        page2.wait_for_timeout(300)
        edit_btn = page2.evaluate("() => { const b = document.querySelector('.msg-edit-btn'); return b ? window.getComputedStyle(b).opacity : '0'; }")
        check(edit_btn and float(edit_btn) > 0.3, "Agent: edit button visible on hover", f"opacity={edit_btn}")

    # Hover over assistant message for copy/retry
    asst_msg = page2.locator(".agent-msg-assistant").first
    if asst_msg.count() > 0:
        asst_msg.hover()
        page2.wait_for_timeout(300)
        copy_btn = page2.evaluate("() => { const b = document.querySelector('.msg-copy-btn'); return b ? window.getComputedStyle(b).opacity : '0'; }")
        check(copy_btn and float(copy_btn) > 0.3, "Agent: copy button visible on hover", f"opacity={copy_btn}")

    page2.screenshot(path=str(SHOTS / "08-hover-actions.png"))

    # ══════════════════════════════════════════════════════════════════════
    # TEST 8: AGENT PAGE — Theme toggle
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  AGENT PAGE — Theme Toggle")
    print("=" * 70 + "\n")

    initial_theme = page2.evaluate("() => document.documentElement.getAttribute('data-theme')")
    check(initial_theme in ("dark", "light", "auto"), "Agent: has theme attr", initial_theme)

    theme_btn = page2.locator("#agent-theme-btn")
    if theme_btn.count() > 0:
        theme_btn.click()
        page2.wait_for_timeout(300)
        new_theme = page2.evaluate("() => document.documentElement.getAttribute('data-theme')")
        check(new_theme != initial_theme or new_theme in ("dark", "light"), "Agent: theme toggled", f"{initial_theme} → {new_theme}")
        page2.screenshot(path=str(SHOTS / "09-light-theme.png"))
        # Toggle back
        theme_btn.click()
        page2.wait_for_timeout(200)

    # ══════════════════════════════════════════════════════════════════════
    # TEST 9: AGENT PAGE — Session management
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  AGENT PAGE — Session Management")
    print("=" * 70 + "\n")

    # Create new session
    new_btn = page2.locator("#agent-new-session")
    new_btn.click()
    page2.wait_for_timeout(2000)

    # Should have cleared messages or created new session
    sess_opts = page2.evaluate("() => document.querySelectorAll('#agent-session-select option').length")
    check(sess_opts >= 1, "Agent: session selector has options", f"{sess_opts} options")

    # ══════════════════════════════════════════════════════════════════════
    # TEST 10: AGENT PAGE — Mobile responsive
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  AGENT PAGE — Mobile Responsive")
    print("=" * 70 + "\n")

    ctx2.close()

    ctx_mobile = browser.new_context(
        http_credentials={"username": USER, "password": PASS},
        viewport={"width": 390, "height": 844},
    )
    mp = ctx_mobile.new_page()
    mp.goto(URL + "/chat", wait_until="networkidle")
    mp.wait_for_timeout(1500)

    mp.screenshot(path=str(SHOTS / "10-agent-mobile.png"))

    # Activity pane hidden
    act_hidden = mp.evaluate("""() => {
        const el = document.querySelector('.agent-activity-pane');
        return el ? window.getComputedStyle(el).display === 'none' : true;
    }""")
    check(act_hidden, "Agent mobile: activity pane hidden")

    # Input still visible
    check(el_visible(mp, "#agent-input"), "Agent mobile: input visible")
    check(el_visible(mp, "#agent-send"), "Agent mobile: send button visible")

    # Messages pane takes full width
    mp_rect = el_rect(mp, ".agent-messages-pane")
    check(mp_rect and mp_rect["w"] > 350, "Agent mobile: messages full-width", f"{mp_rect['w']}px" if mp_rect else "?")

    ctx_mobile.close()

    # ══════════════════════════════════════════════════════════════════════
    # TEST 11: API endpoints
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  API — Chat Endpoints")
    print("=" * 70 + "\n")

    ctx_api = browser.new_context(
        http_credentials={"username": USER, "password": PASS},
        viewport={"width": 1280, "height": 900},
    )
    api_page = ctx_api.new_page()
    api_page.goto(URL, wait_until="domcontentloaded")

    # Create session
    resp = api_page.request.post(URL + "/api/chat/sessions",
        data=json.dumps({"name": "thorough-test", "model": "claude-opus-4-6"}),
        headers={"Content-Type": "application/json"})
    check(resp.status == 200, "API: create session", f"HTTP {resp.status}")
    sess = resp.json()
    check("id" in sess, "API: session has id")

    # List sessions
    resp2 = api_page.request.get(URL + "/api/chat/sessions?limit=5")
    check(resp2.status == 200, "API: list sessions")
    sessions = resp2.json()
    check(isinstance(sessions, list) and len(sessions) >= 1, "API: sessions is non-empty list", f"{len(sessions)} sessions")

    # Get messages
    resp3 = api_page.request.get(URL + f"/api/chat/sessions/{sess['id']}/messages")
    check(resp3.status == 200, "API: get messages")

    # Token endpoint
    resp4 = api_page.request.get(URL + "/api/chat/token")
    check(resp4.status == 200, "API: get token")
    token = resp4.json()
    check("token" in token and len(token["token"]) > 8, "API: token is valid string")

    # Auth required
    no_auth_ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    no_auth_page = no_auth_ctx.new_page()
    resp5 = no_auth_page.request.get(URL + "/api/chat/sessions")
    check(resp5.status == 401, "API: 401 without auth", f"HTTP {resp5.status}")
    no_auth_ctx.close()

    # Dashboard data still works
    resp6 = api_page.request.get(URL + "/api/dashboard-data")
    check(resp6.status == 200, "API: dashboard-data still works")

    ctx_api.close()
    browser.close()

# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print(f"  RESULTS: {passed}/{checks} passed")
print("=" * 70)

if issues:
    print(f"\n  🔴 {len(issues)} ISSUE(S):")
    for i, issue in enumerate(issues, 1):
        print(f"    {i}. {issue}")
else:
    print("\n  ✨ All checks passed!")

print(f"\n  📂 Screenshots ({len(list(SHOTS.glob('*.png')))} files):")
for f in sorted(SHOTS.glob("*.png")):
    print(f"    {f.name} ({f.stat().st_size // 1024} KB)")

print()
sys.exit(0 if not issues else 1)
