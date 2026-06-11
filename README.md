<div align="center">

# ⚡ Atlas

**Execution platform for the Crucible forge→live trading pipeline.**

Crucible (`/root/crucible`) discovers and battle-tests strategies. Atlas executes the survivors —
paper first (the forward-paper gate), then real capital once human-approved — via broker adapters,
and serves the monitoring dashboard.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Architecture](https://img.shields.io/badge/architecture-atlas%2F_package-7C4DFF?style=flat-square)](docs/ARCHITECTURE.md)
[![Live Trading](https://img.shields.io/badge/⚠_live_trading-real_capital_path-DC2626?style=flat-square)](docs/OPERATIONS.md)
[![LLM Cost](https://img.shields.io/badge/LLM_cost-$0_via_Claude_Max-22c55e?style=flat-square)](CLAUDE.md)

</div>

> ⚠️ **This system is on the path to trading real money via Alpaca/IB.** The kill-switch
> (`atlas/execution/kill_switch.py`, `data/HALT` sentinel) is a load-bearing safety control.
> Read [docs/OPERATIONS.md](docs/OPERATIONS.md) before touching production code.

---

## What Atlas is

One Python package, one dependency direction:

```
atlas/
├── kernel/      paths, config, secrets, Telegram notify, market hours, logging
├── db/          SQLite access layer for data/atlas.db (dashboard read side)
├── brokers/     venue adapters: alpaca, ib, ib_web + retry/PDT/price-arbiter plumbing
├── execution/   the forge→live loop: registry, providers, target executor, kill switch
├── analytics/   post-hoc analytics (strategy EV)
└── dashboard/   FastAPI backend (:8899) serving dashboard-ui + headless pi chat

dependency rule: kernel ← db ← brokers ← execution ← dashboard
```

Everything importable lives under `atlas/`. The repo root is **state** (`data/`, `config/` —
shared with Crucible, paths frozen), **ops** (`ops/`, `systemd/`), and **tooling**
(`scripts/`, `tests/`, `dashboard-ui/`, `pi-package/`).

## The pipeline

```
Crucible PASS ──deploy_pass()──▶ SHADOW (Paper Book) ──human approve──▶ CANARY ──▶ LIVE
                                 real paper orders                      ≤$250 real    real capital
                                 on live data, $0                       human-gated   human-gated
```

1. **Crucible** writes `data/live/<name>/target.json` daily and subprocess-calls
   `atlas.execution.providers.deploy_pass()` when a strategy clears all gates.
2. **`atlas-live-shadow.timer`** (Mon–Fri 22:00 UTC) runs [ops/forward-paper.sh](ops/forward-paper.sh):
   record realized returns → Crucible weight refresh → `atlas.execution.daily --mode shadow`.
   Each deployed strategy diffs its **virtual sub-book** against today's target weights and places
   real paper orders through `TargetExecutor` (kill-switch enforced fail-closed inside).
3. **The dashboard** (`atlas-dashboard.service`, :8899) shows the Forge run-log, the Paper Book,
   and the live pipeline state. Crucible's morning report reads the same `data/live/` files.

## Running it

```bash
# Dashboard backend (dev)
python -m uvicorn atlas.dashboard.app:app --host 127.0.0.1 --port 8899

# One shadow cycle by hand
python -m atlas.execution.record_returns
python -m atlas.execution.daily --mode shadow

# Kill switch
python -m atlas.execution.kill_switch status|halt "reason"|resume

# Strategy lifecycle
python -m atlas.execution.registry approve NAME / state NAME canary

# Tests (Windows dev: set ATLAS_PROJECT_ROOT to the repo root)
python -m pytest -q

# Frontend
cd dashboard-ui && npm run build
```

Deployment is systemd on the VPS: `sudo systemd/install.sh` links + enables the unit set and
durably retires anything deleted from `systemd/`. See [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Docs

| Doc | What |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Package map, dependency rule, the Crucible↔Atlas contract |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Halt/resume, approval gates, deploy runbook, rollback |
| [docs/DISASTER_RECOVERY.md](docs/DISASTER_RECOVERY.md) | Restic backup + restore drills |
| [CLAUDE.md](CLAUDE.md) | LLM-call routing rules ($0 Claude Max — load-bearing) |
| [memory/SUMMARY.md](memory/SUMMARY.md) | Agent session memory — read first |

## History

The original Atlas (macro-regime swing-trading system: research loop, backtest engine, AI overlay,
plan/approval execution) was retired in June 2026 — strategy discovery moved to Crucible and the
execution layer was rebuilt around target weights. The full history is in git
(`pre-cleanup-2026-06-11` tags the last pre-restructure tree).
