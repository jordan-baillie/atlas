# Atlas — Three-Layer Adaptive Architecture

**Version:** 2.0
**Date:** 2026-03-31
**Status:** BLUEPRINT — ready for phased build
**Author:** Jordan + Claude (architectural design session)

---

## Vision

Atlas evolves from a long-only S&P 500 trading system into a macro-adaptive, multi-asset portfolio engine with AI agents as the foundational orchestration layer. The system reads the economy top-down, deploys capital across asset classes based on regime, and continuously self-improves through backtested research.

**Two foundational upgrades drive this evolution:**

1. **SQLite as the data backbone.** Every piece of state — OHLCV prices, trades, signals, regime history, research experiments, dashboard data — lives in a single SQLite database. No more 50+ scattered JSON files. No more a 3,704-line `generate_data.py` assembling a monolithic dashboard JSON. The dashboard queries SQLite directly. Writes are atomic. History is queryable. Backup is one file.

2. **All LLM calls through Claude Max OAuth.** Every Claude call — regime analysis, chart intelligence, news synthesis, research, operational cron — routes through `claude -p --output-format json` or Pi agent skills. Zero API costs. No `anthropic.Anthropic()` client anywhere. The Claude Max subscription is the AI budget ceiling.

**Core trading constraint:** Everything that touches capital must be backtestable. The AI layer orchestrates backtested components — it does not generate unbacked signals.

**Current live results (16 trades):** 75% win rate, 8.23x profit factor, +$27.16 expectancy.

---

## What Carries Forward (Proven, Do Not Rewrite)

| Component | Path | Change |
|---|---|---|
| Backtest engine | `backtest/engine.py` (1,632 LOC) | Extend for multi-universe |
| BaseStrategy + 7 strategies | `strategies/` | Unchanged — they work on any OHLCV data |
| Autoresearch sweeper | `research/sweep.py`, `autoresearch_runner.py` | Extend to new universes |
| Autoresearch LLM loop | `research/loop.py`, `program.md` | Unchanged |
| Research brain | `research/brain/` | Markdown stays; structured data moves to SQLite |
| Computer use MCP server | `research/discovery/mcp_server/server.js` | Expand targets |
| Strategy discovery | `research/discovery/` | Unchanged |
| Pi agent + 12 skills | `pi-package/atlas-ops/skills/` | Add regime + portfolio skills |
| Swarm + subagent | `extensions/` | Unchanged |
| Job server | `services/job_server.py` | Unchanged |
| Telegram bot | `services/telegram_bot.py` | Add regime alerts |
| Macro data modules | `data/macro.py`, `data/fred.py` | Write to SQLite instead of cache files |
| Config files | `config/active/*.json` | Stay as JSON files (human-editable) |

---

## Data Infrastructure: SQLite

### Why SQLite

The current data architecture has these problems:

- `journal/decision_journal.json` is **2.5 MB** — loaded entirely into memory to append one entry, then rewritten
- `dashboard/generate_data.py` is **3,704 lines** reading 50+ JSON files to produce one monolithic `dashboard-data.json`
- `research/journal.json` at **223 KB** append-only with no indexing
- Plans stored as individual JSON files up to **226 KB** each
- Broker state, position monitor, ceasefire factors — all scattered JSON
- No ability to query historical data without loading entire files
- Atomic writes require temp-file-rename pattern (fragile, repeated everywhere)

SQLite solves all of this:

- **Single file** (`data/atlas.db`) — one backup, one source of truth
- **Atomic transactions** — no more temp-file-rename patterns
- **Indexed queries** — "show me trades from last month" is instant, not a full file scan
- **Concurrent reads** — dashboard server reads while cron jobs write (WAL mode)
- **Point-in-time queries** — "what was my portfolio 2 weeks ago?" becomes trivial
- **15 MB ceiling** — all current JSON data fits in <15 MB SQLite; grows linearly

### Database location

```
/root/atlas/data/atlas.db          # Production (VPS)
/root/atlas/data/atlas.db-wal      # Write-ahead log (SQLite WAL mode)
/root/atlas/data/atlas.db-shm      # Shared memory (auto-managed)
```

Backtest uses a separate database or in-memory copy to avoid polluting production data.

### Schema

```sql
-- ═══════════════════════════════════════════════════════════
-- PRICE DATA
-- ═══════════════════════════════════════════════════════════

CREATE TABLE ohlcv (
    ticker      TEXT    NOT NULL,
    date        TEXT    NOT NULL,  -- ISO date YYYY-MM-DD
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    adj_close   REAL,
    volume      INTEGER NOT NULL,
    universe    TEXT    NOT NULL,  -- 'sp500', 'sector_etfs', 'treasury_etfs', etc.
    source      TEXT    DEFAULT 'tiingo',
    PRIMARY KEY (ticker, date)
);
CREATE INDEX idx_ohlcv_date ON ohlcv(date);
CREATE INDEX idx_ohlcv_universe ON ohlcv(universe, date);

-- ═══════════════════════════════════════════════════════════
-- MACRO INDICATORS (Layer 1 input)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE macro_indicators (
    date            TEXT PRIMARY KEY,  -- ISO date
    vix             REAL,
    vix3m           REAL,
    vix_term_ratio  REAL,              -- VIX/VIX3M
    yield_10y       REAL,
    yield_2y        REAL,
    yield_3m        REAL,
    yield_curve_10y2y   REAL,
    yield_curve_10y3m   REAL,
    credit_oas      REAL,              -- IG credit OAS (BAMLC0A0CM)
    dxy             REAL,              -- Dollar index
    gold            REAL,
    copper          REAL,
    gold_copper_ratio   REAL,
    fed_funds       REAL,
    unemployment_claims INTEGER,
    spy_close       REAL,
    spy_200dma      REAL,
    spy_above_200dma    INTEGER,       -- 0/1
    spy_200dma_slope    REAL,          -- 20-day slope
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- ═══════════════════════════════════════════════════════════
-- REGIME HISTORY (Layer 1 output)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE regime_history (
    date            TEXT PRIMARY KEY,
    regime_state    TEXT NOT NULL,      -- enum: bull_risk_on, bull_risk_off, etc.
    trend_score     REAL,
    risk_score      REAL,
    active_universes TEXT,              -- JSON array: ["sp500","sector_etfs"]
    sizing_multiplier REAL DEFAULT 1.0,
    enabled_strategies TEXT,            -- JSON array
    reasoning       TEXT,
    model_version   TEXT
);
CREATE INDEX idx_regime_state ON regime_history(regime_state);

-- ═══════════════════════════════════════════════════════════
-- TRADING
-- ═══════════════════════════════════════════════════════════

CREATE TABLE signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    ticker          TEXT    NOT NULL,
    strategy        TEXT    NOT NULL,
    universe        TEXT    NOT NULL,
    direction       TEXT    DEFAULT 'long',
    entry_price     REAL    NOT NULL,
    stop_price      REAL    NOT NULL,
    take_profit     REAL,
    position_size   INTEGER NOT NULL,
    position_value  REAL    NOT NULL,
    risk_amount     REAL    NOT NULL,
    confidence      REAL    NOT NULL,
    rationale       TEXT,
    features        TEXT,              -- JSON
    sector          TEXT,
    regime_state    TEXT,              -- Regime at time of signal
    action          TEXT    NOT NULL,  -- 'accepted', 'rejected', 'filtered'
    action_reason   TEXT,
    config_version  TEXT,
    market_id       TEXT
);
CREATE INDEX idx_signals_date ON signals(timestamp);
CREATE INDEX idx_signals_ticker ON signals(ticker);
CREATE INDEX idx_signals_strategy ON signals(strategy);

CREATE TABLE trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    strategy        TEXT    NOT NULL,
    universe        TEXT,
    direction       TEXT    DEFAULT 'long',
    entry_date      TEXT    NOT NULL,
    entry_price     REAL    NOT NULL,
    shares          INTEGER NOT NULL,
    stop_price      REAL,
    take_profit     REAL,
    exit_date       TEXT,
    exit_price      REAL,
    exit_reason     TEXT,
    pnl             REAL,
    pnl_pct         REAL,
    mae             REAL,              -- Max adverse excursion
    mfe             REAL,              -- Max favourable excursion
    hold_days       INTEGER,
    confidence      REAL,
    regime_at_entry TEXT,
    regime_at_exit  TEXT,
    status          TEXT    DEFAULT 'open',  -- 'open', 'closed'
    config_version  TEXT,
    created_at      TEXT    DEFAULT (datetime('now')),
    updated_at      TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX idx_trades_status ON trades(status);
CREATE INDEX idx_trades_strategy ON trades(strategy);
CREATE INDEX idx_trades_dates ON trades(entry_date, exit_date);

CREATE TABLE plans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT    NOT NULL,
    market_id       TEXT    NOT NULL,
    regime_state    TEXT,
    active_universes TEXT,              -- JSON array
    sizing_multiplier REAL,
    overlay_applied INTEGER DEFAULT 0,
    overlay_adjustments TEXT,           -- JSON
    plan_data       TEXT    NOT NULL,   -- Full plan JSON (signals, risk summary)
    status          TEXT    DEFAULT 'pending',  -- 'pending', 'approved', 'rejected', 'executed'
    approved_at     TEXT,
    executed_at     TEXT,
    created_at      TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX idx_plans_date ON plans(date, market_id);

-- ═══════════════════════════════════════════════════════════
-- PORTFOLIO STATE
-- ═══════════════════════════════════════════════════════════

CREATE TABLE equity_curve (
    date            TEXT    NOT NULL,
    market_id       TEXT    NOT NULL,
    equity          REAL    NOT NULL,
    cash            REAL,
    positions_value REAL,
    day_pnl         REAL,
    regime_state    TEXT,
    PRIMARY KEY (date, market_id)
);

CREATE TABLE portfolio_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    total_equity    REAL,
    cash            REAL,
    positions       TEXT,              -- JSON: [{ticker, shares, value, universe}]
    exposure_by_universe TEXT,         -- JSON: {sp500: 0.45, treasury_etfs: 0.20}
    exposure_by_sector   TEXT,         -- JSON: {energy: 0.15, tech: 0.10}
    regime_state    TEXT,
    source          TEXT DEFAULT 'eod' -- 'eod', 'intraday', 'manual'
);
CREATE INDEX idx_snapshots_ts ON portfolio_snapshots(timestamp);

-- ═══════════════════════════════════════════════════════════
-- AI OVERLAY (Layer 3)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE overlay_decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    regime_state    TEXT    NOT NULL,
    action          TEXT    NOT NULL,  -- 'no_change', 'tighten'
    sizing_override REAL,
    universes_deactivated TEXT,        -- JSON array
    tickers_avoided TEXT,              -- JSON array
    reasoning       TEXT,
    confidence      REAL,
    data_sources    TEXT,              -- JSON: what inputs were used
    -- Outcome evaluation (filled in by weekly evaluator)
    outcome_evaluated   INTEGER DEFAULT 0,
    outcome_correct     INTEGER,       -- 0/1 
    outcome_notes       TEXT,
    evaluated_at        TEXT
);
CREATE INDEX idx_overlay_ts ON overlay_decisions(timestamp);

-- ═══════════════════════════════════════════════════════════
-- GEOPOLITICAL MONITOR
-- ═══════════════════════════════════════════════════════════

CREATE TABLE ceasefire_factors (
    id              TEXT    PRIMARY KEY,
    category        TEXT    NOT NULL,  -- 'ceasefire', 'escalation'
    description     TEXT    NOT NULL,
    weight          REAL    NOT NULL,
    active          INTEGER DEFAULT 0,
    confidence      TEXT    DEFAULT 'medium',
    source          TEXT,
    last_updated    TEXT
);

CREATE TABLE ceasefire_history (
    timestamp       TEXT    PRIMARY KEY,
    probability     REAL    NOT NULL,
    active_factors  TEXT,              -- JSON array of factor IDs
    change_log      TEXT               -- What changed
);

CREATE TABLE news_intel (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    source          TEXT,              -- 'brave', 'gdelt', 'google_rss'
    headline        TEXT,
    url             TEXT,
    relevance_score REAL,
    category        TEXT,              -- 'iran', 'fed', 'earnings', 'macro'
    summary         TEXT,
    created_at      TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX idx_news_ts ON news_intel(timestamp);
CREATE INDEX idx_news_category ON news_intel(category);

-- ═══════════════════════════════════════════════════════════
-- RESEARCH
-- ═══════════════════════════════════════════════════════════

CREATE TABLE research_experiments (
    id              TEXT    PRIMARY KEY,  -- experiment ID
    strategy        TEXT    NOT NULL,
    universe        TEXT    DEFAULT 'sp500',
    experiment_type TEXT,               -- 'param_sweep', 'combined', 'oos', etc.
    params_changed  TEXT,               -- JSON
    description     TEXT,
    -- Results
    sharpe          REAL,
    trades          INTEGER,
    max_dd_pct      REAL,
    profit_factor   REAL,
    cagr_pct        REAL,
    -- Verdict
    status          TEXT    DEFAULT 'running', -- 'running','kept','discarded','error'
    recommendation  TEXT,
    -- Metadata
    baseline_sharpe REAL,
    runtime_s       REAL,
    agent_id        TEXT,
    created_at      TEXT    DEFAULT (datetime('now')),
    completed_at    TEXT
);
CREATE INDEX idx_experiments_strategy ON research_experiments(strategy);
CREATE INDEX idx_experiments_status ON research_experiments(status);

CREATE TABLE research_best (
    strategy        TEXT    NOT NULL,
    universe        TEXT    NOT NULL,
    params          TEXT    NOT NULL,   -- JSON: best known parameters
    sharpe          REAL,
    trades          INTEGER,
    max_dd_pct      REAL,
    updated_at      TEXT    DEFAULT (datetime('now')),
    PRIMARY KEY (strategy, universe)
);

-- ═══════════════════════════════════════════════════════════
-- SYSTEM
-- ═══════════════════════════════════════════════════════════

CREATE TABLE heartbeats (
    service         TEXT    PRIMARY KEY,
    timestamp       TEXT    NOT NULL,
    status          TEXT    NOT NULL,
    detail          TEXT               -- JSON
);

CREATE TABLE system_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    DEFAULT (datetime('now')),
    level           TEXT    NOT NULL,  -- 'info', 'warning', 'error', 'critical'
    service         TEXT    NOT NULL,
    message         TEXT,
    detail          TEXT               -- JSON
);
CREATE INDEX idx_syslog_ts ON system_log(timestamp);
CREATE INDEX idx_syslog_service ON system_log(service);
```

### Database access layer

A single module provides typed access. Every other module goes through this — no raw SQL scattered across the codebase.

```python
# db/atlas_db.py

import sqlite3
import json
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "atlas.db"

@contextmanager
def get_db():
    """Get a database connection with WAL mode and foreign keys."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ── Trades ──────────────────────────────────────────────────

def record_trade_entry(ticker, strategy, universe, entry_price, shares,
                       stop_price, take_profit, confidence, regime_state, **kwargs):
    with get_db() as db:
        db.execute("""
            INSERT INTO trades (ticker, strategy, universe, entry_date, entry_price,
                shares, stop_price, take_profit, confidence, regime_at_entry, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """, (ticker, strategy, universe, datetime.now().isoformat(), entry_price,
              shares, stop_price, take_profit, confidence, regime_state))

def record_trade_exit(ticker, strategy, exit_price, exit_reason):
    with get_db() as db:
        db.execute("""
            UPDATE trades SET exit_date=?, exit_price=?, exit_reason=?, status='closed',
                pnl = (? - entry_price) * shares,
                pnl_pct = ((? - entry_price) / entry_price) * 100,
                hold_days = julianday(?) - julianday(entry_date),
                updated_at = datetime('now')
            WHERE ticker=? AND strategy=? AND status='open'
        """, (datetime.now().isoformat(), exit_price, exit_reason,
              exit_price, exit_price, datetime.now().isoformat(),
              ticker, strategy))

def get_open_positions():
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY entry_date"
        ).fetchall()]

def get_closed_trades(days=None, strategy=None, universe=None):
    with get_db() as db:
        query = "SELECT * FROM trades WHERE status='closed'"
        params = []
        if days:
            query += " AND exit_date >= date('now', ?)"
            params.append(f'-{days} days')
        if strategy:
            query += " AND strategy=?"
            params.append(strategy)
        if universe:
            query += " AND universe=?"
            params.append(universe)
        query += " ORDER BY exit_date DESC"
        return [dict(r) for r in db.execute(query, params).fetchall()]

def performance_summary(days=None):
    trades = get_closed_trades(days=days)
    if not trades:
        return {"trades": 0}
    wins = [t for t in trades if (t['pnl'] or 0) > 0]
    losses = [t for t in trades if (t['pnl'] or 0) <= 0]
    avg_win = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(abs(t['pnl']) for t in losses) / len(losses) if losses else 0
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": (sum(t['pnl'] for t in wins) / 
                         sum(abs(t['pnl']) for t in losses)) if losses else float('inf'),
        "expectancy": sum(t['pnl'] for t in trades) / len(trades),
        "by_universe": _group_performance(trades, 'universe'),
        "by_strategy": _group_performance(trades, 'strategy'),
    }

# ── Regime ──────────────────────────────────────────────────

def record_regime(date, state, trend_score, risk_score, 
                  active_universes, sizing_multiplier, reasoning=""):
    with get_db() as db:
        db.execute("""
            INSERT OR REPLACE INTO regime_history 
                (date, regime_state, trend_score, risk_score, active_universes,
                 sizing_multiplier, reasoning, model_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (date, state, trend_score, risk_score,
              json.dumps(active_universes), sizing_multiplier, reasoning, "v1"))

def get_current_regime():
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM regime_history ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if row:
            r = dict(row)
            r['active_universes'] = json.loads(r['active_universes'] or '[]')
            return r
        return None

# ── OHLCV ───────────────────────────────────────────────────

def upsert_ohlcv(ticker, date, o, h, l, c, adj, vol, universe, source='tiingo'):
    with get_db() as db:
        db.execute("""
            INSERT OR REPLACE INTO ohlcv 
                (ticker, date, open, high, low, close, adj_close, volume, universe, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ticker, date, o, h, l, c, adj, vol, universe, source))

def get_ohlcv(ticker, start_date=None, end_date=None):
    """Returns DataFrame for compatibility with existing strategies."""
    import pandas as pd
    with get_db() as db:
        query = "SELECT * FROM ohlcv WHERE ticker=?"
        params = [ticker]
        if start_date:
            query += " AND date>=?"
            params.append(start_date)
        if end_date:
            query += " AND date<=?"
            params.append(end_date)
        query += " ORDER BY date"
        df = pd.read_sql_query(query, db, params=params, parse_dates=['date'])
        if not df.empty:
            df.set_index('date', inplace=True)
        return df

def get_universe_data(universe_name, start_date=None):
    """Get OHLCV data for all tickers in a universe. Returns dict[ticker, DataFrame]."""
    import pandas as pd
    with get_db() as db:
        query = "SELECT DISTINCT ticker FROM ohlcv WHERE universe=?"
        tickers = [r[0] for r in db.execute(query, (universe_name,)).fetchall()]
    return {t: get_ohlcv(t, start_date=start_date) for t in tickers}

# ... (signals, overlay, research, dashboard queries follow the same pattern)
```

### Dashboard API (replaces generate_data.py)

The 3,704-line `generate_data.py` is replaced by direct SQLite queries in the dashboard server. Instead of regenerating a monolithic JSON, each dashboard tab queries what it needs.

```python
# services/dashboard_server.py — new API endpoints

# GET /api/portfolio → current positions + equity + regime
def handle_portfolio(self):
    positions = atlas_db.get_open_positions()
    regime = atlas_db.get_current_regime()
    equity = atlas_db.get_latest_equity()
    return {"positions": positions, "regime": regime, "equity": equity}

# GET /api/trades?days=30&universe=sp500 → recent closed trades
def handle_trades(self):
    days = int(self.params.get('days', 30))
    universe = self.params.get('universe')
    return atlas_db.get_closed_trades(days=days, universe=universe)

# GET /api/regime/history?days=90 → regime state timeline
def handle_regime_history(self):
    days = int(self.params.get('days', 90))
    return atlas_db.get_regime_history(days=days)

# GET /api/research/experiments?strategy=mean_reversion&limit=50
def handle_experiments(self):
    return atlas_db.get_experiments(
        strategy=self.params.get('strategy'),
        limit=int(self.params.get('limit', 50))
    )

# GET /api/performance → trade performance summary
def handle_performance(self):
    return atlas_db.performance_summary(days=int(self.params.get('days', 0) or 0))

# GET /api/overlay/decisions?days=30 → AI overlay decision log
def handle_overlay(self):
    return atlas_db.get_overlay_decisions(days=int(self.params.get('days', 30)))
```

The dashboard HTML/JS templates update to fetch from these endpoints instead of reading a static JSON. This means **real-time updates** — no more waiting for `generate_data.py` to run.

### Migration path

Existing JSON data is migrated once:

```python
# scripts/migrate_to_sqlite.py
# 1. Create schema
# 2. Import trade_ledger.json → trades table
# 3. Import decision_journal.json → signals table
# 4. Import research/journal.json → research_experiments table
# 5. Import OHLCV parquet snapshots → ohlcv table
# 6. Import ceasefire_factors.json → ceasefire_factors table
# 7. Import broker state → portfolio_snapshots table
# 8. Verify row counts match source file counts
```

After migration, the old JSON files become read-only backups. New writes go to SQLite only.

---

## LLM Routing: Claude Max OAuth Only

**Every LLM call routes through `claude` CLI or Pi agent. No Anthropic API SDK. Zero API costs.**

The codebase already follows this pattern in `research/discovery/discovery.py`:

```python
cmd = ["claude", "-p", "--output-format", "json"]
```

All new LLM-powered components follow the same pattern:

### Routing table

| Component | Routing | How |
|---|---|---|
| Regime overlay | `claude -p --output-format json` | Structured prompt → JSON output |
| Chart intelligence | `claude -p` with image attachment | Screenshot → structured analysis |
| News synthesis | Pi agent (existing iran_monitor_cron) | Already uses Pi via Claude Max |
| Autoresearch LLM loop | Pi agent (existing) | Already uses Pi |
| Strategy discovery | `claude -p --output-format json` (existing) | Already uses this pattern |
| Operational cron | Pi agent with skills (existing) | Already uses Pi via Claude Max |
| Weekly portfolio review | Pi agent with regime skill | New skill, existing infrastructure |
| Health checks | Pi agent (existing) | Already uses Pi |
| Overlay evaluation | `claude -p --output-format json` | Weekly backscore analysis |

### Structured output pattern

For components that need structured JSON responses (regime overlay, chart analysis, overlay evaluation), use the established `claude -p --output-format json` pattern with a system prompt that constrains the output format:

```python
# overlay/engine.py

import subprocess
import json

def run_regime_overlay(regime_data: dict, news_summary: str, 
                       chart_analysis: dict) -> dict:
    """
    Run the AI overlay via claude CLI. Returns tightening adjustments.
    All LLM costs covered by Claude Max subscription.
    """
    prompt = f"""You are the Atlas regime overlay agent. Your job is to assess 
whether the quantitative regime classification needs tightening based on 
qualitative intelligence.

CONSTRAINT: You can ONLY tighten — reduce sizing, deactivate universes, 
avoid tickers. You CANNOT loosen beyond the regime model's defaults.

## Current Regime State
{json.dumps(regime_data, indent=2)}

## News Intelligence (last 24h)
{news_summary}

## Chart Analysis
{json.dumps(chart_analysis, indent=2)}

Respond with a JSON object:
{{
    "adjust": true/false,
    "sizing_multiplier_override": <float, must be <= {regime_data.get('sizing_multiplier', 1.0)}>,
    "universes_to_deactivate": [<list of universe names to remove>],
    "tickers_to_avoid": [<list of specific tickers>],
    "reasoning": "<brief explanation>",
    "confidence": <float 0-1>
}}

If no tightening is warranted, set adjust=false and leave other fields empty/default."""

    result = subprocess.run(
        ["claude", "-p", "--output-format", "json"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=120
    )
    
    if result.returncode != 0:
        logger.error(f"Claude CLI failed: {result.stderr}")
        return {"adjust": False, "reasoning": "CLI error — defaulting to no change"}
    
    return json.loads(result.stdout)
```

### Computer use via Claude Max

Computer use for TradingView charts, alternative data scraping, and research discovery all run through the existing MCP server + Pi agent infrastructure, which authenticates via Claude Max OAuth. No separate API key or billing.

The flow is:
```
Pi agent (Claude Max OAuth)
  → loads computer use MCP server (Xvfb + xdotool)
  → navigates target site
  → takes screenshots
  → Pi agent analyses screenshots (vision is included in Claude Max)
  → outputs structured JSON
```

---

## Three-Layer Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                    LAYER 3: AI OVERLAY                              │
│         claude -p via Claude Max OAuth — TIGHTEN ONLY              │
│         Reads: regime + news + charts + alt data                   │
│         Writes: overlay_decisions table in SQLite                  │
│         NOT backtestable — evaluated by track record over time      │
├─────────────────────────────────────────────────────────────────────┤
│                    LAYER 2: MULTI-ASSET STRATEGIES                  │
│         7 proven strategies × 6 universes                          │
│         Reads OHLCV from SQLite, writes signals to SQLite          │
│         Universe activation controlled by Layer 1                   │
│         FULLY BACKTESTABLE — walk-forward validated                │
├─────────────────────────────────────────────────────────────────────┤
│                    LAYER 1: QUANTITATIVE REGIME MODEL               │
│         Reads macro_indicators from SQLite                         │
│         Writes regime_history to SQLite                            │
│         Pure Python, no LLM — deterministic classification         │
│         FULLY BACKTESTABLE — historical data available             │
└─────────────────────────────────────────────────────────────────────┘
│                                                                     │
│                    DATA BACKBONE: SQLite (atlas.db)                 │
│         Single source of truth for all state                       │
│         Dashboard queries directly — no generation step            │
│         WAL mode for concurrent read/write                         │
└─────────────────────────────────────────────────────────────────────┘
```

### Daily information flow

```
06:00 AEST  data/ingest.py writes OHLCV → ohlcv table
            data/macro.py writes indicators → macro_indicators table
            data/fred.py writes FRED data → macro_indicators table
            ↓
06:05       regime/model.py reads macro_indicators, classifies →
            writes regime_history table
            ↓
06:10       overlay/engine.py reads regime + news + charts via claude -p →
            writes overlay_decisions table
            ↓
19:00       brokers/plan.py reads regime_history + overlay_decisions,
            scans active universes from ohlcv table,
            runs strategies, writes signals table,
            writes plans table (status='pending')
            ↓
19:05       Telegram notification → human reviews plan
            ↓
            Human approves → plans table (status='approved')
            ↓
23:30       live_executor reads approved plan from plans table,
            executes, writes trades table
            ↓
08:00+1     eod_settlement reads trades, updates exits,
            writes equity_curve, portfolio_snapshots
            Dashboard reads all of it live from SQLite
```

---

## Layer 1: Quantitative Regime Model

### Regime states

Six states covering trend × risk appetite:

| State | When | What Atlas does |
|---|---|---|
| `bull_risk_on` | SPY > 200 DMA, VIX < 20, credit tight | Full deployment — all strategies, all growth universes |
| `bull_risk_off` | SPY > 200 DMA, but VIX elevated or credit widening | Selective — fewer strategies, rotate toward defensives |
| `transition_uncertain` | Flat/choppy, conflicting signals | Small positions, defensive tilt, mean reversion focus |
| `bear_risk_off` | SPY < 200 DMA, VIX > 25, credit widening | Capital preservation — trend follow safe havens only |
| `bear_capitulation` | VIX > 35, curve inverted, credit blowout | Minimal deployment — mostly cash, small gold/treasury |
| `recovery_early` | SPY crossing back above 200 DMA, VIX declining | Increasing deployment — momentum + trend for the turn |

### Regime → Configuration mapping

```python
# regime/states.py

REGIME_CONFIGS = {
    "bull_risk_on": {
        "active_universes": ["sp500", "sector_etfs", "commodity_etfs"],
        "strategy_types": ["all"],
        "sizing_multiplier": 1.0,
        "max_positions": 5,
    },
    "bull_risk_off": {
        "active_universes": ["sp500", "sector_etfs", "treasury_etfs"],
        "strategy_types": ["mean_reversion", "trend_following"],
        "sizing_multiplier": 0.7,
        "max_positions": 4,
    },
    "transition_uncertain": {
        "active_universes": ["sector_etfs", "treasury_etfs", "gold_etfs"],
        "strategy_types": ["mean_reversion", "short_term_mr"],
        "sizing_multiplier": 0.5,
        "max_positions": 3,
    },
    "bear_risk_off": {
        "active_universes": ["treasury_etfs", "gold_etfs", "defensive_etfs"],
        "strategy_types": ["trend_following"],
        "sizing_multiplier": 0.5,
        "max_positions": 3,
    },
    "bear_capitulation": {
        "active_universes": ["treasury_etfs", "gold_etfs"],
        "strategy_types": ["trend_following"],
        "sizing_multiplier": 0.3,
        "max_positions": 2,
    },
    "recovery_early": {
        "active_universes": ["sp500", "sector_etfs", "commodity_etfs"],
        "strategy_types": ["momentum_breakout", "trend_following"],
        "sizing_multiplier": 0.7,
        "max_positions": 4,
    },
}
```

### Implementation files

| File | Purpose |
|---|---|
| `regime/model.py` | `RegimeModel` class — reads `macro_indicators` table, scores, classifies |
| `regime/states.py` | `RegimeState` enum + `REGIME_CONFIGS` mapping |
| `regime/indicators.py` | Individual indicator scoring functions |
| `regime/history.py` | Backfill historical classifications into `regime_history` table |
| `regime/backtest.py` | Regime-aware wrapper around `backtest/engine.py` |
| `config/active/regime.json` | Tunable parameters (thresholds, weights) |

---

## Layer 2: Multi-Asset Strategy Layer

### Universe definitions

```python
# universe/definitions.py

UNIVERSES = {
    "sp500":          {"method": "sp500_constituents", "top_n": 100},
    "sector_etfs":    {"method": "static", "tickers": ["XLF","XLE","XLK","XLV","XLI","XLC","XLY","XLP","XLU","XLB","XLRE"]},
    "treasury_etfs":  {"method": "static", "tickers": ["TLT","IEF","SHY","TIP","BND"]},
    "commodity_etfs": {"method": "static", "tickers": ["GLD","SLV","USO","XOP","CORN","DBA","DBB","UNG","CCJ","FCX"]},
    "gold_etfs":      {"method": "static", "tickers": ["GLD","IAU","GDX","GDXJ"]},
    "defensive_etfs": {"method": "static", "tickers": ["SH","PSQ","XLU","XLP","VIG","USMV"]},
}
```

### Key principle

**Strategies don't change.** `momentum_breakout.generate_signals()` receives OHLCV DataFrames. It doesn't know if those DataFrames are AAPL or TLT or GLD. The intelligence is in which universes are active, not in the strategies.

### Changes to existing modules

| Module | Change |
|---|---|
| `universe/builder.py` | Add `build_from_definition(universe_name)` reading from SQLite |
| `data/ingest.py` | Add `ingest_universe(name)` — fetch + write OHLCV to SQLite |
| `strategies/base.py` | Add `universe` field to `Signal` dataclass |
| `brokers/plan.py` | Accept `active_universes` list, merge data from SQLite |
| `research/best/` | Keyed by `(strategy, universe)` — stored in `research_best` table |

### Autoresearch expansion

- Sweeper accepts `--universe` flag to target specific universes
- `research_best` table keyed by `(strategy, universe)` — optimal params may differ per asset class
- New experiment type `regime_mapping_test` optimises regime thresholds
- Cross-universe correlation analysis in combined portfolio testing

---

## Layer 3: AI Overlay

### Asymmetric constraint

The AI **can only tighten** — reduce sizing, deactivate universes, flag tickers to avoid. It **cannot loosen** beyond what the regime model set. The worst case of an AI error is missed upside, never an unbacked loss.

### Data sources feeding the overlay

| Source | Tool | Frequency | How it routes |
|---|---|---|---|
| Geopolitical intel | `iran_monitor_cron.sh` (Pi agent) | 4-hourly | Existing — via Claude Max OAuth |
| Macro news | Brave/GDELT/Google RSS (existing) | Daily | Existing scripts |
| TradingView charts | Computer Use MCP (Pi agent) | Daily pre-market | Existing infra, new targets |
| Alternative data | Computer Use MCP (Pi agent) | Weekly | New cron scripts |
| Ceasefire tracker | `ceasefire_tracker.py` | 4-hourly | Pure Python, no LLM |

### Self-evaluation

Every overlay decision is stored in the `overlay_decisions` table. A weekly evaluation script:
1. Reads decisions from the past week
2. For each "tighten" decision, measures what would have happened without tightening
3. For each "no_change" decision, checks if tightening would have helped
4. Updates `outcome_evaluated`, `outcome_correct`, `outcome_notes`
5. Sends a Telegram summary of overlay accuracy

This is the Credibility Engine pattern turned inward.

---

## Computer Use Targets

Existing infrastructure (Xvfb + MCP server + Pi agent) expanded to new targets:

| Target | Script | Schedule | Output table |
|---|---|---|---|
| SSRN papers | `research/discovery/` (existing) | Wed | research_experiments |
| Quantpedia | `research/discovery/` (existing) | Fri | research_experiments |
| Quant blogs | `research/discovery/` (existing) | Sat | research_experiments |
| TradingView | `overlay/sources/chart_intel.py` (new) | 18:30 AEST Mon-Fri | Passed to overlay engine |
| OpenInsider | `overlay/sources/alt_data.py` (new) | Sun 09:00 AEST | news_intel table |
| Finviz screener | `overlay/sources/alt_data.py` (new) | Sun 09:00 AEST | news_intel table |

All computer use sessions run through Pi agent with Claude Max OAuth. No separate API costs.

---

## Portfolio Construction

With multiple universes active, the portfolio needs cross-asset awareness.

```python
# portfolio/constructor.py

UNIVERSE_LIMITS = {
    "sp500":          {"max_positions": 5, "max_pct_equity": 0.60},
    "sector_etfs":    {"max_positions": 3, "max_pct_equity": 0.30},
    "treasury_etfs":  {"max_positions": 2, "max_pct_equity": 0.40},
    "commodity_etfs": {"max_positions": 3, "max_pct_equity": 0.30},
    "gold_etfs":      {"max_positions": 2, "max_pct_equity": 0.20},
    "defensive_etfs": {"max_positions": 2, "max_pct_equity": 0.30},
}
```

Portfolio constructor runs inside plan generation:
1. Strategies produce signals across all active universes
2. Group by universe, apply per-universe limits
3. Check cross-universe correlation (don't triple-bet on energy via XOP + XLE + CVX)
4. Apply regime sizing × overlay sizing
5. Rank by confidence, output final plan
6. Write to `plans` table, notify via Telegram

---

## Strategy Lifecycle

Atlas strategies progress through 4 states tracked in the `strategy_lifecycle`
SQLite table (see `db/schema.sql`):

```
RESEARCH → PAPER → LIVE → RETIRED
```

### States

| State | Meaning |
|-------|---------|
| `RESEARCH` | Strategy under sweep; not authorized for any execution |
| `PAPER` | Strategy executes on Alpaca paper broker only; producing fills + PnL |
| `LIVE` | Strategy executes via Alpaca live broker with real capital |
| `RETIRED` | Strategy disabled; historic data preserved |

Transitions are enforced by `monitor/strategy_lifecycle.py` (`transition()`).
Only legal moves (RESEARCH→PAPER, PAPER→LIVE, LIVE→RETIRED, etc.) are allowed;
illegal transitions raise `ValueError`.

### Auto-promotion gates (PAPER → LIVE)

A strategy may auto-promote PAPER → LIVE only when ALL ten gates pass.
Gate constants live in `scripts/auto_promote_paper_to_live.py`; the full
evaluation function is `_evaluate_gates()` there, and the callable
`evaluate_and_promote()` is exported from `services/api/lifecycle.py`.

| Gate | Criterion | Default |
|------|-----------|---------|
| A | Days in PAPER state | ≥ 30 days |
| B | Paper trades in last 30 days | ≥ 30 trades |
| C | Paper Sharpe | ≥ 0.3 |
| D | Relative Sharpe gap vs research-best | < 50% |
| E | DSR per-strategy Sharpe variance | within tolerance |
| F | Research-best Sharpe | ≥ 0.5 |
| G | OOS Sharpe | ≥ 0.3 |
| H | OOS trade count | ≥ 30 |
| I | OOS CAGR | ≥ 5% |
| J | No active divergence alert in last N days | 7 days clean |

Gate J is evaluated by `_check_divergence_gate()` in
`scripts/auto_promote_paper_to_live.py`, reading `data/divergence_state.json`.

On all-gates-pass the script writes to `data/promotion_log.json`, calls
`monitor.strategy_lifecycle.transition()`, and sends a Telegram notification.
Manual override is available via `POST /api/strategy-lifecycle/promote-paper`
(`services/api/lifecycle.py`).

### Divergence rollback semantics

If a PAPER strategy's PnL diverges from research-best for ≥ 5 consecutive days,
auto-rollback fires PAPER → RESEARCH.
If a LIVE strategy diverges, a force-to-watch health escalation triggers
(operator must act; no silent LIVE → PAPER state flip).

Rollback logic: `scripts/check_live_research_divergence.py` —
`process_rollbacks()` and `run_divergence_check()`.
Divergence streaks are persisted in `data/divergence_state.json`.

### State transitions are append-only

The `strategy_lifecycle` table stores the **current** state (one row per
`strategy × universe` pair). Every transition is also written to
`strategy_lifecycle_history` (append-only; never deleted).

Current state query:
```sql
SELECT state FROM strategy_lifecycle WHERE strategy=? AND universe=?
```

Full history query:
```sql
SELECT * FROM strategy_lifecycle_history
WHERE strategy=? AND universe=? ORDER BY transitioned_at
```

---

## New File Structure

```
atlas/
├── db/                              # NEW — Data backbone
│   ├── __init__.py
│   ├── atlas_db.py                  # Typed access layer (all queries here)
│   ├── schema.sql                   # CREATE TABLE statements
│   └── migrate.py                   # One-time JSON → SQLite migration
│
├── regime/                          # NEW — Layer 1
│   ├── __init__.py
│   ├── model.py                     # RegimeModel class
│   ├── states.py                    # RegimeState enum + REGIME_CONFIGS
│   ├── indicators.py                # Indicator scoring
│   ├── history.py                   # Historical backfill
│   └── backtest.py                  # Regime-aware backtest wrapper
│
├── portfolio/                       # NEW — Cross-asset construction
│   ├── __init__.py
│   ├── constructor.py               # Multi-universe plan building
│   ├── correlation.py               # Cross-asset correlation checks
│   └── limits.py                    # Per-universe position limits
│
├── overlay/                         # NEW — Layer 3
│   ├── __init__.py
│   ├── engine.py                    # claude -p overlay call
│   ├── evaluator.py                 # Weekly self-evaluation
│   └── sources/
│       ├── chart_intel.py           # TradingView via computer use
│       ├── alt_data.py              # OpenInsider, Finviz via computer use
│       └── news.py                  # Wrapper around existing news_intel
│
├── universe/
│   ├── builder.py                   # EXTENDED — multi-universe from SQLite
│   └── definitions.py              # NEW — UNIVERSES dict
│
├── data/
│   ├── atlas.db                     # NEW — the single source of truth
│   ├── ingest.py                    # MODIFIED — writes to SQLite
│   ├── macro.py                     # MODIFIED — writes to SQLite
│   ├── fred.py                      # MODIFIED — writes to SQLite
│   └── ...
│
├── config/active/
│   ├── sp500.json                   # Stays as JSON — human-editable
│   ├── regime.json                  # NEW — regime parameters
│   └── universes.json               # NEW — universe definitions
│
├── services/
│   ├── dashboard_server.py          # MODIFIED — queries SQLite directly
│   └── ...
│
├── journal/
│   ├── logger.py                    # MODIFIED — writes to SQLite tables
│   └── [old JSON files → read-only backup]
│
├── [everything else unchanged]
```

---

## Build Sequence

### Phase 0: SQLite Foundation (week 1)

**Goal:** Stand up the database, migrate existing data, prove the access layer works. Everything else depends on this.

1. Create `db/schema.sql` with all table definitions
2. Create `db/atlas_db.py` access layer with core functions
3. Create `db/migrate.py` — import `trade_ledger.json`, `decision_journal.json`, `research/journal.json`, OHLCV snapshots
4. Verify migration: row counts, spot-check values
5. Modify `journal/logger.py` to write to SQLite instead of JSON
6. Modify `data/ingest.py` to write OHLCV to SQLite (keep file cache as fallback for 2 weeks)
7. Add 3 dashboard API endpoints (`/api/portfolio`, `/api/trades`, `/api/performance`) reading from SQLite
8. Run alongside existing JSON for 1 week to verify consistency

**Deliverable:** `atlas.db` with migrated data, core modules writing to SQLite, dashboard partially served from SQLite.

### Phase 1: Regime Model (weeks 2-3)

**Goal:** Classify every trading day since 2015. Validate classifications match known market regimes.

1. Create `regime/` module
2. Add missing FRED series (credit OAS, DXY) to `data/fred.py`, writing to `macro_indicators` table
3. Add VIX term structure to `data/macro.py`
4. Build historical classification → `regime_history` table
5. Visual validation: regime states overlaid on SPY chart
6. Create Pi agent skill `atlas-regime` for operational regime management

**Deliverable:** `regime_history` table populated 2015-present, regime model classifying today.

### Phase 2: Multi-Universe (weeks 4-5)

**Goal:** Strategies produce signals on all six universes. Backtest each independently.

1. Create `universe/definitions.py`
2. Extend `universe/builder.py` for static ticker lists
3. Extend `data/ingest.py` to fetch all universe tickers → SQLite
4. Add `universe` field to `Signal` dataclass
5. Backtest strategies individually on each ETF universe
6. Extend autoresearch sweeper with `--universe` flag

**Deliverable:** Backtest results for all strategy × universe combinations.

### Phase 3: Regime → Plan Wiring (weeks 6-7)

**Goal:** Plan generator reads regime state and scans only active universes with adjusted sizing.

1. Wire `regime/model.py` output into `brokers/plan.py`
2. Create `portfolio/constructor.py` with cross-universe limits
3. Create `regime/backtest.py` — regime-aware wrapper
4. Run full regime-aware backtest 2015-2026
5. Compare: regime-aware portfolio vs. SP500-only (Sharpe, max DD, bear market behaviour)
6. Add regime context to Telegram plan notifications

**Deliverable:** Regime-aware backtest results. Plan generator respects regime state.

### Phase 4: AI Overlay (weeks 8-9)

**Goal:** Claude-powered tightening layer running daily via Claude Max OAuth.

1. Create `overlay/engine.py` using `claude -p --output-format json`
2. Wire into daily cron — runs after regime model, writes `overlay_decisions` table
3. Create `overlay/sources/chart_intel.py` — TradingView via computer use
4. Create `overlay/evaluator.py` — weekly backscore
5. Add overlay context to plan approval messages
6. Run for 2 weeks in "log only" mode before letting it affect plans

**Deliverable:** Overlay running daily, logging decisions, with weekly evaluation.

### Phase 5: Dashboard + Research Expansion (weeks 10-12)

**Goal:** Full dashboard migration to SQLite API. Autoresearch across all universes.

1. Migrate remaining dashboard tabs to SQLite API endpoints
2. Add regime state visualisation to dashboard
3. Add universe breakdown to portfolio view
4. Retire `generate_data.py` (keep as backup script)
5. Run autoresearch across ETF universes
6. Add regime mapping optimisation to research pipeline
7. Create `overlay/sources/alt_data.py` — OpenInsider/Finviz via computer use

**Deliverable:** Full three-layer system in production. Dashboard fully SQLite-backed.

---

## Success Metrics

| Metric | Current | Target |
|---|---|---|
| Universes traded | 1 | 6 |
| Regime states | 1 (implicit) | 6 (explicit, backtested) |
| Data files to manage | 50+ JSONs | 1 SQLite file |
| Dashboard generation | 3,704-line script, periodic | Direct SQLite queries, real-time |
| API costs | $0 (Claude Max) | $0 (Claude Max — unchanged) |
| Bear market behaviour | Stops trading | Rotates to defensive assets |
| Backtest Sharpe (portfolio) | ~0.8 (SP500 only) | ≥0.6 (all-regime, all-asset) |
| Max drawdown (all regimes) | N/A | ≤15% |
| AI overlay value-add | N/A | Net positive over 6 months |

---

## Relationship to Cronus

Cronus connects at the portfolio construction layer:
- Reads regime state from `regime_history` table
- Cronus positions included in `portfolio_snapshots` for cross-asset exposure tracking
- AI overlay considers Cronus exposure when tightening
- Separate broker (IBKR), separate execution, separate seasonality logic
- Coordination via shared SQLite, not a merger

## Relationship to Moomoo Portfolio

The passive Moomoo portfolio stays manually managed. Over time:
- Regime model flags when conditions invalidate the energy thesis
- Overlay evaluator compares Moomoo performance vs. what regime model would have recommended
- If Atlas proves itself on commodity ETFs, some Moomoo positions could migrate

Not a near-term priority — don't fix what's working.

---

## Multi-Universe Consolidation — 2026-05-04 (PAUSED)

**Status**: PAUSED  
**Paused at**: 2026-05-04 (US market closed; configs flipped same evening)  
**Decision-maker**: User (operator), engineering executed end-to-end.

### Markets paused

| Market | Pre-pause status | Open positions at pause | Pause action |
|---|---|---|---|
| `commodity_etfs` | Live (v1.2) | GLD/momentum_breakout — 2 shares @ $442.80 | Mode flipped `live` → `passive`; OCO bracket maintained until close script runs |
| `sector_etfs` | Live (v1.0.2) | XLE/momentum_breakout (8 shares @ $59.06), XLI/momentum_breakout (9 shares @ $173.97) | Mode flipped `live` → `passive`; OCO brackets maintained until close script runs |

### Markets remaining live

- `sp500` (v3.2.1) — UNCHANGED. Active live trading universe.

### What "passive" means here

The configs use `trading.mode = "passive"` while keeping `trading.live_enabled = true`. This unusual combination is INTENTIONAL:

- `execute_approved.py:71-73` checks `mode != "live"` → skips when mode=passive → **no new entries**
- `sync_protective_orders.py`, `intraday_monitor.py`, `eod_settlement.py` check `live_enabled` → continue running → **OCO brackets keep being maintained on remaining open positions**

This is the safe in-between state for "pause new entries, keep protecting open positions" until the closure script runs. After positions close, Phase 3 will flip `live_enabled: false` and remove the per-market cron entries.

### Closure plan

`scripts/consolidation_close_positions.py` (added same commit window) is a manually-triggered operator script that:

1. Cancels OCO bracket orders at Alpaca for the 3 target tickers
2. Submits MARKET SELL orders for the position quantities
3. Updates `trades`, state files, `position_protective_orders` via `LivePortfolio.execute_exit`

Default mode is `--dry-run`. Operator runs `--live` during US RTH. Hard universe guard prevents the script from ever touching `sp500`.

### Re-enable criteria

A future operator decision to redeploy capital to these markets requires ALL of:

1. sp500 live performance shows sustained edge over 30+ trading days post-consolidation
2. `sector_etfs` and/or `commodity_etfs` `research_best` Sharpe ≥ 0.5 with passing OOS gates 1–4
3. Operator explicit decision to multi-universe again

### Reversibility

To re-enable a paused market:

1. Edit `config/active/<market>.json`: set `trading.mode = "live"`, `trading.auto_approve = true`, bump version to e.g. `vN.M.K-relive`
2. Restore the cron entries (see `git log --grep=consolidation -- scripts/atlas.crontab` for the removed lines)
3. Re-add the heartbeat watchdog entry in `config/heartbeat.json`
4. Apply: `sudo crontab /root/atlas/scripts/atlas.crontab`
5. Smoke-test: pre-market plan should generate within 24h, then operator manually approves first plan to validate the path

### Research-only mode

Note: even after pausing live, the **research sweep** for these markets continues (`atlas-research-window@<market>.timer`). `research_priorities.json` already tags them — keep this in mind during re-enable: research_best params will likely be more current than the saved live config.
