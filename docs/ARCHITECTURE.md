# Atlas Architecture

*Rewritten 2026-06-11 for the `atlas/` package restructure ("great deletion").*

Atlas is the **execution side** of a two-repo system:

- **Crucible** (`/root/crucible`) — autonomous strategy discovery + battle-testing (the forge).
- **Atlas** (`/root/atlas`) — executes the PASSes: paper first (shadow), then human-gated real
  capital, via broker adapters; serves the monitoring dashboard.

## Package map

```
atlas/                          # ALL importable code — nothing else in the repo is a Python package
├── kernel/                     # shared kernel — imports nothing from other atlas packages
│   ├── paths.py                # PROJECT_ROOT/DATA_DIR/CONFIG_DIR/LIVE_DATA_DIR (honours ATLAS_PROJECT_ROOT)
│   ├── config.py               # config/active/*.json loading + config_overrides layer
│   ├── secrets.py              # ~/.atlas-secrets.json + env
│   ├── notify.py               # outbound Telegram (send_message/notify/tg_escape) — the ONLY Python sender
│   ├── market_hours.py         # NYSE RTH / last-session helpers
│   └── logging_config.py       # setup_logging + SQLiteErrorWriter (errors table)
├── db/                         # SQLite access for data/atlas.db; __init__ IS the connection layer
│   ├── __init__.py             # get_db/init_db/DB_PATH + re-exports (patch atlas.db.X in tests)
│   ├── trades.py  equity.py  regime.py  system_misc.py
│   └── schema.sql              # self-contained — fresh init_db() builds every live table
├── brokers/                    # venue adapters ONLY (I/O, mapping, venue quirks)
│   ├── base.py                 # BrokerAdapter ABC + Order*/Position*/Account* types
│   ├── registry.py             # factory: get_broker / get_live_broker (alpaca | ib | ib_web)
│   ├── alpaca/  ib/  ib_web/   # one sub-package per venue
│   └── retry.py  pdt_state.py  price_arbiter.py  tiingo.py
├── execution/                  # the forge→live loop
│   ├── daily.py                # python -m atlas.execution.daily --mode shadow|live
│   ├── registry.py             # config/live_strategies.json + approve/state CLI + PROVIDERS
│   ├── providers.py            # deploy_pass() ← Crucible's entry point; target.json file-provider
│   ├── target_executor.py      # weights → diff vs current → orders on any BrokerAdapter
│   ├── virtual_book.py         # per-strategy sub-books (N strategies share one paper account)
│   ├── track_expectation.py    # realized-vs-modeled gate (on_track/diverging/halt)
│   ├── record_returns.py       # daily MTM → returns.jsonl/equity_state.json
│   └── kill_switch.py          # L1 env / L2+L3 halt files / L4 drawdown; halt|resume|status CLI
├── analytics/strategy_ev.py    # signal_ev table (dashboard EV panel)
└── dashboard/                  # FastAPI backend on :8899
    ├── app.py                  # uvicorn atlas.dashboard.app:app — router assembly
    ├── auth.py                 # basic auth from ~/.atlas-secrets.json (fail-closed)
    ├── api/                    # portfolio, health, dashboard(+builder), forge, live, static_serve
    └── chat/                   # headless pi chat: db, sessions, ws, pi_session
```

**Dependency rule (one direction, enforce in review):**
`kernel ← db ← brokers ← execution ← dashboard`; `analytics` sits beside dashboard (db only).
One sanctioned exception: `kernel/config.py` lazily imports `atlas.db` for the override layer.

**Repo root = state + ops + tooling:** `data/` and `config/` are runtime state (paths frozen —
see the contract below). `ops/` holds host-facing scripts run by systemd; `systemd/` holds ALL
unit files + `install.sh`; `scripts/` is dev tooling (lint guards, git hooks) plus
`sharadar_download.py` (Crucible subprocess-calls it); `dashboard-ui/` is the React SPA
(served from `dashboard-ui/dist` by `static_serve`).

## The Crucible↔Atlas contract (DO NOT BREAK)

File-based; one Python import string. Everything here is load-bearing for the nightly pipeline:

| Surface | Direction | Detail |
|---|---|---|
| `data/live/<name>/target.json` | Crucible → Atlas | daily target weights `{asof, weights, strategy_path}` |
| `data/live/<name>/meta.json` | Crucible → Atlas | written on deploy |
| `atlas.execution.providers.deploy_pass(name, capital=, broker=, expectation=, strategy_path=)` | Crucible → Atlas | subprocess call from `crucible/live/deploy.py` with `sys.path.insert(0,'/root/atlas')` |
| `config/live_strategies.json` | both | deployed-strategy registry (states: shadow/canary/live) |
| `data/live/<name>/{runs,returns}.jsonl`, `book.json`, `equity_state.json` | Atlas → Crucible | morning report reads these |
| `scripts/sharadar_download.py` | Crucible → Atlas | `crucible/forward/bab_track.py` runs it with cwd=/root/atlas |
| `data/sharadar/`, `data/cache/` | shared | market-data cache (Crucible's loaders read it) |
| `/root/.pi/model-policy.json` | shared | per-call LLM tier policy |
| `~/.atlas-secrets.json` | shared | Telegram/broker credentials |

Changing any path or the `deploy_pass` signature requires a **coordinated commit in both repos
deployed in the same window** (precedent: the 2026-06-11 restructure commit pair).

## The daily loop

`atlas-live-shadow.timer` (Mon–Fri 22:00 UTC, `ConditionPathExists=!/root/atlas/data/HALT`)
→ `ops/forward-paper.sh`:

1. `atlas.execution.record_returns` — MTM each strategy's virtual book at live prices, append
   `returns.jsonl`, update `equity_state.json`.
2. `crucible live/deploy.py refresh` — recompute today's target weights for every deployed strategy.
3. `atlas.execution.daily --mode shadow` — per strategy: provider reads `target.json` →
   `TargetExecutor.rebalance()` diffs against the strategy's **virtual sub-book** (never the
   blended account) → places real **paper** orders → `track_expectation` verdict → append
   `runs.jsonl` → Telegram digest via `kernel.notify`.

State machine: `shadow` (paper, autonomous) → `canary` (≤$250 real, requires
`registry approve`) → `live` (real capital, board-gated). Canary/live without approval are
computed but executed dry and flagged AWAITING APPROVAL.

## Safety

- **Kill switch** (`atlas/execution/kill_switch.py`): L1 `ATLAS_AUTO_REMEDIATION_DISABLED=1`,
  L2 `data/AUTO_REMEDIATION_HALT`, L3 `data/HALT` / `.live_halt`, L4 drawdown-from-peak.
  Checked **inside** `TargetExecutor` before any order (fail-closed) AND at the systemd layer
  (`ConditionPathExists`). Human surface: `python -m atlas.execution.kill_switch halt|resume|status`.
  L4 reads each deployed book's `data/live/<name>/returns.jsonl` (re-pointed 2026-06-11 from the
  retired `equity_history` table); the first book in drawdown breach trips the layer for the executor.
- **Auth**: every dashboard route basic-auths against `~/.atlas-secrets.json`; missing secrets
  = nothing served.
- **Price arbiter**: Tiingo-vs-Alpaca divergence halts new entries per-ticker (alerts RTH-only).

## Scalability seams

- **New strategy** = a registry row + a `target.json` writer. Virtual books isolate N strategies
  on one paper account.
- **New venue** = one `atlas/brokers/<venue>/` package + a factory registration.
- **BOREAS futures** (IB, verdict ~2026-08-28): provider stub `boreas_carry_trend` +
  `ContractSpec` futures sizing in `target_executor` + `brokers/ib{,_web}` already in place.
- **One-click approval**: a POST endpoint in `dashboard/api/live.py` calling
  `atlas.execution.registry.approve` is the designed seam (not built).
