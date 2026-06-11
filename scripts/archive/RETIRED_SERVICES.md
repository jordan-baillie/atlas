# Retired Services Log

This file tracks services, scripts, and subsystems that have been retired from Atlas.
Entries are listed in reverse chronological order (newest first).

---

## 2026-04-22 — research queue system retired

- Archived `research/queue.json` → `research/archive/queue_wave1-5_2026-03-16.json` (frozen since Mar 16, 37 days)
- 3 valuable momentum hypotheses + 9 vol_scaling variants migrated to `research/hypotheses.json` (loaded by llm_loop_runner)
- Deleted `systemd/atlas-research-runner.service` from VCS (was already disabled on host)
- Director queue-depth gate removed (scripts/director_cron.py) — was producing silent 37-day block on experiment generation
- See investigation: /root/.pi/expertise/research-analyst/2026-04-22-queue-disposition.md

---
