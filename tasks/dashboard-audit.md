# Atlas Dashboard — Full Audit Report

**Date:** 2026-03-11
**Auditor:** Claude (pi agent)
**Scope:** All tabs (Trading, Research, Monitor), both themes (light/dark), desktop (1280px) + mobile (420px)
**Method:** Visual screenshots via Playwright, CSS/JS code review, console error capture

---

## 🔴 Critical Bugs

### 1. Performance chart invisible in light mode
**Location:** Trading tab → `/PERFORMANCE` section
**Severity:** Critical — users cannot see their equity curve

The equity chart canvas is completely blank in light mode. The chart draws lines using dark-mode-only colors (e.g. hardcoded green `#7fb858` on a `#eaeaea` background). The canvas rendering function doesn't re-read theme CSS variables, so switching themes leaves an empty 300px white rectangle.

**Evidence:**
- Dark mode: chart renders correctly with green (Atlas) and blue (SPY) lines
- Light mode: identical section header, legend, and range buttons shown — but the canvas area is pure white with no visible lines

**Root cause:** `drawEquityChart()` uses color constants or reads CSS variables only once at init, not per-frame.

---

### 2. Monitor tab errors on every load
**Location:** Monitor tab
**Severity:** Critical — entire tab is non-functional for static deploys

Console errors on every Monitor tab visit:
```
Monitor load failed: Error: HTTP 404
Live price fetch failed: HTTP 404
```

The tab tries to fetch `/api/prices` and `/api/monitor` which only exist when a live API server is running. When served as static files (the normal deploy), the tab shows only:
- Ceasefire probability widget (works, reads from static JSON)
- Empty `/ALERT FEED 0` section
- Empty `/TEMPLATES 0` section
- No position cards, no health data

**Root cause:** Monitor tab assumes a live API backend. No graceful fallback for static-file mode.

---

### 3. "LIVE" mode pill shows when not trading live
**Location:** Header → right side → red `● LIVE` pill
**Severity:** Critical — misleading safety indicator

The header displays a pulsing red dot with "LIVE" text, implying real-money trading is active. Trading is actually paused (research-only mode). A user or observer glancing at the dashboard would believe the system is executing live trades.

**Root cause:** The mode pill reads from `dashboard-data.json` which may still report `mode: "live"` from the last active config. No validation against actual broker connectivity or order activity.

---

## 🟡 Styling Issues

### 4. Light mode header is visually identical to dark mode
**Location:** Header bar in both themes
**Severity:** Medium — users can't tell which theme is active

Side-by-side comparison shows the header background, text colors, and pill styles are nearly identical between light and dark themes. The theme toggle buttons (☀ ▸ ☾) use the same grey tones in both modes. The header `background: var(--bg)` resolves to `#111111` (dark) vs `#eaeaea` (light), but the overall visual weight is the same due to the pills and text having insufficient contrast change.

**Fix:** Add stronger visual differentiation — white header bg in light mode, darker pill borders, invert the toggle button active state.

---

### 5. "Director" label collides with org-chart connector line
**Location:** Research tab → agent canvas → Director row
**Severity:** Medium — visual clutter in the hierarchy visualization

The Director's name label ("Director") renders at `deskY + deskH + 14` which places it at the same Y-coordinate as the horizontal org-chart connector bar. The blue status dot below the label also overlaps the line. The text is partially obscured.

**Fix:** Either render the Director label above the desk (to the right of the sprite), or push the org-chart lines lower (below the Director's status dot).

---

### 6. Org-chart connector lines are too faint
**Location:** Research tab → agent canvas → between Director and team
**Severity:** Low — hierarchy structure is unclear

The connector lines use `textTertiary` color at `0.45` opacity with `1.5px` width. In dark mode this is barely visible; in light mode it's nearly invisible against the light floor. The hierarchy isn't communicated effectively.

**Fix:** Increase opacity to `0.6`, use `text-secondary` color, or add a subtle shadow/glow to the lines.

---

### 7. Server rack prop is too small to be readable
**Location:** Research tab → agent canvas → left wall
**Severity:** Low — decorative element reads as visual noise

The "server rack" on the left wall renders as a few tiny green/teal dots (~6px per LED). At the canvas's scale, it looks like rendering artifacts rather than a recognizable object. It doesn't communicate "server" to the viewer.

**Fix:** Either enlarge it (16px+ LEDs with a visible rack frame) or remove it. A simpler "status lights" strip would be more readable at this scale.

---

### 8. Plant prop floats in mid-air
**Location:** Research tab → agent canvas → top-right corner
**Severity:** Low — breaks the spatial illusion

The potted plant sprite is positioned at the wall/floor transition zone but doesn't sit on any surface. It appears to float between the wall and floor. In a pixel-art room scene, objects should have clear surface attachment.

**Fix:** Position the plant on the floor (below the baseboard line) or on a wall shelf.

---

### 9. Research KPI values have inconsistent font weight
**Location:** Research tab → KPI strip (Experiments, Pass Rate, Strategies, Promoted)
**Severity:** Low — visual inconsistency

The inline CSS sets `.rkpi-value` to `font-weight: 800`, but `stripe-refresh.css` overrides most heavy weights to `500` for a lighter aesthetic. However, `.rkpi-value` is not included in the stripe-refresh override selectors, so it renders at 800 — making these four values visually heavier than every other number on the dashboard.

**Fix:** Add `.rkpi-value` to the stripe-refresh font-weight override list, or reduce to 500 in the inline style.

---

### 10. Live Activity feed is a wall of identical green
**Location:** Research tab → `/LIVE ACTIVITY` section
**Severity:** Medium — poor scannability

All 29 visible activity items show `PASS` in green with identical visual treatment. There's no visual distinction between strategy types (the badges all look the same at a glance), no grouping, no zebra striping. The relative timestamps ("8m ago", "11h ago") get stale if the page isn't refreshed.

**Fix:** Add alternating row backgrounds, group by strategy, or add a subtle color tint per strategy type. Show absolute timestamps alongside relative ones.

---

### 11. Leaderboard bars are invisible
**Location:** Research tab → `/STRATEGY LEADERBOARD` section
**Severity:** Medium — the primary visualization of strategy performance is broken

The leaderboard shows strategy names and Sharpe values, but the horizontal bar fills are invisible or nearly so. The `.lb-bar-wrap` background (`var(--surface)` = `transparent` in stripe-refresh) makes the bar track invisible, and the fill bar colors have insufficient contrast against the page background.

**Fix:** Give `.lb-bar-wrap` a solid border or visible background (e.g., `var(--surface-hover)`). Ensure bar fills use opaque strategy colors.

---

### 12. Discovery cards use emoji icons inconsistently
**Location:** Research tab → `/DISCOVERIES` section
**Severity:** Low — aesthetic inconsistency

Discovery items use emoji icons (📊, 🏆, ⚠️, 🔬, etc.) which clash with the monospace/geometric design language established by `stripe-refresh.css`. The left-border colors (green for high-impact, amber for medium) are applied inconsistently — some amber-worthy items have green borders.

**Fix:** Replace emojis with monochrome SVG icons or simple text prefixes. Audit the impact classification logic.

---

### 13. Mobile: table columns overflow and are clipped
**Location:** Trading tab at 420px viewport → Manual Holdings, Orders tables
**Severity:** Medium — data is hidden on mobile

At 420px width, multi-column tables (especially Manual Holdings with 8 columns) overflow horizontally. The `.table-scroll` wrapper adds a gradient fade on the right edge, but important columns like P&L and P&L% are completely hidden. Users must know to scroll right.

**Fix:** Either collapse less-important columns on mobile, use a card layout instead of a table below 700px, or add a visible "scroll →" indicator.

---

### 14. Research tab badge "4 ACTIVE" is ambiguous
**Location:** Tab nav → Research tab button
**Severity:** Low — confusing label

The Research tab shows a badge "4 ACTIVE" which represents the number of active agents (Atlas, Nova, Sage, Director). Users would more likely expect this to show running experiments or pending results. The green "ACTIVE" text competes visually with the tab label.

**Fix:** Change to show experiment count (e.g., "124 exp") or research queue depth. Or relabel as "4 agents".

---

### 15. Two-column layout (Positions + Plan) is lopsided
**Location:** Trading tab → `/OPEN POSITIONS` + `/TODAY'S PLAN` side-by-side
**Severity:** Low — wasted whitespace

The two-column grid has wildly different content heights. The positions section has 1 row (~60px of content); the plan section has entries + exits + risk summary + action buttons (~400px). The left column is 80% empty whitespace, creating visual imbalance.

**Fix:** Let the positions section stack above the plan section on single-column (like it does on mobile), or allow the plan section to span full width when positions are few.

---

## 🔵 Code Quality / Polish

### 16. Fragile CSS specificity chain
**Location:** `<style>` block vs `stripe-refresh.css`
**Severity:** Medium — maintenance hazard

The inline `<style>` block (600+ lines) is overridden by `stripe-refresh.css` (800+ lines) using `html body .class` selectors to win specificity. This creates a fragile chain where:
- Adding a new element requires updates in both files
- Specificity conflicts cause unexpected rendering (e.g., `.agent-floor-wrap` keeps `border-radius: 10px` from inline because stripe-refresh doesn't target it)
- Some properties have `!important` wars

**Fix:** Long-term: consolidate into one stylesheet with a single specificity strategy. Short-term: audit and add missing selectors to stripe-refresh.

---

### 17. Contradictory `table-scroll` overflow rules
**Location:** Inline `<style>` block
**Severity:** Low — causes tooltip clipping issues

```css
.table-scroll { overflow: visible !important; }  /* Line 1 */
.table-scroll { overflow-x: auto !important; }   /* Line 2 */
```

These two rules fight each other. The first sets ALL overflow to visible (for strategy tooltips). The second tries to restore horizontal scrolling. The result is unpredictable: vertical overflow may leak, and tooltips may still get clipped depending on browser.

**Fix:** Use `overflow-x: auto; overflow-y: visible;` in a single rule without `!important`.

---

### 18. ~100 lines of unused CSS
**Location:** Both `<style>` and `stripe-refresh.css`
**Severity:** Low — dead code

Classes defined but never used in the rendered HTML:
- `.research-hero`, `.research-card`, `.research-card-value`, `.research-card-label`
- `.insight-card`
- `.queue-bar`, `.bar-pass`, `.bar-fail`, `.bar-defer`
- `.badge-deferred`
- `.badge-running` (only used if an experiment is currently running — rare)

**Fix:** Audit and remove, or add a comment noting they're for future use.

---

### 19. Agent canvas doesn't re-render on theme change
**Location:** Research tab → agent floor canvas
**Severity:** Low — stale theme colors after switching

`_readThemeColors()` reads CSS computed values each frame (good), but `_resizeAgentCanvas()` is not called when the theme changes. The canvas pixel dimensions and DPR transform may be stale. More importantly, the wall/floor colors update per-frame but the props (whiteboard, plant, server rack) may use cached colors from init.

**Fix:** Call `_resizeAgentCanvas()` and force a full re-render on theme toggle.

---

### 20. No loading state for agent canvas
**Location:** Research tab → agent floor area
**Severity:** Low — blank rectangle on first load

When the Research tab first renders, the agent canvas shows a blank grey rectangle for 1-2 frames until `updateAgentFloor()` receives data. There's no skeleton, spinner, or "Loading agents..." text.

**Fix:** Draw a simple "Loading..." text or skeleton state in `drawAgentFloor()` when `f.agents.length === 0`.

---

### 21. Market clock dots don't differentiate proximity to open
**Location:** Header → market clocks
**Severity:** Low — missed information design opportunity

Both ASX and NYSE show identical amber dots with "Xh Ym to open". There's no visual distinction between "opening in 30 minutes" vs "opening in 16 hours". A color gradient (grey → amber → green as open approaches) would add useful information.

**Fix:** Calculate proximity percentage and interpolate dot color.

---

### 22. Plus-row grid markers at page bottom are pure noise
**Location:** Bottom of every tab → `+` markers in a grid
**Severity:** Low — visual clutter

The Stripe-inspired `+` grid markers appear at the very bottom of each tab's content, after all meaningful data. They serve no informational purpose and add ~30px of visual noise. In Stripe's design, these markers reinforce grid alignment — here they're orphaned at the page footer.

**Fix:** Remove them, or move them to section boundaries where they'd reinforce the grid.

---

### 23. Missing `<meta name="theme-color">`
**Location:** `<head>` section
**Severity:** Low — mobile browser chrome doesn't match

The HTML lacks `<meta name="theme-color">` which means mobile browsers (Chrome, Safari) show their default toolbar color instead of matching the dashboard's dark/light theme.

**Fix:** Add `<meta name="theme-color" content="#111111">` and update it dynamically on theme change.

---

## Priority Matrix

| # | Issue | Severity | Effort | Fix First? |
|---|-------|----------|--------|------------|
| 1 | Chart invisible in light mode | 🔴 Critical | Medium | ✅ |
| 3 | False "LIVE" indicator | 🔴 Critical | Low | ✅ |
| 2 | Monitor tab 404 errors | 🔴 Critical | Medium | ✅ |
| 11 | Leaderboard bars invisible | 🟡 Medium | Low | ✅ |
| 10 | Activity feed wall of green | 🟡 Medium | Low | |
| 5 | Director label/line overlap | 🟡 Medium | Low | ✅ |
| 13 | Mobile table overflow | 🟡 Medium | Medium | |
| 4 | Light/dark header identical | 🟡 Medium | Low | |
| 16 | CSS specificity chain | 🔵 Medium | High | |
| 9 | KPI font-weight inconsistency | 🟡 Low | Low | |
| 6 | Org-chart lines too faint | 🟡 Low | Low | |
| 7 | Server rack unreadable | 🟡 Low | Low | |
| 8 | Plant floats in air | 🟡 Low | Low | |
| 12 | Emoji icons in discoveries | 🟡 Low | Low | |
| 14 | Tab badge "4 ACTIVE" | 🟡 Low | Low | |
| 15 | Two-column lopsided | 🟡 Low | Medium | |
| 17 | table-scroll overflow conflict | 🔵 Low | Low | |
| 18 | Unused CSS (~100 lines) | 🔵 Low | Low | |
| 19 | Canvas theme-change re-render | 🔵 Low | Low | |
| 20 | No agent canvas loading state | 🔵 Low | Low | |
| 21 | Market clock dot colors | 🔵 Low | Low | |
| 22 | Plus-row grid noise | 🔵 Low | Low | |
| 23 | Missing theme-color meta | 🔵 Low | Low | |

---

## Recommended Fix Order

**Phase 1 — Critical (do now):**
1. Fix chart rendering in light mode (re-read theme colors per frame)
2. Fix LIVE indicator (show actual mode from config, or "RESEARCH" when no active orders)
3. Graceful fallback for Monitor tab when API unavailable

**Phase 2 — Visual polish (next session):**
4. Fix leaderboard bar visibility
5. Fix Director label / org-chart line overlap
6. Improve light mode header differentiation
7. Fix Research KPI font-weight

**Phase 3 — Code cleanup (backlog):**
8. Consolidate CSS specificity
9. Remove dead CSS
10. Fix table-scroll overflow contradiction
11. Add agent canvas loading state
