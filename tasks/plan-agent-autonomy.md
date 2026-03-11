# Plan: Increasing Agent Autonomy — Applying Mac Mini Agent Patterns to Atlas/Pi

**Date:** 2026-03-11
**Source:** [indydevdan Mac Mini Agent video](https://youtu.be/LOazLNQnB80)
**Status:** PLAN — awaiting review

---

## Core Ideas from the Video

The video demonstrates a Mac Mini running as a dedicated agent device with this architecture:

```
Trigger Layer (just commands / HTTP)
  → Listen Server (job queue, HTTP)
    → Agent Instance (Claude Code + skills)
      → Drive (tmux terminal orchestration)
      → Steer (macOS GUI control via Swift)
      → Apps, tools, CLIs
    → Results back (AirDrop / communication)
```

**Key principles:**
1. "When you increase your agent's autonomy, you increase your own"
2. "Build the system that builds the system"
3. "It's not about what you can do — it's what you can teach your agents to do for you"
4. "Agentic engineering is knowing what your agents are doing so well you don't have to look"
5. Agents need their own device/sandbox — no ceiling on capability
6. Just 2 skills + 2 CLIs drive the entire multi-device system
7. Spec-driven tasks with proof-of-work requirements
8. Agent cleans up after itself — full lifecycle management

---

## Current State: What We Already Have

| Capability | Our Equivalent | Gap |
|-----------|---------------|-----|
| Agent harness | Pi (claude-opus-4-6) | ✅ Already great |
| Terminal control | bash tool, subprocess | ✅ Direct (no tmux layer) |
| Visual proof / charts | matplotlib + Telegram send_photo | ✅ Tested, works |
| Trigger layer | Cron + systemd + Telegram bot | 🟡 No ad-hoc job dispatch |
| Job server | systemd services (static) | 🔴 No dynamic job queue |
| Communication back | Telegram bot (send_message) | 🟡 One-way, no rich results |
| Multi-agent | Swarm (parallel code mods), autoresearch | 🟡 Domain-specific only |
| Proof of work | Logs, journal.json | 🔴 No screenshots, no before/after |
| Self-management | healthz skill, autofix cron | 🟡 Reactive, not proactive |
| Spec-driven tasks | Pi skills, AGENTS.md | 🟡 Per-skill, no unified spec system |

---

## Proposed Enhancements (5 Phases)

### Phase 1: Telegram as Trigger Layer (the "Direct" + "Listen" equivalent)
**Effort: Medium | Impact: Very High**

The video uses an HTTP "listen" server + "direct" CLI. We already have a Telegram bot running 24/7. Extend it to be our job dispatch system.

**New Telegram commands:**
```
/task <prompt>           — Dispatch a Pi agent job with any prompt
/task @spec <spec_name>  — Run a named spec file (like video's send-to-cc)
/jobs                    — List active/recent jobs with status
/job <id>                — Get detailed status of a specific job
/kill <id>               — Kill a running job
/logs <id>               — Get last N lines of a job's output
```

**Implementation:**
- Add a `JobManager` class to `services/telegram_bot.py` (or new `services/job_server.py`)
- Job specs stored in `/root/atlas/jobs/` as YAML:
  ```yaml
  id: job_20260311_093000_a1b2
  status: running  # queued | running | done | failed
  prompt: "Run a full health check and fix any issues"
  skill: atlas-healthz
  pid: 12345
  tmux_session: job-a1b2
  started_at: 2026-03-11T09:30:00
  completed_at: null
  result_summary: null
  artifacts: []
  ```
- Each job spawns in its own tmux session:
  ```bash
  tmux new-session -d -s job-a1b2 \
    "pi --print --no-session --skill $SKILL 'prompt' > /tmp/job-a1b2.log 2>&1"
  ```
- Job monitor thread watches tmux sessions, updates status
- On completion: send result summary back via Telegram
- Named specs in `/root/atlas/specs/` (like video's `specs/update-hooks-mastery.md`)

**Files to create/modify:**
- `services/job_server.py` — JobManager class, tmux orchestration
- `services/telegram_bot.py` — Add /task, /jobs, /job, /kill, /logs handlers
- `specs/` directory — Named task specs

**Why this matters:** Right now, kicking off an agent task requires SSH + terminal. With this, you're one Telegram message away from dispatching any agent task from your phone, anywhere.

---

### Phase 2: Drive Skill — Terminal Orchestration via tmux
**Effort: Low | Impact: High**

The video's "Drive" app gives agents structured tmux control. Our agents can already run `bash` commands, but they can't:
- Spin up parallel terminal sessions
- Monitor long-running processes across sessions
- Orchestrate multi-agent workflows in separate terminals

**New Pi skill: `drive`**
```
skills/drive/
├── SKILL.md          — How to use tmux for multi-session work
├── drive.sh          — CLI wrapper for common tmux operations
└── templates/        — Pre-built session layouts
```

**Skill capabilities:**
```bash
drive new <name>              # Create new tmux session
drive send <name> "command"   # Send command to session
drive read <name>             # Read current output from session
drive list                    # List all active sessions
drive kill <name>             # Kill a session
drive wait <name> <pattern>   # Wait for output matching pattern
drive layout research         # Pre-built: 4-pane research layout
```

**SKILL.md teaches the agent:**
- When to use multiple sessions vs sequential commands
- How to monitor background work (send command, do other work, check back)
- Session naming conventions
- Cleanup discipline (always kill sessions when done)
- Observation loop: send → wait → read → decide

**Why this matters:** Currently our autoresearch.py manually orchestrates subprocesses. With a drive skill, ANY agent task can spin up parallel workers when it makes sense. The agent decides its own parallelism strategy.

---

### Phase 3: Proof-of-Work System
**Effort: Medium | Impact: High**

The video emphasizes agents PROVING their work — visual proof, logs, before/after comparisons. Our agents currently just say "done" and log to files.

**No browser-tools.** Headless Chrome is unreliable on this server. Instead:
matplotlib for charts, dashboard-data.json for metrics, Telegram send_photo for delivery.
All tested and confirmed working.

**Components:**

**3a. Chart Generation Engine** (`utils/charts.py`)
Matplotlib-based chart generator that reads `dashboard-data.json` and produces PNGs:
```python
def equity_chart(days=30) -> Path:
    """Generate equity curve PNG from dashboard data."""

def research_progress_chart() -> Path:
    """Experiments/day, pass rate trend, Sharpe progression."""

def strategy_comparison_chart(strategies: list) -> Path:
    """Side-by-side Sharpe/win-rate/drawdown bars for strategies."""

def before_after_chart(metric: str, before: dict, after: dict) -> Path:
    """Visual diff of metrics before and after a change."""
```
- All charts saved to `/root/atlas/artifacts/charts/`
- Consistent dark theme matching the dashboard aesthetic
- Designed for Telegram delivery (10:4 aspect ratio, 150 DPI, readable on phone)

**3b. Telegram Photo/Document Delivery** (`utils/telegram.py` additions)
```python
def send_photo(image_path: str, caption: str = "") -> bool:
    """Send a PNG/JPG to Telegram chat (our AirDrop equivalent)."""

def send_document(file_path: str, caption: str = "") -> bool:
    """Send any file as a Telegram document."""

def send_proof(job_id: str, summary: str, charts: list[Path],
               artifacts: list[Path] = None) -> bool:
    """Send a complete proof-of-work bundle: message + charts + files."""
```
Uses Telegram Bot API multipart upload (tested — works from this server).

**3c. Structured Proof Artifacts**
Every agent task that modifies state must produce a proof artifact:
```yaml
# /root/atlas/artifacts/proof-<job_id>.yaml
job_id: job_20260311_093000_a1b2
task: "Optimize mean_reversion parameters"
started_at: 2026-03-11T09:30:00
completed_at: 2026-03-11T09:45:00
changes_made:
  - file: config/active/sp500.json
    type: parameter_update
    before: {rsi_period: 14, oversold: 30}
    after: {rsi_period: 11, oversold: 25}
verification:
  - type: backtest
    result: "Sharpe improved 0.31 → 0.38"
  - type: chart
    path: artifacts/charts/before-after-20260311.png
  - type: test
    command: "python3 -m pytest tests/test_mean_reversion.py"
    result: "12 passed, 0 failed"
charts:
  - artifacts/charts/equity-curve.png
  - artifacts/charts/strategy-comparison.png
```

**3d. Job Completion Flow**
When a job finishes:
1. Generate relevant charts (equity curve, strategy comparison, etc.)
2. Build proof artifact YAML
3. Send to Telegram as a bundle:
   - Rich HTML summary message with key metrics
   - Charts as photos (inline, viewable on phone)
   - Proof YAML + logs as document attachments
   - Inline buttons: [Re-run] [Approve Changes]

**Why this matters:** Trust. "Agentic engineering is knowing what your agents are doing so well you don't have to look." Proof-of-work is how you build that trust.

---

### Phase 4: Spec-Driven Task System
**Effort: Low | Impact: Medium**

The video uses structured spec files (instructions + tasks + deliverables). We have Pi skills for specialized workflows, but no general-purpose spec system.

**Spec format:**
```markdown
# specs/weekly-reoptimize.md

## Instructions
- Time limit: 30 minutes
- If any strategy degrades Sharpe by >0.05, stop and alert
- Always take before/after dashboard screenshots

## Tasks
1. Run baseline backtest for all 7 strategies
2. Compare current Sharpe to last week's baseline
3. For any strategy with Sharpe decline >0.03, run targeted sweep
4. For improvements found, run OOS validation
5. Stage candidates but DO NOT promote (human approval required)

## Deliverables
- [ ] Proof artifact with before/after metrics table
- [ ] Dashboard screenshot
- [ ] Summary sent to Telegram with improvement/decline details
- [ ] Any candidates staged in config/candidates/
```

**Named specs library:**
```
specs/
├── daily-health-check.md
├── weekly-reoptimize.md
├── research-deep-dive.md     — Focus on one strategy for an hour
├── post-incident-review.md   — Analyze what went wrong
├── data-refresh.md           — Full data pipeline refresh
├── performance-report.md     — Weekly P&L and research metrics
└── system-upgrade.md         — Update packages, test, verify
```

**Telegram integration:**
```
/task @spec weekly-reoptimize     → loads spec, dispatches agent
/task @spec daily-health-check    → loads spec, dispatches agent
```

**Why this matters:** Repeatable, auditable, delegatable. Write the spec once, run it forever. The video's key insight: "template your engineering."

---

### Phase 5: Self-Managing Agent Device
**Effort: High | Impact: Very High (long-term)**

The video's philosophy: "I'm never going to touch this device. If something's wrong, I teach my agent to fix it." We're partway there with healthz, but not fully autonomous.

**5a. Proactive Self-Healing**
Currently healthz runs on cron (reactive). Make it continuous:
- New systemd service: `atlas-watchdog.service`
- Runs every 15 min: check all services, disk, memory, data freshness
- On issue detected: attempt auto-fix, then notify
- Escalation: auto-fix → Telegram alert → halt if critical
- Agent can restart its own services, clear caches, rotate logs

**5b. Agent-Driven Maintenance**
```
/task "Check system health and fix anything broken"
```
The agent should be able to:
- `systemctl restart atlas-autoresearch` if it's stuck
- Clear stale lock files
- Rotate oversized logs
- Update pip packages if needed
- Run database maintenance (vacuum sqlite, clean old snapshots)
- Fix broken cron jobs
- Diagnose why a service failed and fix it

**5c. Observability Dashboard**
Extend the existing dashboard with an "Agent Operations" tab:
- Active jobs and their status
- Recent task history with proof artifacts
- System health timeline
- Notification history
- Agent uptime and performance metrics

**Why this matters:** The end state is a server you never SSH into. Everything is managed through Telegram commands that dispatch agent tasks. The agent IS the sysadmin.

---

## Implementation Priority Matrix

| Phase | Effort | Impact | Dependencies | Priority |
|-------|--------|--------|-------------|----------|
| Phase 1: Telegram Trigger Layer | Medium | Very High | None | **🔴 Do First** |
| Phase 2: Drive Skill (tmux) | Low | High | None | **🔴 Do First** |
| Phase 4: Spec System | Low | Medium | Phase 1 | **🟡 Do Second** |
| Phase 3: Proof-of-Work | Medium | High | Phase 1, 2 | **🟡 Do Second** |
| Phase 5: Self-Management | High | Very High | Phase 1-4 | **🟢 Do Third** |

**Recommended execution order:**
1. **Phase 2** (Drive skill) — smallest, unblocks everything else, 1-2 hours
2. **Phase 1** (Telegram job dispatch) — biggest bang for buck, 4-6 hours
3. **Phase 4** (Spec system) — just a directory + convention, 1 hour
4. **Phase 3** (Proof-of-work) — builds on 1+2, 3-4 hours
5. **Phase 5** (Self-management) — ongoing evolution, weeks

---

## What This Looks Like When Done

**Today (current state):**
```
You → SSH → terminal → type commands → read output → decide → type more
You → Telegram → /status (read-only) → /plan → approve/reject
Cron → fixed schedule → pi-cron.sh → predefined scripts
```

**After (target state):**
```
You → Telegram → "/task optimize mean_reversion" → agent runs → proof delivered → done
You → Telegram → "/task @spec weekly-reoptimize" → spec executed → results delivered
You → Telegram → "/jobs" → see all active work → "/logs job123" → real-time output
Agent → detects degradation → self-heals → notifies you → proof of fix
Agent → completes research → screenshots dashboard → sends digest → you review on phone
```

One Telegram message. Anywhere. From your phone. Agent handles everything.
That's the autonomy increase the video is talking about.

---

## Key Differences from the Video's macOS Approach

| Video (macOS) | Our Approach (Linux headless) |
|---------------|------------------------------|
| Steer (Swift GUI control) | matplotlib charts + dashboard-data.json (no browser needed) |
| Drive (tmux wrapper) | Drive skill (same concept, Pi-native) |
| AirDrop results | Telegram send_photo + send_document (tested, works) |
| Just file (command runner) | Telegram /task commands + named specs |
| Listen HTTP server | Telegram bot (already running 24/7) |
| Direct CLI client | Telegram client (your phone) |
| Screen sharing to observe | Dashboard + /jobs + /logs via Telegram |
| macOS accessibility tree | Programmatic data (JSON/YAML), bash for CLI |

The core architecture is identical. Our "device" is a Hetzner Linux server.
Our "GUI" is matplotlib charts generated from dashboard data.
Our "AirDrop" is Telegram photo/document delivery.
Our "just commands" are Telegram /task commands.
The agent autonomy principles are platform-independent.
