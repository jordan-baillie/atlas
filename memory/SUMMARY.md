# Atlas Memory Summary

## What Atlas IS (2026-06-11 — post "great deletion")
Atlas is the **execution platform** for the Crucible forge→live pipeline. Crucible
(`/root/crucible`) discovers + battle-tests strategies; Atlas paper-trades the PASSes (shadow =
the Paper Book, real paper orders on live data, $0), then human-gated real capital, and serves
the dashboard (:8899). All code lives in ONE package: `atlas/{kernel,db,brokers,execution,
analytics,dashboard}` — dependency direction kernel←db←brokers←execution←dashboard. Repo root is
state (`data/`, `config/` — paths frozen, shared with Crucible), ops (`ops/`, `systemd/`),
tooling (`scripts/`, `tests/`, `dashboard-ui/`, `pi-package/`). See `docs/ARCHITECTURE.md`.

## The contract with Crucible (DO NOT BREAK — coordinated commits only)
`data/live/<name>/{target,meta}.json` writes; subprocess `from atlas.execution.providers import
deploy_pass`; reads `config/live_strategies.json` + `data/live/<name>/{runs,returns}.jsonl,
book.json,equity_state.json`; runs `scripts/sharadar_download.py`; shares `data/{sharadar,cache}`,
`/root/.pi/model-policy.json`, `~/.atlas-secrets.json`.

## Live surface (everything else was deleted 2026-06-11; git history is the archive)
- `atlas-dashboard.service` — uvicorn `atlas.dashboard.app:app` :8899 (auth fail-closed).
- `atlas-live-shadow.timer` Mon–Fri 22:00 UTC → `ops/forward-paper.sh` (record_returns →
  crucible refresh → `atlas.execution.daily --mode shadow`). ConditionPathExists=!data/HALT.
- Timers: backup, unified-healthcheck, weekly-maintenance, sediment-cleanup,
  sp500-flatten (transitional). `systemd/install.sh` durably retires removed units.
- Telegram COMMAND BOT IS RETIRED. Outbound notify = `atlas.kernel.notify`. Human controls:
  `python -m atlas.execution.kill_switch halt|resume|status`,
  `python -m atlas.execution.registry approve|state`.

## Lessons that survived the deletion
- Kill switch is enforced INSIDE TargetExecutor (fail-closed) + at systemd. L4 currently reads
  the stale `equity_history` table → fail-open no-data; follow-up: re-point at the live books.
- Virtual sub-books: N strategies share one paper account; each diffs against its OWN book —
  never the blended account positions.
- Honest Paper Book: equity curve filtered to PAPER_BOOK_INCEPTION (2026-06-09); no borrowed
  track record from the swing era.
- Tests: `tests/conftest.py` isolates prod DB / logs / data-live / throttle — it was lost once
  (June refactor deleted it as collateral) and the suite silently polluted real state. Don't
  let test files import deleted modules; prune tests WITH their subjects.
- Windows dev box: set `ATLAS_PROJECT_ROOT`; read_text() needs encoding="utf-8" (cp1252 traps).
- All pi/claude subprocess calls MUST carry --system-prompt (Claude Max $0 routing — CLAUDE.md).

## Pending / follow-ups
- VPS deploy of the restructure: runbook in `docs/OPERATIONS.md` (atlas+crucible same window).
- BOREAS carry+trend on IB micro-futures gated on the 2026-08-28 verdict (board 2026-06-09);
  seams ready (`boreas_carry_trend` stub, ContractSpec, brokers/ib{,_web}).
- pi-package extensions partially stale (catalog flags them); refresh when chat workflows settle.
- Delete sp500-flatten units + script once the retired account is confirmed flat.
