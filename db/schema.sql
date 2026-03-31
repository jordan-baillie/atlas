-- Atlas v2.0 — SQLite Schema
-- All tables use IF NOT EXISTS for idempotent init
-- Generated from docs/ARCHITECTURE.md

-- ═══════════════════════════════════════════════════════════
-- SCHEMA VERSION
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT    DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);

-- ═══════════════════════════════════════════════════════════
-- PRICE DATA
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS ohlcv (
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
CREATE INDEX IF NOT EXISTS idx_ohlcv_date ON ohlcv(date);
CREATE INDEX IF NOT EXISTS idx_ohlcv_universe ON ohlcv(universe, date);

-- ═══════════════════════════════════════════════════════════
-- MACRO INDICATORS (Layer 1 input)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS macro_indicators (
    date                TEXT    PRIMARY KEY,  -- ISO date
    vix                 REAL,
    vix3m               REAL,
    vix_term_ratio      REAL,              -- VIX/VIX3M
    yield_10y           REAL,
    yield_2y            REAL,
    yield_3m            REAL,
    yield_curve_10y2y   REAL,
    yield_curve_10y3m   REAL,
    credit_oas          REAL,              -- IG credit OAS (BAMLC0A0CM)
    dxy                 REAL,              -- Dollar index
    gold                REAL,
    copper              REAL,
    gold_copper_ratio   REAL,
    fed_funds           REAL,
    unemployment_claims INTEGER,
    spy_close           REAL,
    spy_200dma          REAL,
    spy_above_200dma    INTEGER,           -- 0/1
    spy_200dma_slope    REAL,              -- 20-day slope
    updated_at          TEXT    DEFAULT (datetime('now'))
);

-- ═══════════════════════════════════════════════════════════
-- REGIME HISTORY (Layer 1 output)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS regime_history (
    date                TEXT    PRIMARY KEY,
    regime_state        TEXT    NOT NULL,  -- enum: bull_risk_on, bull_risk_off, etc.
    trend_score         REAL,
    risk_score          REAL,
    active_universes    TEXT,              -- JSON array: ["sp500","sector_etfs"]
    sizing_multiplier   REAL    DEFAULT 1.0,
    enabled_strategies  TEXT,              -- JSON array
    reasoning           TEXT,
    model_version       TEXT
);
CREATE INDEX IF NOT EXISTS idx_regime_state ON regime_history(regime_state);

-- ═══════════════════════════════════════════════════════════
-- TRADING
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS signals (
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
CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy);

CREATE TABLE IF NOT EXISTS trades (
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
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_dates ON trades(entry_date, exit_date);

CREATE TABLE IF NOT EXISTS plans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT    NOT NULL,
    market_id       TEXT    NOT NULL,
    regime_state    TEXT,
    active_universes    TEXT,          -- JSON array
    sizing_multiplier   REAL,
    overlay_applied     INTEGER DEFAULT 0,
    overlay_adjustments TEXT,          -- JSON
    plan_data           TEXT    NOT NULL,  -- Full plan JSON (signals, risk summary)
    status              TEXT    DEFAULT 'pending',  -- 'pending', 'approved', 'rejected', 'executed'
    approved_at         TEXT,
    executed_at         TEXT,
    created_at          TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_plans_date ON plans(date, market_id);

-- ═══════════════════════════════════════════════════════════
-- PORTFOLIO STATE
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS equity_curve (
    date            TEXT    NOT NULL,
    market_id       TEXT    NOT NULL,
    equity          REAL    NOT NULL,
    cash            REAL,
    positions_value REAL,
    day_pnl         REAL,
    regime_state    TEXT,
    PRIMARY KEY (date, market_id)
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp               TEXT    NOT NULL,
    total_equity            REAL,
    cash                    REAL,
    positions               TEXT,  -- JSON: [{ticker, shares, value, universe}]
    exposure_by_universe    TEXT,  -- JSON: {sp500: 0.45, treasury_etfs: 0.20}
    exposure_by_sector      TEXT,  -- JSON: {energy: 0.15, tech: 0.10}
    regime_state            TEXT,
    source                  TEXT    DEFAULT 'eod'  -- 'eod', 'intraday', 'manual'
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON portfolio_snapshots(timestamp);

-- ═══════════════════════════════════════════════════════════
-- AI OVERLAY (Layer 3)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS overlay_decisions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp               TEXT    NOT NULL,
    regime_state            TEXT    NOT NULL,
    action                  TEXT    NOT NULL,  -- 'no_change', 'tighten'
    sizing_override         REAL,
    universes_deactivated   TEXT,              -- JSON array
    tickers_avoided         TEXT,              -- JSON array
    reasoning               TEXT,
    confidence              REAL,
    data_sources            TEXT,              -- JSON: what inputs were used
    -- Outcome evaluation (filled in by weekly evaluator)
    outcome_evaluated       INTEGER DEFAULT 0,
    outcome_correct         INTEGER,           -- 0/1
    outcome_notes           TEXT,
    evaluated_at            TEXT
);
CREATE INDEX IF NOT EXISTS idx_overlay_ts ON overlay_decisions(timestamp);

-- ═══════════════════════════════════════════════════════════
-- GEOPOLITICAL MONITOR
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS ceasefire_factors (
    id          TEXT    PRIMARY KEY,
    category    TEXT    NOT NULL,  -- 'ceasefire', 'escalation'
    description TEXT    NOT NULL,
    weight      REAL    NOT NULL,
    active      INTEGER DEFAULT 0,
    confidence  TEXT    DEFAULT 'medium',
    source      TEXT,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS ceasefire_history (
    timestamp       TEXT    PRIMARY KEY,
    probability     REAL    NOT NULL,
    active_factors  TEXT,              -- JSON array of factor IDs
    change_log      TEXT               -- What changed
);

CREATE TABLE IF NOT EXISTS news_intel (
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
CREATE INDEX IF NOT EXISTS idx_news_ts ON news_intel(timestamp);
CREATE INDEX IF NOT EXISTS idx_news_category ON news_intel(category);

-- ═══════════════════════════════════════════════════════════
-- RESEARCH
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS research_experiments (
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
    status          TEXT    DEFAULT 'running',  -- 'running','kept','discarded','error'
    recommendation  TEXT,
    -- Metadata
    baseline_sharpe REAL,
    runtime_s       REAL,
    agent_id        TEXT,
    created_at      TEXT    DEFAULT (datetime('now')),
    completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_experiments_strategy ON research_experiments(strategy);
CREATE INDEX IF NOT EXISTS idx_experiments_status ON research_experiments(status);

CREATE TABLE IF NOT EXISTS research_best (
    strategy    TEXT    NOT NULL,
    universe    TEXT    NOT NULL,
    params      TEXT    NOT NULL,  -- JSON: best known parameters
    sharpe      REAL,
    trades      INTEGER,
    max_dd_pct  REAL,
    updated_at  TEXT    DEFAULT (datetime('now')),
    PRIMARY KEY (strategy, universe)
);

-- ═══════════════════════════════════════════════════════════
-- SYSTEM
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS heartbeats (
    service     TEXT    PRIMARY KEY,
    timestamp   TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    detail      TEXT               -- JSON
);

CREATE TABLE IF NOT EXISTS system_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    DEFAULT (datetime('now')),
    level       TEXT    NOT NULL,  -- 'info', 'warning', 'error', 'critical'
    service     TEXT    NOT NULL,
    message     TEXT,
    detail      TEXT               -- JSON
);
CREATE INDEX IF NOT EXISTS idx_syslog_ts ON system_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_syslog_service ON system_log(service);
