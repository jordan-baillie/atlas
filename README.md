<div align="center">

# ⚡ Atlas

**Live-execution PLATFORM for the forge→live system** (broker substrate + reconciliation + kill-switch + dashboard).

> ⚠️ **2026-06-09 — "old Atlas is no more".** The original equity-research + swing-trading system (regime model,
> strategies, backtest engine, overlay, research loop) was REMOVED. Atlas is now an execution-only platform:
> strategy DISCOVERY lives in the forge (`/root/hephaestus`), the rails in `/root/shared/research_integrity`.
> **Read `memory/SUMMARY.md` first.** Plan: `tasks/ATLAS_REFACTOR_PLAN.md` + `tasks/LIVE_INTEGRATION_MAP.md`.
> Docs below this banner describe the PRE-refactor system and are being updated.

<sub>(historical) Macro-adaptive, multi-asset portfolio engine. Quantitative regime model → multi-asset strategies → AI tighten-only overlay.</sub>

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Architecture](https://img.shields.io/badge/architecture-v2.0_three--layer-7C4DFF?style=flat-square)](docs/ARCHITECTURE.md)
[![Live Trading](https://img.shields.io/badge/⚠_live_trading-real_capital-DC2626?style=flat-square)](#live-trading-safety)
[![LLM Cost](https://img.shields.io/badge/LLM_cost-$0_via_Claude_Max-22c55e?style=flat-square)](#costs)

</div>

> ⚠️ **This system trades real money via Alpaca.** The kill-switch chain (L1/L2/L3) and the `data/HALT` sentinel are load-bearing safety controls. Read [Live Trading Safety](#live-trading-safety) before touching production code.

---

## What Atlas Is

Atlas is a fully automated swing-trading platform that reads the macro regime top-down, deploys capital across multiple asset universes, and continuously self-improves through walk-forward backtested research. It is **not** an LLM-driven trading bot — every signal that touches capital comes from a backtested strategy. The AI layer only **tightens** (reduces sizing, deactivates universes) and is evaluated against its own track record.

The codebase has converged on three foundational decisions: a **single SQLite database** (`data/atlas.db`) replacing 50+ scattered JSON files, a **three-layer architecture** that cleanly separates quantitative regime classification from strategy execution from AI overlay, and **all LLM calls via Claude Max OAuth** (zero marginal cost, no `Anthropic()` SDK anywhere).

For the full design rationale, schemas, and build sequence, read **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — it is the north-star document.

---

## Architecture at a Glance

```
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 3 — AI OVERLAY (Claude Max OAuth, tighten-only)              │
│  Reads regime + news + charts → writes overlay_decisions             │
│  NOT backtestable — judged by track record                          │
├─────────────────────────────────────────────────────────────────────┤
│  LAYER 2 — MULTI-ASSET STRATEGIES (7 strategies × 6 universes)      │
│  Reads OHLCV from SQLite → writes signals + plans                   │
│  FULLY BACKTESTABLE — walk-forward validated                        │
├─────────────────────────────────────────────────────────────────────┤
│  LAYER 1 — QUANTITATIVE REGIME MODEL (pure Python, deterministic)   │
│  Reads macro_indicators → writes regime_history                     │
│  6 states · FULLY BACKTESTABLE                                      │
├─────────────────────────────────────────────────────────────────────┤
│  DATA BACKBONE — SQLite (data/atlas.db, WAL mode)                   │
│  Single source of truth. Dashboard queries directly.                │
└─────────────────────────────────────────────────────────────────────┘
```

| Layer | What it does | Key files |
|-------|-------------|-----------|
| L1 — Regime model | Classifies every trading day into one of 6 regime states | `regime/model.py`, `regime/states.py` |
| L2 — Strategies | 7 strategies run across 6 universes, generate signals → plans | `strategies/`, `brokers/plan.py` |
| L3 — Overlay | Claude reads news/charts/regime, can only **tighten** sizing | `overlay/engine.py`, `overlay/sources/` |
| Data | All state, history, OHLCV, research lives here | `data/atlas.db`, `db/atlas_db.py` |

**Targets** (per [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)): max drawdown ≤15%, all-regime portfolio Sharpe ≥0.6, 6 universes traded, AI overlay net-positive over 6 months.

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/jordan-245/atlas.git /root/atlas
cd /root/atlas

# 2. Install (Python 3.10+)
pip install -r requirements.txt          # or `uv pip install -r requirements.txt`

# 3. Credentials (creates ~/.atlas-secrets.json with 600 perms)
python3 scripts/cli.py setup-secrets

# 4. Backtest
python3 scripts/cli.py backtest -m sp500

# 5. Dashboard (local dev)
cd dashboard-ui && npm install && npm run dev   # → http://localhost:5173
# Backend: in another terminal, run `python3 services/api/server.py`

# 6. (Optional) Run the test suite
pytest tests/ -q --timeout=30
```

> First-time setup gotchas: Alpaca API can be flaky on DNS — if calls timeout, add `34.232.237.2 api.alpaca.markets` to `/etc/hosts`. Tiingo API token goes in `~/.atlas-secrets.json` as `TIINGO_API_TOKEN`.

---

## Repo Layout

| Directory | Purpose |
|-----------|---------|
| `strategies/` | Strategy implementations — `BaseStrategy` ABC + 7 active strategies |
| `brokers/` | Broker adapters: `alpaca/` (live), execution, portfolio state, plan generation |
| `data/` | OHLCV ingest, macro/FRED indicators, **`atlas.db`** (SQLite, WAL mode) |
| `db/` | Typed access layer — every other module goes through `atlas_db.py` |
| `regime/` | L1 — quantitative regime model, 6-state classifier |
| `overlay/` | L3 — Claude-driven tighten-only overlay + computer-use sources |
| `portfolio/` | Cross-asset portfolio construction, per-universe limits, correlation guards |
| `research/` | Walk-forward sweeps, brain knowledge base, autoresearch loop, OOS gates |
| `backtest/` | Walk-forward engine, entry-gate filters, signal enrichment |
| `services/` | FastAPI dashboard API, Telegram bot, job server |
| `scripts/` | Operational scripts — EOD settlement, reconciliation, health checks, cron dispatcher |
| `config/` | JSON configs — `active/{market}.json` (live), `candidates/` (staged), `versions/` |
| `dashboard-ui/` | React 19 + Vite 8 frontend (TypeScript) |
| `monitor/` | Intraday monitoring — trailing stops, lifecycle state machine |
| `plans/` | Daily trade plans (pending → approved → executed) |
| `indicators/` | Technical indicator library |
| `signals/` | Signal generation utilities |
| `risk/` | Position sizing, drawdown limits, leverage gates, kill-switch logic |
| `universe/` | Stock universe construction, point-in-time membership, ETF lists |
| `tests/` | pytest suite (~880 tests) + Playwright UI tests in `tests/ui/` |
| `logs/` | Rotating log files |
| `docs/` | Architecture, decisions, runbooks, recovery procedures |
| `systemd/` | Systemd unit source-of-truth (symlinked to `/etc/systemd/system/` via `install.sh`) |

---

## Configuration

Live trading config lives in `config/active/{market}.json`. **Never edit by hand** — promotion runs through `_run_promotion_sweep` → 4 OOS gates → Telegram approval → `_do_promote()`. See `research/README.md` for the canonicality rules.

### Markets

| Market | Mode | Starting Equity | Max Positions | Status |
|--------|------|----------------|---------------|--------|
| `sp500` | **live** | $971 | 10 | ✅ Active |
| `sector_etfs` | **live** | $3,216 | 5 | ✅ Active |
| `commodity_etfs` | **live** | $1,001 | 5 | ✅ Active |
| `asx` | passive | — | 10 | 📋 Monitor-only |
| `crypto` | paper | — | 5 | 🧪 Sandbox |
| `treasury_etfs` | passive | — | 3 | 📋 Defined, not active |
| `gold_etfs` | passive | — | 2 | 📋 Defined, not active |
| `defensive_etfs` | passive | — | 3 | 📋 Defined, not active |

Each config defines: `universe` (filter rules), `risk` (per-trade %, max positions, daily DD cap, leverage), `trading` (mode, broker, auto-approve), `strategies` (enabled flags + per-strategy params).

### Kill-Switch Chain

| Tier | Trigger | Action |
|------|---------|--------|
| **L1** | Per-trade max risk exceeded (`risk.max_risk_per_trade_pct`) | Reject single signal |
| **L2** | Per-market daily drawdown breach (`risk.max_daily_drawdown_pct`, default 2%) | Halt that market for the day |
| **L3** | Catastrophic / cross-market | Write `data/HALT` sentinel — all execution paths refuse to submit orders |

The HALT file is the load-bearing emergency stop. Cleared via `scripts/clear_halt.py` after manual review.

---

## Operations

| Component | What | How to inspect |
|-----------|------|----------------|
| `atlas-dashboard.service` | FastAPI backend on `127.0.0.1:8899`, fronted by Caddy + Cloudflare tunnel | `systemctl status atlas-dashboard` |
| `atlas-telegram-bot.service` | Plan approvals, alerts, commands | `systemctl status atlas-telegram-bot` |
| `atlas-error-remediation.service` | Auto-remediation Phase 3 (every 5 min) | `journalctl -u atlas-error-remediation -n 50` |
| `atlas-heartbeat-watchdog.timer` | Schedule-aware staleness detector — config in `config/heartbeat.json` | `systemctl list-timers atlas-*` |
| `atlas-research-window@*.timer` | Per-universe nightly research sweep | One timer per active universe |
| `atlas-backup.timer` | Daily restic backup | Logs to `logs/backup.log` |
| **Cron** | `scripts/pi-cron.sh` dispatches premarket/postclose/reconcile/calibration jobs | `crontab -l` |
| **Secrets** | `~/.atlas-secrets.json` (600 perms, never committed) | Alpaca + Tiingo + Telegram credentials |
| **Systemd source** | `systemd/*.{service,timer}` → `install.sh` symlinks into `/etc/systemd/system/` | All units version-controlled |

Shared env file `/etc/atlas/atlas.conf` exposes `ATLAS_HOME` and `ATLAS_PYTHON` (optional, currently advisory).

---

## Dashboard

Production: **<https://atlas.getflowtide.com>** (Cloudflare tunnel → 127.0.0.1:8899, Basic Auth).

Five tabs: **Portfolio** (positions, P&L, equity curve), **Research** (experiments, strategy comparisons), **Finance** (Up Bank integration), **System** (heartbeats, kill-switch state, remediation log), **Trades** (full ledger).

### Local development

```bash
cd dashboard-ui
npm install
npm run dev            # Vite dev server → http://localhost:5173
```

Backend API runs from `services/api/`. The dashboard talks to `/api/*` endpoints that query SQLite directly (no `generate_data.py` step).

### UI tests

Playwright suite under `tests/ui/`:

```bash
cd tests/ui && npx playwright test
# Or per-spec:
npx playwright test test_dashboard_e2e.py
```

The audit harness (`tests/ui/dashboard_audit.py`) snapshots WARN/ERROR counts and renders a screenshot grid for visual regression.

---

## Testing

```bash
pytest tests/ -q --timeout=30                 # full suite (~880 tests, ~5-7 min)
pytest tests/test_kill_switch.py -q           # safety-critical
pytest tests/services/ -q                     # API contract
pytest tests/test_per_market_drawdown.py -q   # latest equity attribution math
```

Key suite locations: `tests/brokers/`, `tests/services/`, `tests/monitor/`, `tests/overlay/`. Conftest at `tests/conftest.py` enforces **session-scope DB isolation** (autouse fixtures redirect `db.atlas_db._db_path_override` to a tmp DB) — any new test that writes to prod state without isolation will be caught by `_zz_verify_no_pollution_*` session-end checks.

---

## Live Trading Safety

| Control | Where | Notes |
|---------|-------|-------|
| Kill-switch chain (L1/L2/L3) | `risk/kill_switch.py` | L3 writes `data/HALT` sentinel |
| Daily drawdown (per market) | `risk.max_daily_drawdown_pct` in each `config/active/*.json` | Default 2% |
| Per-market equity attribution | `portfolio/per_market_cash_flow.py` | Live cash-flow tracking via Alpaca activities API; degraded mode suppresses kill switch unless 20% catastrophic override |
| Approval flag | `trading.auto_approve` in config | Telegram `/approve_plan` for manual gating |
| Paper mode | `trading.mode: "paper"` | Full execution simulation, zero broker calls |
| HALT file | `data/HALT` | Honored by every execution path; cleared via `scripts/clear_halt.py` |
| Leverage gate | `risk.max_gross_exposure_pct` (currently 1.75) | Pre-submit check vs. live broker state |

**Reconciliation** runs at 09:00 UTC (report) and 14:00 UTC (`--fix` mode) — broker is the source of truth for "what we hold," Atlas SQLite is metadata. Three-way drift between broker / SQLite / state JSON is a P0 alert.

---

## Costs

**LLM calls cost $0.** Atlas uses Claude Max via OAuth — every `pi` or `claude` CLI subprocess call **must** include `--system-prompt "You are Claude Code, Anthropic's official CLI for Claude."` to route to the Max subscription. Without that flag, calls fall through to pay-per-token "extra usage" billing and will eventually fail with `400 out of extra usage`.

Reference:
- `/root/AGENTS.md` — global routing rule
- `/root/.pi/teams/skills/claude-auth.md` — full guidance
- Atlas verified call sites: `services/job_server.py`, `services/pi_session.py`, `research/discovery/discovery.py`, `research/llm_loop_runner.py`, `overlay/engine.py`, `scripts/autoresearch.py`

The Anthropic Python SDK is **never** instantiated with an API key. Ever.

---

## Status

| Phase | Goal | State |
|-------|------|-------|
| **Phase 0** | SQLite foundation, migration | ✅ Complete |
| **Phase 1** | Regime model, 6-state classifier, historical backfill | ✅ Complete |
| **Phase 2** | Multi-universe strategies (7 × 6) | ✅ Complete |
| **Phase 3** | Regime → plan wiring, portfolio construction | ✅ Complete |
| **Phase 4** | AI overlay (`claude -p` driven, tighten-only) | ✅ Complete (shadow mode → enforce) |
| **Phase 5** | Dashboard cutover to direct SQLite queries | 🔧 In progress |

Recent landmarks (see `tasks/todo.md` and CEO journal for detail): per-market equity attribution via live Alpaca activities API (`#FIX-PMEQ-001`), schedule-aware heartbeat watchdog, auto-remediation Phase 3 enabled with 9-class whitelist + recursive self-protection, dashboard equity-curve normalization via cumulative-deposits subtraction.

---

## Further Reading

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — north-star design document (schemas, build sequence, regime configs)
- **[docs/DECISIONS.md](docs/DECISIONS.md)** — historical key decisions
- **[docs/runbooks/](docs/runbooks/)** — daily-health-check, data-refresh, performance-report, post-incident-review, weekly-reoptimize
- **[docs/auto-remediation-runbook.md](docs/auto-remediation-runbook.md)** — Phase 3 remediation procedure
- **[docs/DISASTER_RECOVERY.md](docs/DISASTER_RECOVERY.md)** — recovery scenarios + DR drills
- **[docs/state-model.md](docs/state-model.md)** — trade lifecycle state machine
- **[research/README.md](research/README.md)** — canonicality rules for `config/active/` vs `research_best` SQLite vs JSON

---

<div align="center">

*Broker is sole source of truth. Backtested signals only. Tighten-only AI. Single SQLite database. $0 LLM cost.*

</div>
