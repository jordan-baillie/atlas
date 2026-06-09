# Atlas Refactor — "Old Atlas is no more": clean live-execution platform for the forge system

Goal: strip Atlas to its reusable **live-execution substrate** and rebuild around the new forge→live system
(target-weight, long-short, multi-broker). Remove the old equity-research + swing-trading system entirely.

Dependency scout (2026-06-09): the broker substrate + every running service import **nothing** from the old
research/swing cluster — so the big deletion is clean. The entry+stop execution layer is the one entangled part.

## Classification

### KEEP — the live-execution substrate + infra the new system reuses
- `brokers/` minus plan/live_executor: **base.py** (BrokerAdapter + types), **registry.py**, **routing_policy.py**,
  **live_portfolio.py**, **preflight.py**, **alpaca/**, mapper/market_data/secrets/tradable_assets/execution_analytics
- `core/`: **reconcile.py**, **remediation_kill_switch.py** (+ keep fix_worker if it stays useful)
- `db/` (portfolio, broker_orders, ledger, equity_history), `utils/` (fix the indicators leak)
- `services/`: chat_server (dashboard backend), api/forge.py, telegram_bot (rework), api/approvals (rework), dashboard refresh
- `risk/` risk-precompute + portfolio risk · `monitor/` kill-switch monitoring · `markets/` (US/futures)
- `data/` (DBs + market data + **Sharadar — used by the forge**) · `config/` (active configs, prune variants)
- `dashboard-ui/` (the Forge dashboard) · `pi-package/` (agent tooling) · `alerting/`, `ops/`

### REMOVE — Tier 1: old equity-research + swing-trading cluster (self-contained, ~half the repo + 2.8GB)
`research/` (2.8GB, → replaced by hephaestus) · `backtest/` (→ research_integrity rails) · `strategies/` (old
swing strategies → forge) · `signals/` · `overlay/` · `regime/` · `universe/` · `indicators/`
+ their scripts: research_runner, research_promote, reoptimize_full_universe, strategy_evaluator, validate_oos,
  run_strategy_battery, pipeline_forward_tick, pipeline_status, analyze_*, health_check (strategy bits), etc.
+ their tests (brokers/tests/test_plan_regime, portfolio/tests/test_constructor, etc.)
One leak to fix: `utils/helpers.py` re-exports `indicators.technical` (calc_atr/rsi…) — drop the re-export.

### REWORK — Tier 2: entry+stop execution layer (entangled; REPLACE, don't just delete)
`brokers/plan.py` (1116 L) + `brokers/live_executor.py` (3204 L) — the long-only entry+stop+take-profit swing
model. Referenced by KEEP services: `services/telegram_bot.py` (**192 refs** — approval UX), `services/api/
approvals.py` (50), `core/reconcile.py` (2), `monitor/health_writer.py` (1), `core/fix_worker.py`,
`risk/cross_universe_guard.py`. → Replaced by the new **target_executor** + a simpler target-rebalance approval flow.

## Staged execution (small reviewable commits; verify services stay up at each step)
- **Phase 0 — clean baseline. ✅ DONE** (commit 690e573, tag `pre-refactor`).
- **Phase 1 — Tier 1 deletion. ✅ DONE** (commit c3a6178). Removed the 8-dir cluster + ~20 old scripts + 43
  broken tests + stragglers + indicators TA re-exports. Source .py 493→320. Verified: imports clean, dashboard
  HTTP 200, telegram/dashboard active, reconcile-shadow + risk-precompute timer scripts intact.
- **Phase 4a/4b — prune + docs. ✅ DONE** (commits 2dd5f96, 344bdc7). Removed 27 old systemd units + 33
  old-research scripts + scripts/archive/. Rewrote memory/SUMMARY.md (execution-platform) + README banner.
  Source .py 320->286; scripts 160->74. Services + dashboard green.
- **Phase 2 — build the new pieces (additive, separate milestone).** `brokers/target_executor.py`,
  `brokers/ib/` adapter, `live/track_expectation.py` (Phase-4 tasks #5/#6/#8).
- **Phase 3 — Tier 2 replacement.** Once target_executor works: retire plan.py + live_executor.py, replace the
  telegram/approval flow with the target-rebalance flow, repoint reconcile/monitor. Verify end-to-end.
- **Phase 4 — final prune.** scripts/ one-offs, config variants, dead tests, docs (AGENTS/CLAUDE/memory) to
  reflect the new system. Update systemd install (only the kept timers).

## Risks / guards
- Running services must not break — verify imports + `systemctl status` after each phase.
- Sharadar data under `data/sharadar/` is consumed by the FORGE — must NOT be deleted.
- Paper portfolio state ($1336, +$283 realized) lives in the DB — preserve.
- Tier 2 telegram entanglement (192 refs) is real work — do it deliberately after target_executor, not now.
- Everything is git-revertible; tag before each destructive phase.
