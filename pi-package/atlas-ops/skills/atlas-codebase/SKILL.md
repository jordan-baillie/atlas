---
name: atlas-codebase
description: "Atlas codebase architecture map, module responsibilities, file locations, config structure, CLI commands, services, and Pi extension tools. Use when navigating the codebase, finding files, understanding module boundaries, or checking what CLI commands and tools are available."
type: reference
---

# Atlas Codebase Reference

Complete architecture map for the Atlas multi-market swing-trading system.

---

## Directory Structure

```
/root/atlas/
├── backtest/           # Walk-forward backtesting engine
│   ├── engine.py       # Core WF engine (train/test/step windows)
│   ├── metrics.py      # Sharpe, CAGR, drawdown, profit factor calculations
│   ├── vol_scaling.py  # Volatility-scaled position sizing
│   └── results/        # Cached backtest result files
├── brokers/            # Broker integrations
│   ├── base.py         # Abstract broker interface
│   ├── alpaca/         # Alpaca broker (ACTIVE — commission-free)
│   │   ├── broker.py   # AlpacaBroker implementation
│   │   ├── mapper.py   # Order/position mapping
│   │   ├── market_data.py
│   │   └── tradable_assets.py
│   ├── live_executor.py  # Execute approved plans via broker
│   ├── live_portfolio.py # LivePortfolio state management
│   ├── plan.py         # Trade plan generation
│   ├── position.py     # Position tracking
│   ├── registry.py     # Broker factory registry
│   ├── secrets.py      # Credential loading from ~/.atlas-secrets.json
│   └── state/          # Broker state files
├── config/             # Trading configuration
│   ├── active/         # Live configs (one per market)
│   │   ├── sp500.json  # Primary market config
│   │   └── asx.json    # Secondary market config
│   ├── candidates/     # Staged configs awaiting promotion
│   ├── inactive/       # Disabled market configs
│   └── versions/       # Pre-promotion backups
├── dashboard/          # Web dashboard
│   ├── generate_data.py  # Dashboard data generator
│   ├── live_prices.py    # Real-time price fetcher
│   ├── templates/        # HTML templates
│   ├── data/             # Generated dashboard JSON
│   └── cache/            # Dashboard cache
├── data/               # Data layer
│   ├── ingest.py       # yfinance download, cache, freshness checks
│   ├── fred.py         # FRED economic data
│   ├── macro.py        # Macro indicators
│   ├── cache/          # Parquet price cache (per-market subdirs)
│   ├── processed/      # Derived datasets
│   └── position_monitor/  # Ceasefire/geopolitical factors
├── markets/            # Market definitions
│   ├── base.py         # Abstract Market class
│   ├── sp500.py        # S&P 500 market (200 tickers)
│   ├── asx.py          # ASX market
│   └── registry.py     # Market factory
├── strategies/         # Trading strategies (one file each)
│   ├── base.py         # BaseStrategy + Signal dataclass
│   ├── momentum_breakout.py
│   ├── mean_reversion.py
│   ├── trend_following.py
│   ├── opening_gap.py
│   ├── sector_rotation.py
│   ├── short_term_mr.py
│   ├── connors_rsi2.py
│   ├── bb_squeeze.py       # Dormant
│   ├── mtf_momentum.py     # Dormant
│   └── dividend_capture.py # Dormant
├── research/           # Research & experimentation
│   ├── loop.py         # Autoresearch loop
│   ├── discovery.py    # Strategy discovery pipeline
│   ├── quick_screen.py # Quick (<10s) strategy viability check
│   ├── portfolio_optimizer.py  # Multi-strategy optimization
│   ├── promoter.py     # Config promotion pipeline
│   ├── models.py       # Research data models
│   ├── param_history.py
│   └── results/        # Experiment result TSV/JSON files
├── utils/              # Shared utilities
│   ├── config.py       # Config loading (get_active_config, get_market_config)
│   ├── telegram.py     # Telegram notifications
│   ├── charts.py       # Chart generation
│   ├── allocation.py   # Position allocation logic
│   ├── helpers.py      # Misc helpers
│   ├── logging_config.py
│   ├── signal_enrichment.py
│   ├── dynamic_sizing.py
│   ├── dividends.py
│   ├── earnings.py
│   ├── market_breadth.py
│   └── relative_strength.py
├── monitor/            # Position and risk monitoring
│   ├── evaluator.py    # Degradation checks
│   ├── models.py       # Monitor data models
│   └── seed.py         # Monitor data seeding
├── scripts/            # CLI tools and automation
│   ├── cli.py          # Main CLI entry point
│   ├── health_check.py
│   ├── autoresearch.py
│   ├── auto_reoptimize.py
│   ├── reoptimize_full_universe.py
│   ├── validate_oos.py
│   ├── daily_automation.py
│   ├── eod_settlement.py
│   ├── intraday_monitor.py
│   ├── sync_protective_orders.py
│   ├── director_cron.py
│   ├── pi-cron.sh      # Pi agent dispatch for cron
│   └── weekly_maintenance.sh
├── services/           # Service entry points
│   ├── dashboard_server.py
│   ├── telegram_bot.py
│   └── job_server.py
├── plans/              # Generated trade plans (plan_{market}_{date}.json)
├── logs/               # Application logs and equity curves
│   ├── equity_curve_sp500.json
│   ├── equity_curve_asx.json
│   └── *.log
├── memory/             # Symlink → research/brain/SUMMARY.md
│   └── SUMMARY.md
├── tasks/              # Project management
│   ├── lessons.md      # Operational lessons (35+)
│   └── skills-plan.md  # Living system architecture plan
├── tests/              # Test suite
├── pi-package/         # Pi extensions and skills
│   └── atlas-ops/
│       ├── extensions/ # 8 Pi extensions
│       └── skills/     # Pi skills (this file lives here)
└── docs/systemd/       # Service unit file backups (live copies in /etc/systemd/system/)
```

---

## CLI Reference

All commands via `python3 scripts/cli.py`. Market flag `-m` goes BEFORE the subcommand.

```bash
python3 scripts/cli.py -m <market> <command> [options]
```

| Command | Purpose | Key Options |
|---------|---------|-------------|
| `ingest` | Download/update market data | `-m sp500` |
| `universe` | Build trading universe | `-m sp500` |
| `backtest` | Run walk-forward backtest | `-m sp500 --days 252` |
| `plan` | Generate daily trade plan | `-m sp500 --date YYYY-MM-DD` |
| `approve` | Approve a pending trade plan | `-m sp500` |
| `status` | Show portfolio status | `-m sp500` |
| `ledger` | Show trade ledger | `-m sp500` |
| `review` | Run self-annealing review | `-m sp500` |
| `markets` | List available markets | (no args) |
| `broker` | Show broker connection & account | `-m sp500` |
| `live-run` | Execute approved plan via broker | `-m sp500` |
| `orders` | Show open orders from broker | `-m sp500` |
| `halt` | Emergency: cancel all orders | `-m sp500` |
| `sync` | Reconcile Atlas state with broker | `-m sp500` |
| `history` | Show live execution history with fees | `-m sp500` |
| `fees` | Analyse actual fees vs config | `-m sp500` |
| `market-check` | Check market state and calendar | `-m sp500` |
| `schedule` | Show recommended cron schedule | `-m sp500` |
| `setup-secrets` | Configure broker credentials | (interactive) |

---

## Config Structure

Active configs at `config/active/<market>.json`:

```json
{
  "version": "v3.0",
  "market_id": "sp500",
  "trading": {
    "mode": "live",              // "live" | "paper" | "dry-run"
    "approval_required": true,   // Must approve plans before execution
    "broker": "alpaca"
  },
  "risk": {
    "starting_equity": 3518.12,
    "max_risk_per_trade_pct": 0.0035,
    "max_open_positions": 10,
    "max_sector_concentration": 2,
    "max_daily_drawdown_pct": 0.02,
    "require_stop_loss": true,
    "trailing_stop": { "enabled": false }
  },
  "strategies": {
    "momentum_breakout": {
      "enabled": true,
      "lookback": 20,
      "atr_multiplier": 2.0,
      // ... strategy-specific params
    },
    // ... other strategies
  },
  "universe": { "source": "sp500", "max_tickers": 200 },
  "backtest": { "train_window": 252, "test_window": 63, "step": 21 }
}
```

---

## Strategy Interface

All strategies inherit from `strategies.base.BaseStrategy`:

```python
from strategies.base import BaseStrategy, Signal

class MyStrategy(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        s_cfg = config.get('strategies', {}).get('my_strategy', {})
        self.param = s_cfg.get('param', default_value)

    def generate_signals(self, data: pd.DataFrame, config: dict) -> pd.DataFrame:
        """Return DataFrame with 'signal' column: 1=buy, -1=sell, 0=hold."""
        ...
        return signals_df

    def calc_stop_loss(self, entry_price: float, data: pd.DataFrame) -> float:
        """Return stop-loss price."""
        ...
```

Currently enabled (SP500 v3.0): momentum_breakout, mean_reversion, trend_following, opening_gap, sector_rotation, short_term_mr, connors_rsi2.

---

## Systemd Services

| Service | Runs | Status Check |
|---------|------|-------------|
| `atlas-dashboard` | Dashboard web server (auth-protected) | `systemctl status atlas-dashboard` |
| `atlas-dashboard-refresh` | 10-second data refresh loop | `systemctl status atlas-dashboard-refresh` |
| `atlas-telegram-bot` | Telegram bot for alerts and `/task` dispatch | `systemctl status atlas-telegram-bot` |
| `atlas-director` | Automated queue management and portfolio review | `systemctl status atlas-director` |
| `atlas-research-runner` | Queue-based experiment execution daemon | `systemctl status atlas-research-runner` |
| `atlas-research-window` | Time-boxed parameter sweep windows | `systemctl status atlas-research-window` |

Service files: `/etc/systemd/system/atlas-*.service`
Restart: `systemctl restart atlas-<name>`
Logs: `journalctl -u atlas-<name> --no-pager -n 50`

---

## Cron Schedule (TZ=Australia/Brisbane)

| Time | Days | What |
|------|------|------|
| 18:00 | Mon-Fri | Health check + autofix |
| 19:00 | Mon-Fri | Premarket SP500 (pi-cron dispatch) |
| 19:15 | Mon-Fri | Sync protective orders SP500 |
| 01:30-07:30 (half-hourly) | Tue-Sat | Intraday monitor SP500 |
| 08:00 | Tue-Sat | Postclose SP500 (pi-cron dispatch) |
| 23:45 | Mon-Fri | Sync protective orders SP500 |
| Every 4h | Daily | Iran/ceasefire monitor |
| Every 1h | Daily | Ceasefire cron |
| 06:00 Sun | Weekly | Weekly maintenance |
| 07:00 Sun | Weekly | Data science cron |

---

## Pi Extension Tools

### atlas-jobs (job execution)
| Tool | Purpose |
|------|---------|
| `atlas_jobs_list_catalog` | List all available job definitions |
| `atlas_jobs_run` | Start a job by name (backtest, ingest, health_check, etc.) |
| `atlas_jobs_get` | Check status of a running/completed job |
| `atlas_jobs_list_runs` | List recent job runs (filter by job/status) |
| `atlas_jobs_cancel` | Cancel a running job |

### atlas-state (key-value store)
| Tool | Purpose |
|------|---------|
| `atlas_state_put` | Store JSON state |
| `atlas_state_get` | Retrieve stored state |
| `atlas_state_list` | List keys in a scope |
| `atlas_state_delete` | Delete a key |
| `atlas_state_new_correlation` | Generate workflow correlation ID |
| `atlas_state_lock_acquire` | Acquire a distributed lock |
| `atlas_state_lock_release` | Release a lock |
| `atlas_state_lock_status` | Check lock status |

### atlas-risk-gates (safety checks)
| Tool | Purpose |
|------|---------|
| `atlas_risk_check_plan_gate` | Evaluate if a plan can be approved/executed |
| `atlas_risk_approve_plan` | Mark plan as APPROVED (with audit trail) |
| `atlas_risk_check_config_promotion` | Check if candidate config is safe to promote |
| `atlas_risk_promote_config` | Promote candidate → active (with backup + audit) |
| `atlas_risk_check_reopt_promotion` | Combined config + validation artifact check |
| `atlas_risk_list_config_backups` | List available config backups |
| `atlas_risk_restore_config_backup` | Restore active config from backup |

### atlas-artifacts (result analysis)
| Tool | Purpose |
|------|---------|
| `atlas_artifacts_load` | Load and parse JSON artifact |
| `atlas_artifacts_summarize` | Summarize health/reopt/validation artifacts |
| `atlas_artifacts_compare` | Compare two artifacts with numeric deltas |

---

## Key File Paths

| What | Path |
|------|------|
| Active config (SP500) | `config/active/sp500.json` |
| Active config (ASX) | `config/active/asx.json` |
| Candidate configs | `config/candidates/*.json` |
| Config backups | `config/versions/active_config_pre_reopt_*.json` |
| Equity curve (SP500) | `logs/equity_curve_sp500.json` |
| Equity curve (ASX) | `logs/equity_curve_asx.json` |
| Trade plans | `plans/plan_{market}_{date}.json` |
| Broker secrets | `~/.atlas-secrets.json` |
| Lessons | `tasks/lessons.md` |
| Brain knowledge base | `memory/SUMMARY.md` |
| Research results | `research/results/*.tsv` |
| Research queue | `research/queue/` |
| Pi job manifests | `.pi/atlas-runs/*.json` |
| Pi state store | `.pi/atlas-state/kv/` |
| Service logs | `journalctl -u atlas-<service>` |
| Application logs | `logs/*.log` |
