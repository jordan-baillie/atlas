# Research Tab Redesign — Pixel Agents Style

## Vision
Replace the current research tab (8 collapsible sections, data-heavy tables, overwhelming) with a **simple, visual, alive** view inspired by pixel-agents. The core question the tab answers: "What are my agents doing, and what have they found?"

## Design Concept

### The Layout (top to bottom)

```
┌─────────────────────────────────────────────────────────┐
│  ╔═══════════════════════════════════════════════════╗   │
│  ║  AGENT FLOOR — Canvas with pixel characters      ║   │
│  ║                                                   ║   │
│  ║  🧑‍💻 typing    🧑‍💻 reading    🚶 walking   💤 idle  ║   │
│  ║  "Testing MR"  "Scanning"   (between)  (no job)  ║   │
│  ║                                                   ║   │
│  ╚═══════════════════════════════════════════════════╝   │
│                                                          │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐   │
│  │ 176      │ │ 39%      │ │ 25       │ │ 3        │   │
│  │ experiments  pass rate │ │ strategies  promoted  │   │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘   │
│                                                          │
│  ┌─ Live Activity ──────────────────────────────────┐   │
│  │ ● 2m ago  momentum_breakout  PASS  Sharpe 0.64   │   │
│  │ ○ 5m ago  trend_following    FAIL  Sharpe -0.12   │   │
│  │ ● 8m ago  bb_squeeze         PASS  Sharpe 0.58   │   │
│  │ ...                                               │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  ┌─ Discoveries ────────────────────────────────────┐   │
│  │ 💡 Volume filter improves MR: Sharpe -0.02→0.38  │   │
│  │ 💡 VIX filter hurts MR-heavy portfolios          │   │
│  │ 💡 Fee drag kills all strategies at $4K equity    │   │
│  │ ⭐ Top strategy: Momentum Breakout (Sharpe 0.69)  │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  ┌─ Leaderboard (compact) ──────────────────────────┐   │
│  │ 1. Lower Band Reversion  ████████░░  0.71        │   │
│  │ 2. Mean Reversion        ███████░░░  0.64        │   │
│  │ 3. Trend Following       ██████░░░░  0.62        │   │
│  │ 4. Opening Gap           ██████░░░░  0.62        │   │
│  │ ...                                               │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

### The Agent Floor (the hero element)

A `<canvas>` element at the top of the research tab showing pixel-art agent characters.

**Characters represent actual agents/processes:**
- **Autoresearch daemon** — main researcher, always present
- **Sweep workers** — appear when sweep is running (up to 6)
- **Job agents** — appear when `/task` jobs are running (up to 3)

**Character states (animations):**
- **typing** — agent is actively running experiments / executing
- **reading** — agent is analyzing results / scanning data
- **idle/walking** — agent is between tasks, wandering
- **sleeping** — process is stopped/dead
- **celebrating** — just found a pass/promotion (brief animation)

**Speech bubbles** showing what each agent is doing:
- "Testing momentum_breakout..." (current experiment)
- "Found: Sharpe 0.69! ✓" (recent result)
- "Waiting for data..." (blocked)
- "zzz" (sleeping/offline)

**Implementation:**
- Pure `<canvas>` + JS, no dependencies
- 16x16 pixel sprites defined as 2D arrays (like pixel-agents does)
- 4-frame walk cycle, 2-frame typing cycle, idle bob
- Dark floor tiles matching dashboard bg
- Simple A* or random wander for movement
- Characters rendered at 3x or 4x scale (48px or 64px tall)

### What We Remove (vs current)
- ❌ Lifecycle Pipeline visualization (confusing)
- ❌ Coverage Matrix (data-heavy, rarely useful)
- ❌ Queue Overview table (agent handles this)
- ❌ Hypotheses section (merge into Discoveries)
- ❌ Patterns section (merge into Discoveries)
- ❌ 15-column leaderboard table (replace with visual bars)
- ❌ All collapsible sections (everything visible at a glance)

### What We Keep (simplified)
- ✅ KPI strip (4 metrics instead of 6)
- ✅ Activity feed (simplified, newest first, last 15)
- ✅ Leaderboard (compact bar chart, top 10 only)
- ✅ Engine status (encoded in the agent characters themselves)

### What We Add
- ✅ Agent Floor canvas (the pixel-agents hero)
- ✅ Discoveries section (consolidated patterns + hypotheses + top results)
- ✅ Job status (active /task jobs shown as characters)

## Data Requirements

### New data in dashboard-data.json → `research.agents[]`

```json
{
  "research": {
    "agents": [
      {
        "id": "autoresearch",
        "name": "Researcher",
        "status": "typing",       // typing|reading|idle|sleeping
        "task": "Testing momentum_breakout lookback=20",
        "since": "2026-03-11T01:40:00Z",
        "experiments_done": 14,
        "last_result": { "verdict": "pass", "strategy": "momentum_breakout", "sharpe": 0.64 }
      },
      {
        "id": "job-0311_112044",
        "name": "Job Agent",
        "status": "typing",
        "task": "Running #daily-health-check",
        "since": "2026-03-11T11:20:00Z"
      }
    ],
    "discoveries": [
      {
        "text": "Volume filter improves MR signal quality",
        "type": "pattern",        // pattern|hypothesis|record
        "impact": "high",
        "detail": "Sharpe -0.02 → 0.38 with 1.5x volume threshold"
      }
    ],
    ...existing fields...
  }
}
```

### generate_data.py changes
- Build `agents[]` from:
  - `/tmp/autoresearch-heartbeat.json` status
  - `/tmp/research-daemon-heartbeat.json` status  
  - `jobs/*.json` running jobs via `job_server.get_manager().list_jobs()`
- Build `discoveries[]` from:
  - Existing `patterns[]` (already generated)
  - Existing `hypotheses[]` (already generated)
  - Top results from `research/best/` files

## File Plan

### Files to modify:
1. **`dashboard/templates/index.html`**
   - Replace research tab HTML (lines 1033-1110)
   - Replace research CSS (line ~381+)
   - Replace `renderResearch()` function (line ~3036+)
   - Add canvas pixel engine (new ~200 line section)
   - Add sprite data (new ~100 line section)

2. **`dashboard/generate_data.py`**
   - Add `_build_agents()` → `research.agents[]`
   - Add `_build_discoveries()` → `research.discoveries[]`
   - Keep existing fields for backward compat

### Files untouched:
- `utils/charts.py` — already done, separate concern
- `services/telegram_bot.py` — doesn't touch research tab
- `stripe-refresh.js/css` — separate file, not research-related

## Sprite Design

Simple 16x16 characters using the dashboard's color palette:

```
Agent colors (mapped to dashboard vars):
  Researcher:  --green (#7fb858) body, --text hair
  Job Agent:   --blue (#5a93c0) body, --text hair  
  Sweep Worker: --amber (#d4a84a) body, --text hair
  
Floor: --surface (#151310)
Desk:  --surface-raised (#231f18) with --border edge
```

4 sprite states × 4 directions × 2 frames = 32 sprites per character
For simplicity: front-facing only, 4 states × 2 frames = 8 sprites

## Phased Implementation

### Phase A: Data layer (generate_data.py)
- Add `_build_agents()` and `_build_discoveries()`
- Test data output

### Phase B: Canvas engine + sprites (index.html)
- Pixel sprite definitions
- Canvas rendering loop
- Character state machine (typing/reading/idle/sleeping)
- Speech bubbles
- Floor/desk rendering

### Phase C: Simplified sections
- New KPI strip (4 metrics)
- New activity feed (compact)
- New discoveries section
- New leaderboard bars
- Remove old sections

### Phase D: Wire data → canvas
- Map `research.agents[]` to canvas characters
- Auto-update on data refresh
- Celebration animation on new pass/promotion
