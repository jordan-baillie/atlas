# Atlas Knowledge Index

*Auto-regenerated 2026-05-12. Re-run via `python3 scripts/regen_knowledge_index.py`.*

*This file is overwritten on each regen — do not edit by hand.*

---

## 1. Directory Map

| Directory | Purpose |
|-----------|---------|
| `backtest/` | Backtesting framework — engine, metrics, report generation |
| `brokers/` | Broker adapters — `alpaca/` (live trading), `paper/` (simulated), live_executor.py, live_portfolio.py |
| `config/` | JSON configs — `active/` (per-market live config), `markets.json`, strategy_params |
| `core/` | Cross-cutting modules — reconcile, remediation kill-switch |
| `dashboard-ui/` | React 19 + Vite frontend for the trading dashboard (Recharts, Tailwind) |
| `data/` | Data layer — OHLCV cache (parquet), loaders, atlas.db (SQLite ~100 MB), FRED/Tiingo helpers |
| `db/` | Database utilities — schema.sql, atlas_db.py helpers, migrations/ |
| `docs/` | Architecture docs, decision records, runbooks, audit reports |
| `indicators/` | Technical indicators library (vol cones, Yang-Zhang vol, etc.) |
| `journal/` | Trade ledger (JSON + SQLite dual-write, deprecated as primary source) |
| `logs/` | Rotating log files (not committed) |
| `monitor/` | Intraday monitoring — trailing stops, price tracking, lifecycle management |
| `overlay/` | Overlay signals (VIX, breadth, macro, alt-data) feeding signal enrichment |
| `plans/` | Trade plans — pending signals in JSON awaiting execution by `execute_approved.py` |
| `portfolio/` | Portfolio management — allocation pools, rebalancing, position sizing |
| `regime/` | Market regime detection (bull/bear/transition) — RegimeModel, distributions |
| `research/` | Research engine — backtests, parameter sweeps, experiment tracking (research_experiments table) |
| `risk/` | Risk management — VaR, ruin probability, per-trade sizing, drawdown limits |
| `scripts/` | Operational scripts — EOD settlement, reconciliation, cron jobs, health checks, migrations |
| `services/` | FastAPI servers — `chat_server.py` (dashboard API + WebSocket), Telegram bot, sub-routers in `api/` |
| `signals/` | Signal generation from strategies |
| `strategies/` | Strategy implementations (momentum, mean reversion, trend-following, etc.) |
| `tests/` | pytest test suite (336 test files, run via `pytest tests/ -x -v --timeout=30`) |
| `universe/` | Stock universe construction and filtering — membership, builder, auto-exclusions |
| `utils/` | Shared utilities — Telegram notify, config helpers, atomic writes |

## 2. Key Entry-Point Files

| File | Purpose |
|------|---------|
| [`services/chat_server.py`](services/chat_server.py) ✓ | Main dashboard API server (FastAPI) — 210 LOC bootstrap, routes in services/api/ |
| [`services/api/dashboard.py`](services/api/dashboard.py) ✓ | Dashboard data endpoint — _build_dashboard_data(), 30s cache, 8 broker RPCs |
| [`services/api/approvals.py`](services/api/approvals.py) ✓ | Trade plan approve/reject business logic + routes |
| [`services/api/lifecycle.py`](services/api/lifecycle.py) ✓ | Strategy lifecycle REST endpoints — list, transition, promote |
| [`services/api/research.py`](services/api/research.py) ✓ | Research dashboard endpoints — experiments, brain, sessions |
| [`services/api/health.py`](services/api/health.py) ✓ | System health endpoint — DB staleness, service status |
| [`services/telegram_bot.py`](services/telegram_bot.py) ✓ | Telegram notification bot — command handlers, alert dispatch |
| [`brokers/live_executor.py`](brokers/live_executor.py) ✓ | Order execution via Alpaca — execute_plan, reconcile fills, place stops |
| [`brokers/live_portfolio.py`](brokers/live_portfolio.py) ✓ | Live portfolio state management — positions, equity, drawdown, HALT |
| [`brokers/plan.py`](brokers/plan.py) ✓ | Trade plan generation — TradePlanGenerator, signal pipelines, risk checks |
| [`brokers/alpaca/broker.py`](brokers/alpaca/broker.py) ✓ | Alpaca broker adapter — get_positions(), place_order(), _broker_call() |
| [`scripts/eod_settlement.py`](scripts/eod_settlement.py) ✓ | End-of-day stop/TP checking — runs ~22:00 UTC after US close |
| [`scripts/sync_protective_orders.py`](scripts/sync_protective_orders.py) ✓ | Protective order sync — stops/TPs/OCO vs broker every 15 min |
| [`scripts/execute_approved.py`](scripts/execute_approved.py) ✓ | Execute approved trade plans — runs 23:15 AEST Mon-Fri |
| [`scripts/reconcile_positions.py`](scripts/reconcile_positions.py) ✓ | Reconcile internal state vs broker positions |
| [`scripts/reconcile_ledger.py`](scripts/reconcile_ledger.py) ✓ | Reconcile trade ledger fill prices from broker |
| [`db/atlas_db.py`](db/atlas_db.py) ✓ | SQLite helpers — get_db(), record_trade_exit(), MAE/MFE, batch upserts |
| [`db/schema.sql`](db/schema.sql) ✓ | Canonical DB schema — source of truth for all tables |

## 3. Active Strategies (sp500)

- `bb_squeeze`
- `connors_rsi2`
- `dividend_capture`
- `mean_reversion`
- `momentum_breakout`
- `mtf_momentum`
- `opening_gap`
- `sector_rotation`
- `short_term_mr`
- `trend_following`

## 4. Active Markets

- `asx` — mode=`passive` live_enabled=`False`
- `commodity_etfs` — mode=`passive` live_enabled=`False`
- `crypto` — mode=`paper` live_enabled=`False`
- `defensive_etfs` — mode=`passive` live_enabled=`False`
- `gold_etfs` — mode=`passive` live_enabled=`False`
- `regime` — mode=`?` live_enabled=`?`
- `sector_etfs` — mode=`passive` live_enabled=`False`
- `sp500` — mode=`live` live_enabled=`True`
- `treasury_etfs` — mode=`passive` live_enabled=`False`

## 5. Recent Commits (last 20)

```
81916c1e fix(state): resolve CAT stop_order_id/tp_order_id collision
58aa809a fix(research): correct silent-failure threshold semantics — max not min (follow-up eb647724)
eb647724 fix(research): dynamic silent-failure threshold based on enabled-strategy count (#326)
797c3655 fix(broker): idempotent cancel_order for 42210000 pending-cancel race
b1faf663 feat(telegram): persist inbound messages via catch-all MessageHandler
c086f641 feat(telegram): persist outbound messages to telegram_messages
20b0b691 feat(db): add telegram_messages table + persistence helpers
811c922e fix(trades): cleanup R-05b phantom rows from reconciler pre-1ef93bae
896a7f8d chore(monitoring): track same-bar stop rate (defer #316 dependency)
debb4a9a fix(trades): cleanup R-05a phantom rows + audit trail
677ec1dc docs(todo): close #324 #325 #326 — three telegram-alert fixes
ea47d7a3 fix(monitoring): heartbeat director_cron schedule + silent-failure min-age guard
636d3c8d fix(broker): add PDT pre-submit guard for BUY orders (FTNT incident)
a94841dc chore: state sync post-batch-1
8a36e978 chore(pi): remove vendored retired parallel-agent package + subagent extensions (#320)
9233d6c5 fix(reconcile): restore #315 changes orphaned by #319 reset
95055f92 fix(tests): repair sweep_universe test patches + mean_reversion Series scalar coercion (#322)
ca5f79f5 chore(scripts): quarantine autoresearch.py — service never existed (#321)
1b510bea feat(plan): skip plan generation for passive universes (#300)
29ec35ff chore(gitignore): whitelist research/archive/**/*.py (#323)
```

## 6. Test Inventory

**Total test files**: 338

| Module group | Files |
|-------------|-------|
| `tests/` | 329 |
| `tests/archive/` | 1 |
| `tests/brokers/` | 2 |
| `tests/monitor/` | 1 |
| `tests/overlay/` | 2 |
| `tests/services/` | 1 |
| `tests/ui/` | 2 |

## 7. Operational Scripts Quick Reference

| Script | Cron / Trigger | Notes |
|--------|----------------|-------|
| `scripts/pi-cron.sh premarket sp500` | 19:00 AEST Mon-Fri | Market analysis + plan generation |
| `scripts/pi-cron.sh postclose sp500` | 08:00 AEST Tue-Sat | EOD reconciliation + health report |
| `scripts/execute_approved.py -m sp500` | 23:15 AEST Mon-Fri | Execute pending trade plans |
| `scripts/sync_protective_orders.py --market sp500` | Every 15 min | Sync stop/TP/OCO orders |
| `scripts/intraday_monitor.py -m sp500` | Every 30 min RTH | Trailing stop monitoring |
| `scripts/eod_settlement.py` | 22:04 UTC Mon-Fri | EOD stop-loss / take-profit check |
| `scripts/reconcile_positions.py --market sp500` | 09:00 AEST Tue-Sat | State vs broker reconciliation |
| `scripts/reconcile_ledger.py --market sp500` | 09:30 AEST Tue-Sat | Fill-price ledger sync |
| `scripts/sync_broker_orders.py` | Every 4h | Upsert broker_orders cache table |
| `scripts/compute_daily_risk.py` | 23:00 AEST daily | VaR, vol cones, ruin probability |
| `scripts/cleanup_sediment.py --apply` | 04:00 UTC daily | Delete old incident snapshot files |
| `scripts/check_doc_staleness.py` | 08:00 AEST daily | Alert if KNOWLEDGE_INDEX or SUMMARY >30d |
| `scripts/check_macro_freshness.py` | 09:30 AEST daily | Check FRED/macro data staleness |
| `scripts/check_live_research_divergence.py` | 06:30 AEST daily | Sharpe divergence monitor + rollback |

