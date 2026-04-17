"""Playwright visual test — captures React dashboard screenshots."""
import asyncio
import json
import os
from pathlib import Path

async def test():
    from playwright.async_api import async_playwright

    screenshots_dir = Path("/root/atlas/tests/screenshots")
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    secrets_path = Path("/root/.atlas-secrets.json")
    if secrets_path.exists():
        secrets = json.loads(secrets_path.read_text())
        username = secrets.get("dashboard_user", "admin")
        password = secrets.get("dashboard_pass", "")
    else:
        username = "admin"
        password = ""

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            http_credentials={"username": username, "password": password},
            viewport={"width": 1920, "height": 1080},
        )
        page = await ctx.new_page()

        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # Portfolio tab (default view)
        print("Loading portfolio tab...")
        await page.goto("http://localhost:8899/", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)
        await page.screenshot(path=str(screenshots_dir / "react_01_portfolio.png"), full_page=True)
        print(f"  Saved react_01_portfolio.png")

        # Finance tab
        print("Looking for Finance tab...")
        fin = await page.query_selector('button:has-text("Finance")')
        if fin:
            await fin.click()
            await asyncio.sleep(3)
            await page.screenshot(path=str(screenshots_dir / "react_02_finance.png"), full_page=True)
            print(f"  Saved react_02_finance.png")
        else:
            print("  Finance tab not found, trying alternative selectors...")
            # Try other possible selectors
            for sel in ['[data-tab="finance"]', 'text=Finance', 'button >> text=Finance']:
                el = await page.query_selector(sel)
                if el:
                    await el.click()
                    await asyncio.sleep(3)
                    await page.screenshot(path=str(screenshots_dir / "react_02_finance.png"), full_page=True)
                    print(f"  Saved react_02_finance.png via {sel}")
                    break
            else:
                print("  Could not find Finance tab with any selector")

        # Mobile viewport
        print("Testing mobile viewport...")
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.goto("http://localhost:8899/", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)
        await page.screenshot(path=str(screenshots_dir / "react_03_mobile.png"), full_page=True)
        print(f"  Saved react_03_mobile.png")

        print(f"\nConsole errors: {len(errors)}")
        for e in errors[:10]:
            print(f"  ❌ {e}")

        await browser.close()

    # List saved screenshots
    print("\nScreenshots saved:")
    for f in sorted(screenshots_dir.glob("react_*.png")):
        size_kb = f.stat().st_size / 1024
        print(f"  {f.name} ({size_kb:.1f} KB)")

if __name__ == "__main__":
    asyncio.run(test())
