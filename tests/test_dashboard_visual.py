import asyncio
from playwright.async_api import async_playwright

async def test_dashboard():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        
        context = await browser.new_context(
            http_credentials={'username': 'atlas', 'password': 'Kennycleo.240597'},
            viewport={'width': 1920, 'height': 1080}
        )
        page = await context.new_page()
        
        # Collect console errors
        errors = []
        page.on('console', lambda msg: errors.append(f"{msg.type}: {msg.text}") if msg.type == 'error' else None)
        page.on('pageerror', lambda err: errors.append(f"PAGE ERROR: {err}"))
        
        # 1. Load Portfolio tab (default)
        print("=== Loading Portfolio Tab ===")
        await page.goto('http://localhost:8899/', wait_until='networkidle', timeout=30000)
        await asyncio.sleep(4)  # Wait for animations + data load
        await page.screenshot(path='tests/screenshots/01_portfolio_tab.png', full_page=True)
        print("Portfolio screenshot saved")
        
        # Check summary strip
        summary = await page.query_selector('.summary-strip')
        print(f"  Summary strip: {'FOUND' if summary else 'MISSING'}")
        stats = await page.query_selector_all('.summary-strip .stat')
        print(f"  Stats in strip: {len(stats)}")
        for s in stats:
            label = await s.query_selector('.stat-label')
            value = await s.query_selector('.stat-value')
            l_text = await label.text_content() if label else '?'
            v_text = await value.text_content() if value else '?'
            print(f"    {l_text}: {v_text}")
        
        # Check canvases on portfolio tab
        canvases = await page.query_selector_all('canvas')
        print(f"  Total canvases on page: {len(canvases)}")
        
        equity_canvas = await page.query_selector('#equity-canvas')
        if equity_canvas:
            box = await equity_canvas.bounding_box()
            print(f"  Equity canvas: {box['width']:.0f}x{box['height']:.0f}" if box else "  Equity canvas: no bounding box")
            # Check if canvas has content (Chart.js uses webgl sometimes, so check 2d)
            is_blank = await page.evaluate('''() => {
                const canvas = document.getElementById('equity-canvas');
                if (!canvas) return 'no-canvas';
                const ctx = canvas.getContext('2d');
                if (!ctx) return 'no-2d-context';
                try {
                    const data = ctx.getImageData(0, 0, Math.min(canvas.width, 100), Math.min(canvas.height, 100)).data;
                    const nonZero = Array.from(data).filter(v => v !== 0).length;
                    return nonZero === 0 ? 'BLANK' : 'HAS_CONTENT (' + nonZero + ' non-zero values)';
                } catch(e) { return 'error: ' + e.message; }
            }''')
            print(f"  Equity canvas content: {is_blank}")
        else:
            print("  Equity canvas: MISSING")
        
        regime_canvas = await page.query_selector('#regime-timeline-canvas')
        if regime_canvas:
            box = await regime_canvas.bounding_box()
            print(f"  Regime timeline canvas: {box['width']:.0f}x{box['height']:.0f}" if box else "  Regime canvas: no box")
            is_blank = await page.evaluate('''() => {
                const canvas = document.getElementById('regime-timeline-canvas');
                if (!canvas) return 'no-canvas';
                const ctx = canvas.getContext('2d');
                if (!ctx) return 'no-2d-context';
                try {
                    const data = ctx.getImageData(0, 0, Math.min(canvas.width, 100), Math.min(canvas.height, 100)).data;
                    const nonZero = Array.from(data).filter(v => v !== 0).length;
                    return nonZero === 0 ? 'BLANK' : 'HAS_CONTENT (' + nonZero + ' non-zero values)';
                } catch(e) { return 'error: ' + e.message; }
            }''')
            print(f"  Regime canvas content: {is_blank}")
        else:
            print("  Regime timeline canvas: MISSING")
        
        # Check skeletons still visible
        skeletons = await page.query_selector_all('.skeleton')
        visible_skeletons = 0
        for sk in skeletons:
            vis = await sk.is_visible()
            if vis:
                visible_skeletons += 1
        print(f"  Visible skeletons: {visible_skeletons} (should be 0 after load)")
        
        # Check animate-in elements
        animated = await page.query_selector_all('.animate-in')
        print(f"  Elements with .animate-in: {len(animated)}")
        
        # Tab indicator
        indicator = await page.query_selector('.tab-indicator')
        if indicator:
            box = await indicator.bounding_box()
            style = await page.evaluate('''(el) => {
                const s = getComputedStyle(el);
                return { transform: s.transform, width: s.width, opacity: s.opacity, background: s.background };
            }''', indicator)
            print(f"  Tab indicator box: {box}")
            print(f"  Tab indicator style: {style}")
        else:
            print("  Tab indicator: MISSING")
        
        # Regime indicator
        regime_dot = await page.query_selector('#regime-dot')
        regime_label = await page.query_selector('#regime-label')
        if regime_label:
            text = await regime_label.text_content()
            print(f"  Regime label: '{text}'")
        
        # 2. Switch to Finance tab
        print("\n=== Switching to Finance Tab ===")
        finance_btn = await page.query_selector('[data-tab="finance"]')
        if finance_btn:
            await finance_btn.click()
            await asyncio.sleep(3)
            await page.screenshot(path='tests/screenshots/02_finance_tab.png', full_page=True)
            print("Finance screenshot saved")
            
            pace_canvas = await page.query_selector('#pace-canvas')
            if pace_canvas:
                box = await pace_canvas.bounding_box()
                print(f"  Pace canvas: {box['width']:.0f}x{box['height']:.0f}" if box else "  Pace canvas: no box")
                is_blank = await page.evaluate('''() => {
                    const canvas = document.getElementById('pace-canvas');
                    if (!canvas) return 'no-canvas';
                    const ctx = canvas.getContext('2d');
                    if (!ctx) return 'no-2d-context';
                    try {
                        const data = ctx.getImageData(0, 0, Math.min(canvas.width, 100), Math.min(canvas.height, 100)).data;
                        const nonZero = Array.from(data).filter(v => v !== 0).length;
                        return nonZero === 0 ? 'BLANK' : 'HAS_CONTENT (' + nonZero + ' non-zero values)';
                    } catch(e) { return 'error: ' + e.message; }
                }''')
                print(f"  Pace canvas content: {is_blank}")
            else:
                print("  Pace canvas: MISSING")
            
            # Check finance section elements
            fin_sections = await page.query_selector_all('.fin-section')
            print(f"  Finance sections: {len(fin_sections)}")
            
            # Tab indicator position should have moved
            if indicator:
                box2 = await indicator.bounding_box()
                print(f"  Tab indicator (after Finance click): {box2}")
        else:
            print("  Finance tab button: MISSING")
        
        # 3. Switch to Research tab
        print("\n=== Switching to Research Tab ===")
        research_btn = await page.query_selector('[data-tab="research"]')
        if research_btn:
            await research_btn.click()
            await asyncio.sleep(3)
            await page.screenshot(path='tests/screenshots/03_research_tab.png', full_page=True)
            print("Research screenshot saved")
            
            sharpe = await page.query_selector('#sharpe-chart')
            if sharpe:
                box = await sharpe.bounding_box()
                print(f"  Sharpe chart: {box['width']:.0f}x{box['height']:.0f}" if box else "  Sharpe chart: no box")
                is_blank = await page.evaluate('''() => {
                    const canvas = document.getElementById('sharpe-chart');
                    if (!canvas) return 'no-canvas';
                    const ctx = canvas.getContext('2d');
                    if (!ctx) return 'no-2d-context';
                    try {
                        const data = ctx.getImageData(0, 0, Math.min(canvas.width, 100), Math.min(canvas.height, 100)).data;
                        const nonZero = Array.from(data).filter(v => v !== 0).length;
                        return nonZero === 0 ? 'BLANK' : 'HAS_CONTENT (' + nonZero + ' non-zero values)';
                    } catch(e) { return 'error: ' + e.message; }
                }''')
                print(f"  Sharpe chart content: {is_blank}")
            else:
                print("  Sharpe chart: MISSING")
            
            exp_table = await page.query_selector('#experiments-table')
            if exp_table:
                rows = await exp_table.query_selector_all('tbody tr')
                print(f"  Experiments table rows: {len(rows)}")
            else:
                print("  Experiments table: MISSING")
            
            strategy_cards = await page.query_selector_all('.research-strategy-card')
            print(f"  Research strategy cards: {len(strategy_cards)}")
            
            # Tab indicator position for research
            if indicator:
                box3 = await indicator.bounding_box()
                print(f"  Tab indicator (Research): {box3}")
        else:
            print("  Research tab button: MISSING")
        
        # 4. CSS Animation system check
        print("\n=== Animation System Check ===")
        anim_vars = await page.evaluate('''() => {
            const s = getComputedStyle(document.documentElement);
            return {
                ease: s.getPropertyValue('--ease').trim(),
                animDuration: s.getPropertyValue('--anim-duration').trim(),
                animStagger: s.getPropertyValue('--anim-stagger').trim(),
                animEase: s.getPropertyValue('--anim-ease').trim(),
            };
        }''')
        print(f"  CSS animation vars: {anim_vars}")
        
        # Count elements with active CSS animations
        active_anims = await page.evaluate('''() => {
            let count = 0;
            let names = {};
            document.querySelectorAll('*').forEach(el => {
                const s = getComputedStyle(el);
                if (s.animationName && s.animationName !== 'none') {
                    count++;
                    s.animationName.split(',').forEach(n => {
                        n = n.trim();
                        names[n] = (names[n] || 0) + 1;
                    });
                }
            });
            return { count, names };
        }''')
        print(f"  Active animations: {active_anims}")
        
        # 5. Data load check - did the API calls succeed?
        print("\n=== Data/API Check ===")
        data_check = await page.evaluate('''() => {
            return {
                equity_text: document.getElementById('stat-equity')?.textContent,
                today_text: document.getElementById('stat-today')?.textContent,
                positions_text: document.getElementById('stat-positions')?.textContent,
                margin_text: document.getElementById('stat-margin')?.textContent,
                regime_label: document.getElementById('regime-label')?.textContent,
            };
        }''')
        print(f"  Stat values: {data_check}")
        
        # 6. Console errors
        print(f"\n=== Console Errors: {len(errors)} ===")
        for err in errors[:20]:
            print(f"  {err}")
        
        if not errors:
            print("  (none)")
        
        print("\n=== DONE ===")
        await browser.close()

asyncio.run(test_dashboard())
