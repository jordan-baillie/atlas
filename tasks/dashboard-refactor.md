# Dashboard refactor — keep only the new system, delete old swing-equity UI/API

**Reality (scouted 2026-06-09):** the dashboard is ~70% old swing-equity. After the Tier-2 retirement, most
tabs/APIs query DELETED modules (regime, signals, risk-of-ruin, research, strategy-lifecycle) and fail
gracefully into empty states. The live core is **Forge** (`/api/forge/state`). The new operational heart —
the forge→live shadow loop (`live/daily.py`, deployed strategies, track verdicts) — has **no view at all**.

## Target structure (4 tabs)
1. **Forge** — research pipeline. KEEP as-is (the core). `/api/forge/state` + forge components (4).
2. **Portfolio (SLIM)** — broker positions + equity + orders from `/api/dashboard-data` (live broker+SQLite).
   KEEP: EquityChart, PositionsGrid, PositionCard, OrdersTable, SummaryStrip, AllocationBar, PerformanceSection,
   SystemHealth, ReturnBadge. CUT the swing junk (below).
3. **Live (NEW)** — the forge→live pipeline: deployed strategies (`live/registry`), daily shadow runs
   (`data/live/daily/*.json` + `data/live/<name>/runs.jsonl`), track verdicts, awaiting-approval, kill-switch
   state (`remediation_kill_switch`). New `/api/live` endpoint + `LiveTab`. Replaces the old Controls tab.
4. **Midas** — crypto funding-carry paper sim (`/api/midas`, `/root/midas/data/midas.db`). KEEP (running to 2026-08-28).

## CUT — dead swing-equity (delete entirely)
**Portfolio components (deleted-module-backed):** RegimeMatrix, RegimeSection, RegimeTimeline, MacroGauges,
VixTermStructureCard, RiskSection, RiskTable, StrategyBreakdown, GaugeCard, PnlSlicedSection.
**Controls tab (swing lifecycle/universe):** ChangeStateModal, LifecycleActions, StrategyRow, UniverseRow,
RecentChangesPanel, RevertButton, LifecycleTransitionModal, LifecycleHistoryModal + hooks useStrategyLifecycle,
useShowAllUniverses + `api/lifecycle.ts`.
**API routers (delete + unmount from chat_server):** `regime.py` (regime deleted), `risk.py` (indicators/ruin),
`promotions.py` (research), `lifecycle.py` + `paper_progress.py` (swing lifecycle), `monitor_legacy.py` (legacy),
`approvals.py` (Tier-2, already stubbed). Plus the signals endpoints in `portfolio.py` (`/api/signals/*`,
`/api/macro/gauges`, `/api/positions/risk`, `/api/risk/ruin`, `/api/regime/*`).
**UI api/queries.ts:** strip regime/signals/risk/ruin/trades/pnl_filter/lifecycle queries + keys.

## DECISIONS (need a call)
- **D1 — Finance / Up-Bank tab (18 components + `finance.py` + `up_webhook.py`):** this is your *personal* banking
  dashboard (burn-down, budgets, savers, transactions) bolted onto Atlas — NOT trading. **Recommend CUT** to make
  Atlas a pure trading platform (it can live in the up-bank project's own UI). Keep only if you actively use it here.
- **D2 — build the new "Live" tab?** **Recommend YES** — it's the new operational heart (deployed strategies +
  shadow runs + track + kill-switch) and currently has zero visibility.
- **audit:** `knowledge.py` (9 routes — research/brain surface?) + `chat_sessions.py` (dashboard LLM chat?) —
  keep if used by a kept tab, else cut, during execution.

## Execution order (verify build + dashboard 200 after each)
1. Backend: unmount + delete dead routers; strip dead endpoints from portfolio.py; build `/api/live`.
2. Frontend: delete dead components/hooks/queries; slim PortfolioTab; build LiveTab; update TabBar/App.
3. `npm run build` green → restart atlas-dashboard → HTTP 200 → smoke each tab.
4. Delete dead tests; commit in reviewable steps.
