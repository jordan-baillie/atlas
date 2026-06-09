# Atlas Memory Summary

## What Atlas IS now (2026-06-09 — "old Atlas is no more")
Atlas is the **live-execution PLATFORM** for the forge→live system. It is NO LONGER a research or
strategy system. The old equity-research + swing-trading codebase (`research/ backtest/ strategies/ signals/
overlay/ regime/ universe/ indicators/` + ~50 scripts + 27 systemd units) was **removed** (commits after tag
`pre-refactor`; revertible). Strategy DISCOVERY now lives in the **forge (`/root/hephaestus`)**; the
research-integrity rails in `/root/shared/research_integrity`; the knowledge wiki in `/root/research-wiki`.

## The reusable substrate Atlas keeps (the execution layer)
- `brokers/base.py` — `BrokerAdapter` ABC + types (the contract a new adapter implements)
- `brokers/registry.py` (config-driven broker selection) · `routing_policy.py` (paper/live/passive, `needs_paper_pass`)
- `brokers/live_portfolio.py` (position sync) · `core/reconcile.py` (fills/positions) · `brokers/preflight.py`
- `core/remediation_kill_switch.py` — L1 env / L2 remediation-halt / L3 `data/HALT` / L4 drawdown-from-peak
- `brokers/alpaca/` (equities adapter) · `db/` (portfolio, broker_orders, ledger, equity_history) · `utils/`
- Running services (7 live systemd units): `atlas-dashboard` (Forge dashboard :8899) · `atlas-telegram-bot` ·
  timers `atlas-backup` / `atlas-dashboard-refresh` / `atlas-reconcile-shadow` (broker-vs-internal, 30min RTH) /
  `atlas-risk-precompute` / `atlas-consolidation-closure`.

## Being REPLACED (Tier 2 — entry+stop swing model → target-weight)
`brokers/plan.py` + `brokers/live_executor.py` (long-only entry+stop+take-profit) + the telegram approval flow
(192 refs) are the OLD execution model. The new system is **target-weight, long-SHORT, multi-broker**. They get
replaced by a new `brokers/target_executor.py`, NOT reused. See `tasks/LIVE_INTEGRATION_MAP.md`.

## The Phase-4 build (board-ratified 2026-06-09; memo `/root/ceo-board/memos/2026-06-09-forge-go-live-policy/`)
PASS → live, gated. **Paper-first** (shadow on live data IS the gate); real capital gated on forward-paper
evidence AND a unit-economics AUM floor (~$10-15K micro / ~$25K equity). **FIRST deployment = BOREAS carry+trend
on IB micro-futures**, gated on the **2026-08-28** carry verdict (not forge-equity — beta-confound gate makes
equity passes borrow-hard "stranded alpha"). Build: (1) IB micro-futures adapter in `brokers/ib/`, (2)
`brokers/target_executor.py` (the bridge), (3) `live/track_expectation.py` (strategy-vs-backtest gate). Plan:
`tasks/ATLAS_REFACTOR_PLAN.md`, `tasks/LIVE_INTEGRATION_MAP.md`.

## Key facts / guards
- **Sharadar data (`data/sharadar/`) is consumed by the FORGE — never delete.** Maintained by `scripts/
  sharadar_download.py` + `scripts/ingest_sharadar_*.py`.
- Paper portfolio state lives in the DB (`data/atlas.db`, equity_history). Preserve.
- Auth: every `pi`/`claude` subprocess MUST pass `--system-prompt` (Claude Max routing, $0). See `/root/AGENTS.md`.
- No autonomous capital — human approval on every go-live/scale-up/new-broker.
- Tag `pre-refactor` is the rollback point for the whole refactor.
