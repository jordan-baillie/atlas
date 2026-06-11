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
    model_version       TEXT,
    pending_state       TEXT    DEFAULT NULL  -- raw regime awaiting N-day confirmation
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
    updated_at      TEXT    DEFAULT (datetime('now')),
    stop_order_id   TEXT    DEFAULT '',
    tp_order_id     TEXT    DEFAULT '',
    superseded      INTEGER NOT NULL DEFAULT 0 CHECK (superseded IN (0,1)),
    CHECK (exit_date IS NULL OR exit_date >= entry_date),
    CHECK (
        stop_price IS NULL
        OR (direction = 'long'  AND stop_price < entry_price)
        OR (direction = 'short' AND stop_price > entry_price)
    )
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_dates ON trades(entry_date, exit_date);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_unique_open ON trades(ticker, universe) WHERE status='open';
CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_active_closed
  ON trades(ticker, strategy, DATE(exit_date), ROUND(pnl, 2))
  WHERE status = 'closed' AND superseded = 0;
-- Natural-key dedup index (#315): blocks reconciler from re-recording the same
-- logical fill across consecutive days. Key: ticker + fill-date + price + shares.
CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_natural_key
  ON trades(ticker, DATE(exit_date), exit_price, shares)
 WHERE exit_date IS NOT NULL AND status = 'closed';

-- Convenience view: all non-superseded trades (used by P&L consumers)
DROP VIEW IF EXISTS trades_active;
CREATE VIEW IF NOT EXISTS trades_active AS
  SELECT * FROM trades WHERE superseded = 0;

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
    source                  TEXT    DEFAULT 'eod',  -- 'eod', 'intraday', 'manual'
    market_id               TEXT    DEFAULT 'sp500', -- 'sp500', 'commodity_etfs', 'ALL' (aggregate)
    daily_pnl_pct           REAL    -- (total_equity - prev_total_equity) / prev_total_equity * 100
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON portfolio_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_market_ts
    ON portfolio_snapshots(market_id, timestamp);

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

-- NOTE: news_intel is a FUTURE FEATURE — no pipeline currently populates this table.
-- Empty state is expected and not a bug. Pipeline planned for Phase 6+.
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
    strategy         TEXT    NOT NULL,
    universe         TEXT    NOT NULL,
    regime_state     TEXT,              -- NULL = cross-regime fallback (legacy); non-NULL = per-regime best (Rec 5 2026-05-06)
    params           TEXT    NOT NULL,  -- JSON: best known parameters
    -- sharpe REAL,  -- DEPRECATED: whole-portfolio Sharpe; see solo_sharpe / portfolio_sharpe (M2 2026-04-28)
    sharpe           REAL,
    trades           INTEGER,
    max_dd_pct       REAL,
    updated_at       TEXT    DEFAULT (datetime('now')),
    solo_sharpe      REAL,              -- strategy-standalone Sharpe (M2 2026-04-28)
    portfolio_sharpe REAL,              -- whole-portfolio Sharpe with this strategy (M2 2026-04-28)
    metric_type      TEXT    NOT NULL DEFAULT 'unknown',  -- 'solo','portfolio','both','legacy_portfolio','portfolio_diversifier','unknown'
    oos_sharpe       REAL,              -- OOS Sharpe from time-period-split (gates G/H/I 2026-05-06)
    oos_trades       INTEGER,           -- OOS trade count
    oos_cagr         REAL,              -- OOS CAGR % (e.g. 5.2 = 5.2 %)
    oos_max_dd       REAL,              -- OOS max drawdown % (positive)
    PRIMARY KEY (strategy, universe, regime_state)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_best_cross_regime
    ON research_best (strategy, universe)
    WHERE regime_state IS NULL;
CREATE INDEX IF NOT EXISTS idx_research_best_regime
    ON research_best (strategy, universe, regime_state);

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

-- ═══════════════════════════════════════════════════════════
-- VOLATILITY CONES (Phase 3)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS vol_cones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    as_of TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    current_vol REAL,
    p5 REAL,
    p25 REAL,
    p50 REAL,
    p75 REAL,
    p95 REAL,
    n_obs INTEGER,
    lookback_years INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, as_of, horizon)
);
CREATE INDEX IF NOT EXISTS idx_vol_cones_ticker_asof ON vol_cones(ticker, as_of);

CREATE TABLE IF NOT EXISTS vol_regimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    as_of TEXT NOT NULL,
    regime TEXT NOT NULL,
    multiplier REAL,
    vol_20d REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, as_of)
);
CREATE INDEX IF NOT EXISTS idx_vol_regimes_ticker_asof ON vol_regimes(ticker, as_of);

-- ═══════════════════════════════════════════════════════════
-- TREASURY YIELD CURVE (Phase 3.1)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS treasury_curve (
    date                TEXT    PRIMARY KEY,  -- ISO date YYYY-MM-DD
    yield_1m            REAL,                 -- 1-month T-bill yield (%)
    yield_3m            REAL,                 -- 3-month T-bill yield (%)
    yield_6m            REAL,                 -- 6-month T-bill yield (%)
    yield_1y            REAL,                 -- 1-year Treasury yield (%)
    yield_2y            REAL,                 -- 2-year Treasury yield (%)
    yield_3y            REAL,                 -- 3-year Treasury yield (%)
    yield_5y            REAL,                 -- 5-year Treasury yield (%)
    yield_7y            REAL,                 -- 7-year Treasury yield (%)
    yield_10y           REAL,                 -- 10-year Treasury yield (%)
    yield_20y           REAL,                 -- 20-year Treasury yield (%)
    yield_30y           REAL,                 -- 30-year Treasury yield (%)
    treasury_slope      REAL,                 -- 10y - 2y spread
    treasury_curvature  REAL,                 -- (2y + 10y)/2 - 5y (butterfly)
    treasury_level      REAL,                 -- average across all 11 maturities
    updated_at          TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_treasury_curve_date ON treasury_curve(date);

-- ═══════════════════════════════════════════════════════════
-- BROKER STATE (Wave D2 — 2026-04-28)
-- JSON→SQLite dual-write target. JSON remains source of truth
-- until 5 consecutive daily PASSes on verify_dual_write.py.
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS market_state (
  market_id         TEXT    PRIMARY KEY,
  halted            INTEGER NOT NULL DEFAULT 0 CHECK (halted IN (0,1)),
  halt_reason       TEXT,
  halted_at         TEXT,
  mode              TEXT    NOT NULL DEFAULT 'paper' CHECK (mode IN ('live','paper','passive')),
  daily_high_water  REAL,
  hwm_date          TEXT,
  updated_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS equity_history (
  market_id  TEXT NOT NULL,
  date       TEXT NOT NULL,
  equity     REAL NOT NULL,
  pnl        REAL,
  PRIMARY KEY (market_id, date)
);
CREATE INDEX IF NOT EXISTS idx_equity_history_market_date ON equity_history(market_id, date);

-- ═══════════════════════════════════════════════════════════
-- OVERLAY SHADOW LOG (M3 — 2026-04-28)
-- Dry-run record of what overlay WOULD have done to sizing.
-- Plans are NEVER actually modified; this is observation only.
-- ═══════════════════════════════════════════════════════════

-- ── Shadow log — what overlay WOULD have done (M3 dry-run) ──
CREATE TABLE IF NOT EXISTS overlay_shadow_log (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id                     TEXT    NOT NULL,
    ticker                      TEXT    NOT NULL,
    market_id                   TEXT    NOT NULL,
    created_at                  TEXT    NOT NULL DEFAULT (datetime('now')),
    original_size               REAL    NOT NULL,
    overlay_size                REAL    NOT NULL,
    sizing_multiplier           REAL    NOT NULL,
    would_be_dollar_diff        REAL,
    overlay_decision_id         INTEGER,
    overlay_action              TEXT,
    overlay_reasoning           TEXT,
    actual_outcome_pnl          REAL,
    actual_outcome_evaluated    INTEGER NOT NULL DEFAULT 0,
    evaluated_at                TEXT,
    FOREIGN KEY (overlay_decision_id) REFERENCES overlay_decisions(id)
);
CREATE INDEX IF NOT EXISTS idx_shadow_unevaluated
    ON overlay_shadow_log(actual_outcome_evaluated, created_at);
CREATE INDEX IF NOT EXISTS idx_shadow_plan
    ON overlay_shadow_log(plan_id);

-- ═══════════════════════════════════════════════════════════
-- BROKER ORDERS CACHE (RCA #4A — 2026-04-29)
-- Local cache of Alpaca order/fill history for source-of-truth
-- reconciliation. Eliminates phantom-price inference bugs
-- (CHTR pattern). Populated by scripts/sync_broker_orders.py.
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS broker_orders (
    order_id           TEXT PRIMARY KEY,           -- Alpaca order UUID
    symbol             TEXT NOT NULL,              -- ticker (Atlas format)
    side               TEXT NOT NULL,              -- buy | sell
    qty                REAL NOT NULL,              -- requested qty
    filled_qty         REAL,                       -- actually filled (NULL if not filled)
    fill_price         REAL,                       -- avg fill price (NULL if not filled)
    status             TEXT NOT NULL,              -- accepted | filled | canceled | rejected | etc
    submitted_at       TEXT NOT NULL,              -- ISO timestamp
    filled_at          TEXT,                       -- ISO timestamp (NULL if not filled)
    order_class        TEXT,                       -- simple | bracket | oco | oto
    parent_id          TEXT,                       -- parent order ID for bracket children
    raw_alpaca_json    TEXT NOT NULL,              -- full Alpaca order JSON for forensic
    last_synced_at     TEXT NOT NULL               -- when this row was last upserted
);
CREATE INDEX IF NOT EXISTS idx_broker_orders_symbol ON broker_orders(symbol);
CREATE INDEX IF NOT EXISTS idx_broker_orders_status ON broker_orders(status);
CREATE INDEX IF NOT EXISTS idx_broker_orders_submitted_at ON broker_orders(submitted_at);
CREATE INDEX IF NOT EXISTS idx_broker_orders_parent_id ON broker_orders(parent_id);

-- ═══════════════════════════════════════════════════════════
-- PAPER BROKER ORDERS CACHE (2026-05-19)
-- Mirror of broker_orders for the Alpaca PAPER account.
-- Populated by scripts/sync_paper_orders.py every 5 min
-- during US RTH.  Enables paper_trades write-back for
-- PAPER-lifecycle strategies (unblocks paper→live validation).
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS paper_broker_orders (
    order_id           TEXT PRIMARY KEY,           -- Alpaca paper order UUID
    symbol             TEXT NOT NULL,              -- ticker (Atlas format)
    side               TEXT NOT NULL,              -- buy | sell
    qty                REAL NOT NULL,              -- requested qty
    filled_qty         REAL,                       -- actually filled (NULL if not filled)
    fill_price         REAL,                       -- avg fill price (NULL if not filled)
    status             TEXT NOT NULL,              -- new | filled | canceled | rejected | etc
    submitted_at       TEXT NOT NULL,              -- ISO timestamp
    filled_at          TEXT,                       -- ISO timestamp (NULL if not filled)
    order_class        TEXT,                       -- simple | bracket | oco | oto
    parent_id          TEXT,                       -- parent order ID for bracket children
    raw_alpaca_json    TEXT NOT NULL,              -- full Alpaca order JSON for forensic
    last_synced_at     TEXT NOT NULL               -- when this row was last upserted
);
CREATE INDEX IF NOT EXISTS idx_paper_broker_orders_symbol       ON paper_broker_orders(symbol);
CREATE INDEX IF NOT EXISTS idx_paper_broker_orders_status       ON paper_broker_orders(status);
CREATE INDEX IF NOT EXISTS idx_paper_broker_orders_submitted_at ON paper_broker_orders(submitted_at);
CREATE INDEX IF NOT EXISTS idx_paper_broker_orders_parent_id    ON paper_broker_orders(parent_id);

-- ── Position Protective Orders ────────────────────────────────────────────────
-- Single canonical row per open position tracking stop+TP order IDs from broker
-- truth. Eliminates multi-writer drift on trades.stop_order_id.
-- Phase A.1 — 2026-04-29
CREATE TABLE IF NOT EXISTS position_protective_orders (
    market_id       TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    trade_id        INTEGER,               -- FK to trades.id (nullable for legacy)
    position_qty    REAL NOT NULL,
    stop_order_id   TEXT,                  -- Alpaca order_id of stop
    stop_price      REAL,                  -- The stop trigger price
    tp_order_id     TEXT,                  -- Alpaca order_id of TP limit
    tp_price        REAL,                  -- The TP limit price
    oco_class       TEXT,                  -- 'oco' | 'bracket' | NULL (independent)
    last_synced_at  TEXT NOT NULL,         -- ISO timestamp of last sync from broker truth
    status          TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'closed' | 'detached'
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (market_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_protective_status   ON position_protective_orders(status);
CREATE INDEX IF NOT EXISTS idx_protective_trade_id ON position_protective_orders(trade_id);

-- ═══════════════════════════════════════════════════════════
-- CONFIG OVERRIDES (dashboard universe/strategy toggles — 2026-05-05)
-- DB-resident override layer on top of config/active/*.json.
-- Enables operators to toggle universe and strategy state from
-- the dashboard with a full audit trail.
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS config_overrides (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  scope        TEXT    NOT NULL CHECK(scope IN ('universe','strategy')),
  -- universe: market_id (e.g. 'sp500')
  -- strategy: 'market_id.strategy_name' (e.g. 'commodity_etfs.connors_rsi2')
  key          TEXT    NOT NULL,
  -- For scope='universe': 'live' | 'passive' | 'disabled'
  -- For scope='strategy': 'enabled' | 'disabled'
  state        TEXT    NOT NULL,
  reason       TEXT,                           -- mandatory at API layer; nullable at DB
  created_by   TEXT    NOT NULL,               -- 'human:<username>' | 'system' | 'telegram'
  created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
  -- Optional auto-expiry. NULL = never expires.
  expires_at   TEXT,
  -- Effective state immediately before this override was applied.
  prev_state   TEXT,
  -- Lifecycle: 1=active (consulted by readers), 0=superseded/reverted/expired.
  active       INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  ended_at     TEXT,
  ended_reason TEXT CHECK(ended_reason IN ('reverted','expired','superseded') OR ended_reason IS NULL)
);

-- Only one ACTIVE override per (scope, key). Historical rows are unconstrained.
CREATE UNIQUE INDEX IF NOT EXISTS uq_config_overrides_active
  ON config_overrides(scope, key) WHERE active = 1;

-- Sweep job index (find rows due for expiry).
CREATE INDEX IF NOT EXISTS idx_config_overrides_expires
  ON config_overrides(expires_at) WHERE active = 1 AND expires_at IS NOT NULL;

-- Lookup index for read-side resolution.
CREATE INDEX IF NOT EXISTS idx_config_overrides_lookup
  ON config_overrides(scope, key, active);


CREATE TABLE IF NOT EXISTS config_override_audit (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL DEFAULT (datetime('now')),
  override_id  INTEGER REFERENCES config_overrides(id),
  scope        TEXT NOT NULL,
  key          TEXT NOT NULL,
  action       TEXT NOT NULL CHECK(action IN ('create','revert','expire','supersede')),
  from_state   TEXT,
  to_state     TEXT,
  reason       TEXT,
  actor        TEXT NOT NULL,
  source       TEXT NOT NULL CHECK(source IN ('dashboard','cli','telegram','sweep')),
  remote_ip    TEXT,
  payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_config_override_audit_ts ON config_override_audit(ts DESC);
CREATE INDEX IF NOT EXISTS idx_config_override_audit_key ON config_override_audit(scope, key, ts DESC);

-- Immutability — model copied verbatim from fix_audit_log.
CREATE TRIGGER IF NOT EXISTS config_override_audit_no_update
  BEFORE UPDATE ON config_override_audit
  BEGIN SELECT RAISE(ABORT, 'config_override_audit is immutable (append-only)'); END;

CREATE TRIGGER IF NOT EXISTS config_override_audit_no_delete
  BEFORE DELETE ON config_override_audit
  BEGIN SELECT RAISE(ABORT, 'config_override_audit is immutable (append-only)'); END;

-- ═══════════════════════════════════════════════════════════
-- STRATEGY LIFECYCLE — promotion stage state machine
-- Tracks (strategy, universe) tuples through activation stages:
--   RESEARCH → PAPER → LIVE → RETIRED
--
-- SEPARATE from monitor/lifecycle.py which tracks operational
-- health of LIVE strategies (RAMP_UP / ACTIVE / WATCH / PROBATION
-- / SUSPENDED). These answer DIFFERENT questions:
--   - Promotion lifecycle: where in activation pipeline?
--   - Health lifecycle: is this LIVE strategy performing OK?
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS strategy_lifecycle (
    strategy             TEXT NOT NULL,
    universe             TEXT NOT NULL,
    state                TEXT NOT NULL CHECK (state IN ('RESEARCH', 'PAPER', 'LIVE', 'RETIRED')),
    entered_state_at     TEXT NOT NULL,        -- ISO datetime of last transition
    prev_state           TEXT,                 -- state before this transition
    transition_reason    TEXT,                 -- human/system-readable reason
    paper_start_date     TEXT,                 -- set on RESEARCH→PAPER transition
    paper_end_date       TEXT,                 -- set on PAPER→LIVE transition
    auto_promotion_id    TEXT,                 -- link to auto_promote run that triggered
    notes                TEXT,
    PRIMARY KEY (strategy, universe)
);

CREATE TABLE IF NOT EXISTS strategy_lifecycle_history (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy             TEXT NOT NULL,
    universe             TEXT NOT NULL,
    from_state           TEXT,                 -- NULL on initial seed
    to_state             TEXT NOT NULL,
    transitioned_at      TEXT NOT NULL,        -- ISO datetime
    reason               TEXT,
    auto_promotion_id    TEXT,
    operator             TEXT,                 -- 'system' | 'manual' | 'rollback' | operator name
    gate_results         TEXT,                 -- JSON: {A:'pass',B:'pass',...,J:'fail'}  (Phase 3)
    experiment_id        TEXT                  -- link to research/experiments/*.json that drove the transition (Phase 3)
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_history_strategy
    ON strategy_lifecycle_history(strategy, universe, transitioned_at);

-- ═══════════════════════════════════════════════════════════
-- PAPER TRADING TABLES
-- Exact mirrors of `trades` and `position_protective_orders`
-- for strategy paper-trading runs.  Schema added 2026-05-06.
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS paper_trades (
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
    updated_at      TEXT    DEFAULT (datetime('now')),
    stop_order_id   TEXT    DEFAULT '',
    tp_order_id     TEXT    DEFAULT '',
    superseded      INTEGER NOT NULL DEFAULT 0 CHECK (superseded IN (0,1)),
    paper_account_id TEXT,             -- Alpaca paper account number (e.g. "PA3TTBLZM6M7")
    CHECK (exit_date IS NULL OR exit_date >= entry_date),
    CHECK (
        stop_price IS NULL
        OR (direction = 'long'  AND stop_price < entry_price)
        OR (direction = 'short' AND stop_price > entry_price)
    )
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_status   ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_trades_strategy ON paper_trades(strategy);
CREATE INDEX IF NOT EXISTS idx_paper_trades_dates    ON paper_trades(entry_date, exit_date);
CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_trades_unique_open
    ON paper_trades(ticker, universe) WHERE status='open';
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_trades_active_closed
    ON paper_trades(ticker, strategy, DATE(exit_date), ROUND(pnl, 2))
    WHERE status = 'closed' AND superseded = 0;

-- Convenience view: non-superseded paper trades (mirrors trades_active)
DROP VIEW IF EXISTS paper_trades_active;
CREATE VIEW IF NOT EXISTS paper_trades_active AS
  SELECT * FROM paper_trades WHERE superseded = 0;

CREATE TABLE IF NOT EXISTS paper_position_protective_orders (
    market_id       TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    trade_id        INTEGER,               -- FK to paper_trades.id (nullable for legacy)
    position_qty    REAL NOT NULL,
    stop_order_id   TEXT,                  -- Alpaca order_id of stop
    stop_price      REAL,                  -- The stop trigger price
    tp_order_id     TEXT,                  -- Alpaca order_id of TP limit
    tp_price        REAL,                  -- The TP limit price
    oco_class       TEXT,                  -- 'oco' | 'bracket' | NULL (independent)
    last_synced_at  TEXT NOT NULL,         -- ISO timestamp of last sync from broker truth
    status          TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'closed' | 'detached'
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (market_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_paper_protective_status
    ON paper_position_protective_orders(status);
CREATE INDEX IF NOT EXISTS idx_paper_protective_trade_id
    ON paper_position_protective_orders(trade_id);


-- ═══════════════════════════════════════════════════════════
-- TELEGRAM MESSAGE CAPTURE (bidirectional observability)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS telegram_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    direction    TEXT NOT NULL CHECK (direction IN ('outbound', 'inbound')),
    chat_id      TEXT NOT NULL,
    message_id   INTEGER,
    user_id      TEXT,
    username     TEXT,
    body         TEXT NOT NULL,
    parse_mode   TEXT,
    sent_at      TEXT NOT NULL,
    api_status   INTEGER,
    api_error    TEXT,
    is_command   INTEGER DEFAULT 0,
    command_name TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tgm_chat_time ON telegram_messages(chat_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_tgm_direction_time ON telegram_messages(direction, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_tgm_command ON telegram_messages(command_name) WHERE command_name IS NOT NULL;


-- ═══════════════════════════════════════════════════════════
-- RESEARCH KNOWLEDGE LAYER
-- Source-of-truth for external claims about strategies (papers,
-- blogs, internal docs) and divergences between those claims and
-- Atlas's own measured results in research_best.  Phase 0 of the
-- research-system consolidation; see docs/specs/research-db-consolidation.md.
-- ═══════════════════════════════════════════════════════════

-- Ingested source (paper, blog, internal doc).  One row per source;
-- dedup by sha256 of canonical payload.
CREATE TABLE IF NOT EXISTS sources (
    id            TEXT    PRIMARY KEY,           -- 'src-arxiv-<id>' or 'src-<sha8>'
    kind          TEXT    NOT NULL,              -- 'paper' | 'blog' | 'doc' | 'internal'
    url           TEXT,
    title         TEXT    NOT NULL,
    authors       TEXT,                          -- JSON array of strings
    venue         TEXT,                          -- 'arxiv' | 'ssrn' | 'quantpedia' | ...
    published_at  TEXT,                          -- ISO date, NULL if unknown
    sha256        TEXT    UNIQUE,                -- canonical PDF/HTML hash; dedup key
    local_path    TEXT,                          -- relative to atlas root; NULL if remote-only
    ingested_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    extracted_by  TEXT,                          -- 'pdf_vision' | 'text_summary' | 'manual' | ...
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_sources_kind ON sources(kind);
CREATE INDEX IF NOT EXISTS idx_sources_published ON sources(published_at DESC);

-- Structured claim made by a source about a strategy.  External
-- assertion only -- research_best holds Atlas's own measurements.
-- A single paper can yield N claims (different strategies / windows).
CREATE TABLE IF NOT EXISTS claims (
    id                     TEXT    PRIMARY KEY,  -- 'clm-<src_id>-<strategy>-<n>'
    source_id              TEXT    NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    strategy               TEXT    NOT NULL,     -- normalised to STRATEGY_UNIVERSE keys
    universe               TEXT,                 -- normalised; NULL = paper unspecified
    regime_state           TEXT,                 -- NULL = cross-regime claim
    period_start           TEXT,                 -- ISO date; claim's backtest window start
    period_end             TEXT,                 -- ISO date; claim's backtest window end
    claimed_sharpe         REAL,
    claimed_solo_sharpe    REAL,
    claimed_max_dd_pct     REAL,
    claimed_trades         INTEGER,
    claimed_cagr_pct       REAL,
    claimed_profit_factor  REAL,
    claimed_avg_hold_days  REAL,
    extraction_confidence  TEXT    DEFAULT 'medium',  -- 'low' | 'medium' | 'high'
    status                 TEXT    NOT NULL DEFAULT 'active',
                                                 -- 'active' | 'dismissed' | 'superseded'
    dismissed_reason       TEXT,
    notes                  TEXT,
    created_at             TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_claims_strategy ON claims(strategy);
CREATE INDEX IF NOT EXISTS idx_claims_source ON claims(source_id);
CREATE INDEX IF NOT EXISTS idx_claims_active
    ON claims(strategy, universe) WHERE status = 'active';

-- Materialised contradictions: divergence between a claim and the
-- measured row in research_best.  Populated by sync_contradictions().
-- Materialised (not just a view) so resolutions have an explicit lifecycle.
CREATE TABLE IF NOT EXISTS contradictions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id            TEXT    NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    strategy            TEXT    NOT NULL,
    universe            TEXT    NOT NULL,
    metric              TEXT    NOT NULL,        -- 'sharpe' | 'max_dd_pct' | 'cagr_pct' | 'trades'
    claimed_value       REAL,
    measured_value      REAL,
    delta               REAL,                    -- measured - claimed
    delta_abs           REAL,                    -- |delta|, for ranking
    severity            TEXT    NOT NULL,        -- 'minor' | 'major' | 'critical'
    first_seen_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    last_checked_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    resolution          TEXT,                    -- NULL | 'retested' | 'claim_rejected' | 'measurement_corrected' | 'deferred'
    resolution_note     TEXT,
    resolved_at         TEXT,
    UNIQUE(claim_id, metric)                     -- one row per (claim, metric) pair
);
CREATE INDEX IF NOT EXISTS idx_contradictions_unresolved
    ON contradictions(strategy, severity) WHERE resolution IS NULL;
CREATE INDEX IF NOT EXISTS idx_contradictions_recent
    ON contradictions(first_seen_at DESC);

-- Strategy lifecycle state transitions live in the existing
-- strategy_lifecycle_history table (defined earlier in this file).
-- Phase 3 extends that table with the Phase 0 fields below; Phase 3
-- backfills historical promotion_log.json entries into the same table.
-- No separate lifecycle_events table -- one source of truth.

-- One row per Telegram digest send.  Powers dedup ("did we already
-- notify about contradiction X?") and rate limiting.
CREATE TABLE IF NOT EXISTS digest_history (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at                  TEXT    NOT NULL DEFAULT (datetime('now')),
    kind                     TEXT    NOT NULL,   -- 'daily' | 'weekly' | 'alert'
    new_papers               INTEGER NOT NULL DEFAULT 0,
    new_experiments          INTEGER NOT NULL DEFAULT 0,
    new_contradictions       INTEGER NOT NULL DEFAULT 0,
    lifecycle_transitions    INTEGER NOT NULL DEFAULT 0,
    summary                  TEXT,
    delivery_status          TEXT,               -- 'ok' | 'failed:<reason>'
    payload                  TEXT                -- JSON blob of full content sent
);
CREATE INDEX IF NOT EXISTS idx_digest_sent ON digest_history(sent_at DESC);


-- ═══════════════════════════════════════════════════════════
-- PHASE 6: SQL mirrors of the queue.json / journal.json stores.
-- Dual-write is opt-in via ATLAS_KNOWLEDGE_DB_QUEUE / ATLAS_KNOWLEDGE_DB_JOURNAL
-- env vars (research/models.py).  Until those flip, the JSON files remain the
-- canonical source of truth -- these tables just shadow them so operators can
-- compare row counts and rehearse the cutover.  Once stable, the JSON paths
-- are retired and these tables become canonical.
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS queue_mirror (
    id                    TEXT    PRIMARY KEY,             -- QueueEntry.id
    title                 TEXT    NOT NULL,
    category              TEXT    NOT NULL,
    market                TEXT    NOT NULL,
    hypothesis            TEXT,
    method                TEXT    NOT NULL,                -- ExperimentType value
    acceptance_criteria   TEXT,                            -- JSON
    estimated_runtime_min INTEGER NOT NULL DEFAULT 0,
    priority              TEXT    NOT NULL,                -- P1-P5
    status                TEXT    NOT NULL,                -- ExperimentStatus value
    strategy_name         TEXT,
    params_override       TEXT,                            -- JSON
    config_snapshot       TEXT,                            -- JSON
    claimed_by            TEXT,
    claimed_at            TEXT,
    tags                  TEXT,                            -- JSON array
    depends_on            TEXT,                            -- JSON array
    notes                 TEXT,
    payload               TEXT    NOT NULL,                -- full QueueEntry as JSON (canonical)
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL,
    mirrored_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_queue_mirror_status ON queue_mirror(status);
CREATE INDEX IF NOT EXISTS idx_queue_mirror_strategy ON queue_mirror(strategy_name);
CREATE INDEX IF NOT EXISTS idx_queue_mirror_category ON queue_mirror(category);
CREATE INDEX IF NOT EXISTS idx_queue_mirror_priority ON queue_mirror(priority, status);

CREATE TABLE IF NOT EXISTS journal_mirror (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id     TEXT    NOT NULL,
    timestamp         TEXT    NOT NULL,
    market            TEXT    NOT NULL,
    category          TEXT    NOT NULL,
    strategy          TEXT,
    hypothesis        TEXT,
    verdict           TEXT,                                -- pass | fail | partial | deferred
    key_metrics       TEXT,                                -- JSON
    delta_vs_baseline TEXT,                                -- JSON
    learnings         TEXT,                                -- JSON array
    promoted          INTEGER NOT NULL DEFAULT 0 CHECK (promoted IN (0, 1)),
    runtime_s         REAL,
    agent_id          TEXT,
    payload           TEXT    NOT NULL,                    -- full JournalEntry as JSON
    mirrored_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(experiment_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_journal_mirror_experiment ON journal_mirror(experiment_id);
CREATE INDEX IF NOT EXISTS idx_journal_mirror_ts ON journal_mirror(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_journal_mirror_strategy ON journal_mirror(strategy, timestamp DESC);

-- Candidate contradictions view.  Computes (claim x research_best)
-- deltas with severity classification.  Read by sync_contradictions()
-- which INSERTs the WHERE severity IS NOT NULL rows into contradictions.
-- COALESCE(solo_sharpe, sharpe) mirrors db/research.py::get_research_best
-- behaviour (solo_sharpe is the post-M2 canonical column).
DROP VIEW IF EXISTS v_candidate_contradictions;
CREATE VIEW v_candidate_contradictions AS
SELECT
    c.id                                                                AS claim_id,
    c.strategy                                                          AS strategy,
    COALESCE(c.universe, rb.universe)                                   AS universe,
    'sharpe'                                                            AS metric,
    c.claimed_sharpe                                                    AS claimed_value,
    COALESCE(rb.solo_sharpe, rb.sharpe)                                 AS measured_value,
    COALESCE(rb.solo_sharpe, rb.sharpe) - c.claimed_sharpe              AS delta,
    ABS(COALESCE(rb.solo_sharpe, rb.sharpe) - c.claimed_sharpe)         AS delta_abs,
    CASE
        WHEN ABS(COALESCE(rb.solo_sharpe, rb.sharpe) - c.claimed_sharpe) >= 1.0 THEN 'critical'
        WHEN ABS(COALESCE(rb.solo_sharpe, rb.sharpe) - c.claimed_sharpe) >= 0.5 THEN 'major'
        WHEN ABS(COALESCE(rb.solo_sharpe, rb.sharpe) - c.claimed_sharpe) >= 0.3 THEN 'minor'
        ELSE NULL
    END                                                                 AS severity
FROM claims c
JOIN research_best rb
    ON rb.strategy = c.strategy
   AND (c.universe IS NULL OR rb.universe = c.universe)
   AND (c.regime_state IS rb.regime_state)
WHERE c.status = 'active'
  AND c.claimed_sharpe IS NOT NULL
  AND COALESCE(rb.solo_sharpe, rb.sharpe) IS NOT NULL

UNION ALL

SELECT
    c.id, c.strategy,
    COALESCE(c.universe, rb.universe),
    'max_dd_pct',
    c.claimed_max_dd_pct,
    rb.max_dd_pct,
    rb.max_dd_pct - c.claimed_max_dd_pct,
    ABS(rb.max_dd_pct - c.claimed_max_dd_pct),
    CASE
        WHEN ABS(rb.max_dd_pct - c.claimed_max_dd_pct) >= 15 THEN 'critical'
        WHEN ABS(rb.max_dd_pct - c.claimed_max_dd_pct) >= 8  THEN 'major'
        WHEN ABS(rb.max_dd_pct - c.claimed_max_dd_pct) >= 5  THEN 'minor'
        ELSE NULL
    END
FROM claims c
JOIN research_best rb
    ON rb.strategy = c.strategy
   AND (c.universe IS NULL OR rb.universe = c.universe)
   AND (c.regime_state IS rb.regime_state)
WHERE c.status = 'active'
  AND c.claimed_max_dd_pct IS NOT NULL
  AND rb.max_dd_pct IS NOT NULL;

-- Operator-facing view: unresolved contradictions joined to source info.
DROP VIEW IF EXISTS v_open_contradictions;
CREATE VIEW v_open_contradictions AS
SELECT
    co.id              AS contradiction_id,
    co.claim_id,
    co.strategy,
    co.universe,
    co.metric,
    co.claimed_value,
    co.measured_value,
    co.delta,
    co.delta_abs,
    co.severity,
    co.first_seen_at,
    co.last_checked_at,
    cl.source_id,
    s.title            AS source_title,
    s.url              AS source_url,
    s.published_at     AS source_published_at
FROM contradictions co
JOIN claims cl  ON cl.id = co.claim_id
JOIN sources s  ON s.id  = cl.source_id
WHERE co.resolution IS NULL
ORDER BY
    CASE co.severity WHEN 'critical' THEN 0 WHEN 'major' THEN 1 ELSE 2 END,
    co.delta_abs DESC;

-- Per-strategy roll-up.  Powers the wiki materializer (Phase 7) and
-- operator dashboard.  One row per (strategy, universe) cross-regime.
DROP VIEW IF EXISTS v_strategy_summary;
CREATE VIEW v_strategy_summary AS
SELECT
    rb.strategy,
    rb.universe,
    rb.solo_sharpe,
    rb.portfolio_sharpe,
    rb.max_dd_pct,
    rb.trades,
    rb.updated_at                                                       AS last_measured_at,
    (SELECT COUNT(*) FROM claims c
        WHERE c.strategy = rb.strategy AND c.status = 'active')         AS active_claims,
    (SELECT COUNT(*) FROM contradictions co
        JOIN claims c ON c.id = co.claim_id
        WHERE c.strategy = rb.strategy AND co.resolution IS NULL)       AS open_contradictions,
    (SELECT to_state FROM strategy_lifecycle_history le
        WHERE le.strategy = rb.strategy AND le.universe = rb.universe
        ORDER BY le.transitioned_at DESC, le.id DESC LIMIT 1)           AS lifecycle_state
FROM research_best rb
WHERE rb.regime_state IS NULL;
