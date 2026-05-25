# Atlas TUI Widget — Runbook

## Overview

The `atlas-tui-widget` Pi extension is the **single consolidated dashboard** — it renders
a compact live-activity panel above the Pi editor. Clean, phase-aware, orchestrator-ready.

```
◆ idle  │  agents 0  │  tools 17  │  errors 0  │  elapsed 02:34
──────────────────────────────────────────────────────────────────
  ✓ Read              memory/SUMMARY.md                      200ms
  ✓ Bash              git status --short                      89ms
  → subagent          researcher — deep analysis              1.2s…
  ✗ Write             tasks/todo.md                           12ms
```

Header fields:
- **phase** — `idle` (no in-flight tools) or `working` (tools running)
- **agents** — number of currently active delegation tools (`subagent` / `swarm`)
- **tools** — total tool calls this session
- **errors** — total errors this session
- **elapsed** — session wall-clock time

Activity feed uses `→` for running delegation (agent) tools vs `⟳` for regular tools.

Footer shows only: **context%  ·  tokens  ·  model  ·  thinking** — no extra clutter.

---

## What was consolidated (2026-05-25)

This widget replaces three previously separate TUI pieces:
- **`ceo-dashboard`** widget (`ceo-context/index.ts`) — removed; state tracking continues for context injection
- **`ceo-board-status`** footer entry (`ceo-context/index.ts`) — removed
- **`atlas-tui` setStatus** footer entry (`atlas-tui-widget/index.ts`) — removed
- **`[AGENTS]` footer counter** (`projects/footer.ts`) — removed

One dashboard above the editor. Footer stays clean.

---

## Commands

| Command             | Effect                                            |
|---------------------|---------------------------------------------------|
| `/atlas-tui`        | Toggle widget on/off                              |
| `/atlas-tui reset`  | Clear all session stats and restart elapsed timer |
| `/reload`           | Hot-reload the extension (picks up code changes)  |

---

## Enable / Disable

The widget auto-mounts on `session_start`. If you want it permanently disabled, remove the
extension from the pi-package registration (see **Package Wiring** below).

To hide for the current session only, run `/atlas-tui`.

---

## Package Wiring

The extension is registered in `pi-package/atlas-ops/package.json`:

```json
"pi": {
  "extensions": [
    ...
    "./extensions/atlas-tui-widget/src"
  ]
}
```

This means it loads automatically whenever the atlas-ops pi-package is active (`pi` from `/root/atlas`).

---

## Architecture

```
atlas-tui-widget/
  src/
    index.ts     Extension entry point (Pi runtime wiring)
    core.ts      Pure logic + state management (testable without Pi)
  tests/
    verify.ts    Standalone verification script (no Pi required)
```

**Pure functions** (exported for testing):

| Function | Purpose |
|---|---|
| `createState()` | Fresh `TuiState` with session timer started |
| `renderWidget(state, theme, width, widthFns)` | Returns `string[]`, each line ≤ `width`; `widthFns` is injected so tests can mock ANSI-aware truncation |
| `isDelegationTool(toolName)` | True for `subagent`, `swarm`, `delegate*` |
| `rowIcon(status, isDelegation)` | `→` for running delegation, otherwise `✓`/`✗`/`⟳` |
| `summarizeArgs(args)` | Extract relevant arg from tool call params |
| `fmtDuration(ms)` | Human-readable duration |
| `statusColor(status)` | Map status → theme color name |
| `statusIcon(status)` | Map status → `✓`/`✗`/`⟳` |

**Events subscribed:**

| Event | Action |
|---|---|
| `session_start` | Reset state, mount widget |
| `session_shutdown` | Unmount widget |
| `agent_start` / `agent_end` | Request re-render |
| `tool_execution_start` | Add running entry, increment totals |
| `tool_execution_end` | Move to completed, record duration |

**Delegation detection:** `subagent`, `swarm`, and any tool starting with `delegate` increment
the `delegations` counter and receive `→` icon while running.

**Memory bounds:** `recentActivity` is capped at `MAX_ACTIVITY = 5` entries (oldest evicted via
`shift()`). `activeTools` is defensively capped by `capActiveTools()` (default `4 × MAX_ACTIVITY`
entries) to guard against missed `tool_execution_end` events. Rendered feed always capped at 5 rows.

---

## Testing

Run the verification script (no Pi session required):

```bash
cd /root/atlas/pi-package/atlas-ops
npm run verify-tui

# Or directly:
npx tsx extensions/atlas-tui-widget/tests/verify.ts
```

Tests cover (52 total):
1. **Width-safety** — every rendered line ≤ requested width, for widths 40–120
2. **Header format** — phase idle/working, agents count, tools, errors, elapsed labels
3. **Status/color** — `statusColor()`, `statusIcon()`, `rowIcon()`, `isDelegationTool()` mappings; widget content includes correct icons
4. **Bounded memory** — `recentActivity` never exceeds `MAX_ACTIVITY`; feed capped at `MAX_ACTIVITY` rows
5. **FIFO eviction** — oldest entries dropped first
6. **summarizeArgs** — path priority, newline stripping, fallback
7. **fmtDuration** — ms/s/m:ss formatting
8. **`capActiveTools`** — evicts oldest entries when limit exceeded; no-op under limit

---

## TypeScript Check

```bash
cd /root/atlas/pi-package/atlas-ops
npx tsc --noEmit
```

Extensions load via jiti (no compilation step), but tsc catches type errors early.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Widget not appearing | Check `ctx.hasUI` — widget is suppressed in `-p`/JSON mode |
| Widget disappeared | Run `/atlas-tui` to re-enable |
| Type errors on `@mariozechner/pi-tui` | Pi resolves this via its own node_modules at load time |
| All tests fail | Ensure Node ≥ 18 and `npx tsx` is available |

---

## Future Improvements

- Elastic agent orchestrator: show total-delegated alongside active agents (`agents 2/5`)
- Token / cost metrics from `ctx.sessionManager.getBranch()`
- Collapsible widget (keyboard shortcut to hide without `/atlas-tui`)
