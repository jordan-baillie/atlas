# Meta-Skill Library: Applying "One Skill to Unlock Them All" to Atlas

**Date:** 2026-03-17  
**Source:** Indie Dev Dan — Meta-skill coordination for multi-codebase engineering  
**Author:** Pi Agent  

---

## Executive Summary

The video presents a pattern called the **"Library" meta-skill** — a single skill that acts as an index/registry pointing to all other skills, agents, and prompts across your codebases. Think `package.json` but for agentic capabilities. Atlas already has a sophisticated skill architecture via its pi-package, but the **multi-repo sprawl** and **duplication problems** described in the video are actively present in our environment. This report maps the concepts to Atlas's current state and identifies concrete efficiency gains.

---

## 1. Current State Audit: Where Atlas Stands

### What We Have (58 SKILL.md files across the system)

| Location | Count | Type | Purpose |
|----------|-------|------|---------|
| `atlas/pi-package/atlas-ops/skills/` | 12 | Domain-specific | Atlas operations (daily, backtest, health, research, etc.) |
| `~/.pi/agent/skills/` (global) | 13 | General-purpose | quant-analyst, risk-manager, backtesting-frameworks, telegram, etc. |
| `~/.pi/agent/skills/anthropic-skills/` | 15 | Generic tooling | PDF, PPTX, DOCX, frontend-design, etc. |
| `~/.pi/agent/skills/pi-skills/` | 9 | Pi integrations | Gmail, GDrive, browser, search, etc. |
| `/root/pi-swarm/skills/` | 1 | Swarm orchestration | Parallel code modification |
| `/root/NRL-Predict/.pi/skills/` | 1 | NRL-specific | Tuesday tips workflow |
| **Total** | **~58** | | |

### What We Also Have (Non-Skill Agentics)

| Asset | Location | Lines |
|-------|----------|-------|
| Global AGENTS.md | `~/.pi/agent/AGENTS.md` | 189 |
| Atlas AGENTS.md | `atlas/AGENTS.md` | 65 |
| Atlas context injector | `pi-package/.../atlas-context-injector/` | 17KB |
| 8 Pi extensions | `pi-package/atlas-ops/extensions/` | Custom tools |
| Memory/Summary | `atlas/memory/SUMMARY.md` | ~75 |
| Lessons | `atlas/tasks/lessons.md` | Operational learnings |

---

## 2. Problems Already Present (Matching the Video's Diagnosis)

### 🔴 Problem 1: Duplicate Skills, Different Versions
`atlas-healthz` exists in **two locations** with **different content**:
- `~/.pi/agent/skills/atlas-healthz/` (global — stale?)
- `atlas/pi-package/atlas-ops/skills/atlas-healthz/` (authoritative)

Which one gets loaded depends on Pi's resolution order. This is exactly the "out of sync" problem the video describes.

### 🟡 Problem 2: Trading Skills Scattered Across Global
General-purpose trading skills live in `~/.pi/agent/skills/`:
- `quant-analyst`, `risk-manager`, `risk-metrics-calculation`, `backtesting-frameworks`, `statsmodels`, `fred-economic-data`

These are **relevant to multiple projects** (Atlas, Polymarket, potentially NRL-Predict for modeling). If we improve `quant-analyst` for Atlas, the Polymarket bot doesn't benefit unless we manually copy.

### 🟡 Problem 3: AGENTS.md Split Brain
Two AGENTS.md files with overlapping content:
- Global (189 lines): Swarm rules, task management, general workflow
- Atlas (65 lines): Memory, infrastructure, core principles

Some rules appear in both. Changes to swarm coordination in global don't propagate to atlas-specific context, and vice versa.

### 🟢 Problem 4: No Prompt Library
Atlas has no reusable prompt templates. Common operations (daily briefing, incident diagnosis, research cycle) are encoded as skills, but there's no lightweight "prompt" layer for one-off orchestration patterns that don't need a full skill.

### 🟢 Problem 5: Cross-Project Skill Sharing
5 repos on this machine (`atlas`, `NRL-Predict`, `polymarket-bot`, `supercoach-site`, `pi-swarm`). Skills useful across projects (telegram-compose, brave-search, drive, project-ops) are only available if manually installed to `~/.pi/agent/skills/`. No versioning, no sync mechanism.

---

## 3. The Video's Framework: Skills → Agents → Prompts

The video proposes a three-tier hierarchy:

```
┌─────────────────────────────────────┐
│  PROMPTS  (orchestration layer)     │  One-off, single-file instructions
│  "Run daily brief, then research"   │  that compose skills + agents
├─────────────────────────────────────┤
│  AGENTS   (scale & parallelism)     │  Specialized agent configs
│  "Research agent", "Review agent"   │  (swarm builders, director, etc.)
├─────────────────────────────────────┤
│  SKILLS   (raw capabilities)        │  Reusable knowledge + procedures
│  "atlas-backtest", "quant-analyst"  │  (SKILL.md files)
└─────────────────────────────────────┘
```

And a **Library meta-skill** that indexes all three, pointing to their authoritative locations (GitHub repos or local paths).

---

## 4. Concrete Recommendations for Atlas

### Recommendation A: Eliminate Duplicates (Quick Win — 30 min)

**Impact: 🔴 High | Effort: Low**

1. **Delete** `~/.pi/agent/skills/atlas-healthz/` — the authoritative copy is in `pi-package`
2. **Audit** whether any of the 13 global general-purpose skills have stale copies elsewhere
3. **Rule:** Atlas-specific skills live ONLY in `pi-package/atlas-ops/skills/`. Never in global.

### Recommendation B: Create a Library Index Skill (Medium Win — 2 hours)

**Impact: 🟡 Medium | Effort: Medium**

Create a `library` meta-skill at `~/.pi/agent/skills/library/SKILL.md` that acts as a registry:

```markdown
# Library — Skill & Agent Index

## Trading & Quant (authoritative: atlas/pi-package)
- atlas-backtest → /root/atlas/pi-package/atlas-ops/skills/atlas-backtest/
- atlas-daily → /root/atlas/pi-package/atlas-ops/skills/atlas-daily/
- ... (all 12 atlas skills)

## General Quant (authoritative: ~/.pi/agent/skills/)
- quant-analyst → ~/.pi/agent/skills/quant-analyst/
- risk-manager → ~/.pi/agent/skills/risk-manager/
- backtesting-frameworks → ~/.pi/agent/skills/backtesting-frameworks/

## Cross-Project Utilities
- telegram-compose → ~/.pi/agent/skills/telegram-compose/
- drive (tmux) → ~/.pi/agent/skills/drive/
- brave-search → ~/.pi/agent/skills/pi-skills/brave-search/

## Project-Specific
- nrl-tuesday-tips → /root/NRL-Predict/.pi/skills/nrl-tuesday-tips/

## Agents
- swarm → /root/pi-swarm/skills/swarm/ (parallel code modification)
- atlas-director → systemd service (research orchestration)
- atlas-research-runner → systemd service (experiment execution)
```

**Why this helps:** When the agent encounters a task it doesn't have a skill for, it can check the library first. Currently, skill discovery depends on Pi's `available_skills` injection — which is scoped to the active project. A library skill transcends project boundaries.

### Recommendation C: Consolidate AGENTS.md (Quick Win — 1 hour)

**Impact: 🟡 Medium | Effort: Low**

The global AGENTS.md (189 lines) and atlas AGENTS.md (65 lines) overlap on:
- Plan-first workflow
- Task management
- Self-improvement loop
- Demand elegance

**Action:** 
- Global AGENTS.md = universal rules (swarm, task management, verification, lessons)
- Atlas AGENTS.md = ONLY atlas-specific additions (memory location, infrastructure notes, operational procedures)
- Remove duplicated sections from atlas AGENTS.md

This reduces context injection size and eliminates conflicting rule versions.

### Recommendation D: Create a Prompts Layer (Medium Win — 2-3 hours)

**Impact: 🟡 Medium | Effort: Medium**

Atlas currently encodes everything as skills. But some operations are better as lightweight **prompt templates** — composable, single-purpose orchestration scripts:

```
atlas/prompts/
├── daily-premarket.md      # "Run ingest, generate plan, summarize risk"
├── weekly-health.md        # "Run healthz, compare to last week, draft Telegram summary"  
├── incident-triage.md      # "Check services, read last 50 log lines, diagnose"
├── research-kickoff.md     # "Review brain queue, pick top hypothesis, design experiment"
└── reopt-evaluate.md       # "Run OOS validation, compare to baseline, recommend promote/reject"
```

**Why this helps:** Skills are heavyweight — they contain domain knowledge, procedures, edge cases. Prompts are lightweight — they sequence skills and set context. Currently, Atlas skills try to do both, which makes them long and monolithic. Separating the layers makes each skill more focused and more reusable.

### Recommendation E: Pin Skill Versions with Checksums (Future — Low Priority)

**Impact: 🟢 Low (for now) | Effort: High**

The video mentions using GitHub references for distributed skills. Atlas is single-machine today, so this isn't urgent. But if we ever:
- Run Atlas on multiple machines
- Share trading skills with collaborators
- Want to roll back a skill change that caused a bad trade

Then versioning skills (even just a `version:` field in the YAML frontmatter and a changelog) would be valuable.

---

## 5. Atlas-Specific Efficiency Gains

Beyond the video's general advice, here's what would specifically make Atlas more efficient:

### E1: Reduce Context Window Bloat

Currently injected per session:
- System prompt + AGENTS.md (global: 189 lines + atlas: 65 lines) = ~254 lines of rules
- Available skills list: 40+ skill descriptions
- Atlas context injector: live system state (~30 lines)

**~25% of the context window is consumed before the first user message.** Consolidating AGENTS.md and making skills lazily-loaded (via a library index) could reclaim 30-40% of that overhead.

### E2: Skill Composition for Complex Workflows

The daily cron (`atlas-daily` skill) internally references concepts from:
- `atlas-state-queries` (checking system state)
- `atlas-backtest` (plan validation)  
- `atlas-incident` (error handling)
- `atlas-lessons` (known patterns)

But these cross-references are implicit. A prompt layer would make composition **explicit**: "Load atlas-daily, and if any step fails, load atlas-incident."

### E3: Research Agent Skill Isolation

The research loop (`atlas-research-loop`, `atlas-director`, `atlas-strategy-discovery`) runs as systemd services with their own agent context. Currently, these services duplicate some knowledge that's already in skills. A library index would let the research runner pull the latest version of any skill on demand, rather than having baked-in knowledge that goes stale.

---

## 6. Priority Ranking

| # | Action | Impact | Effort | Do When |
|---|--------|--------|--------|---------|
| 1 | Delete duplicate `atlas-healthz` from global | 🔴 Eliminates confusion | 5 min | Now |
| 2 | Consolidate AGENTS.md (remove duplication) | 🟡 Cleaner context | 1 hour | This week |
| 3 | Create prompts/ directory with 3-5 templates | 🟡 Faster daily ops | 2 hours | This week |
| 4 | Build library index skill | 🟡 Cross-project discovery | 2 hours | When adding next project |
| 5 | Add version/changelog to atlas skills | 🟢 Audit trail | 3 hours | When skills change frequently |
| 6 | Lazy skill loading optimization | 🟢 Context window savings | 4+ hours | When context limits bite |

---

## 7. Key Takeaway

The video's core insight maps directly to Atlas: **skills are raw capabilities, agents provide scale, prompts orchestrate both.** Atlas already has strong skills (12 domain-specific) and strong agents (swarm, research runner, director). What's missing is:

1. **A clean separation** — skills try to be both knowledge AND orchestration
2. **A single index** — no library/registry to discover and coordinate across projects
3. **Deduplication discipline** — at least one confirmed duplicate already, plus overlapping AGENTS.md content

The biggest immediate win is **consolidating AGENTS.md and eliminating the atlas-healthz duplicate** — 30 minutes of work that removes active sources of confusion. The prompts layer is the most architecturally interesting addition and would make daily operations measurably faster by reducing the cognitive overhead of "which skill do I load for this workflow?"
