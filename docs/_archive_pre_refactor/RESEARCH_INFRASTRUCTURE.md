# Atlas Research Infrastructure — Agent Runbook (built 2026-06)

> **Read this before running any research/backtest.** It documents the rail-equipped validation
> pipeline, the survivorship-free data, the markets, and the golden rules. Everything here is the
> standard as of 2026-06; older sp500-only/yfinance workflows are survivorship-biased (see #Markets).

---

## TL;DR — run a trustworthy validated backtest

```bash
python3 scripts/run_strategy_battery.py \
    --strategy <name> --market shm \
    --grid-size 12 --max-positions 35 --select default \
    --holdout-eval \
    --output-path backtest/results/search/battery_<name>_shm.json
```

That single command applies ALL THREE integrity rails + the write-once holdout. The artifact's
`verdict` is the trustworthy answer. **Do not trust a raw battery tier without the holdout** (see
Rails). For a multi-strategy sweep use `scripts/run_search.py --market shm`.

---

## Markets (what data exists)

`vo.load_data(market=X)` reads `data/cache/X/*.parquet`. Pick the market with `--market X`.

| Market | What | Use | Survivorship |
|---|---|---|---|
| **`shm`** | Sharadar survivorship-correct mid/small-cap, 609 names 2016-2026, **235 in-window delisted** | **RESEARCH DEFAULT** (`config/research_default.json`) | ✅ correct (full delisted) |
| `sp500` | 203 current top-liquid large-caps | LIVE/current-tradable universe only | ❌ biased (survivors) |
| `sp500hist` | yfinance S&P500 historical, ~617 names | partial-correction large-cap | ⚠️ partial (~32% delisted) |
| `sector_etfs`/`commodity_etfs`/… | static ETF baskets | other live markets | n/a |

**Why shm is the default:** momentum on biased `sp500` gave CPCV +1.04 (a MIRAGE — it collapsed to
−0.06 on clean large-cap and the rails caught it). Research must run on survivorship-correct data or
results are invalid. The bias is UPSTREAM of every gate — only clean data fixes it.

Each market needs: `config/active/<market>.json` + `data/processed/sector_map_<market>.json`
(REQUIRED — without sectors, `max_sector_concentration` collapses the book to ~2 positions; see
Building a strategy). `shm` config models small-cap slippage (0.0015, volume-aware).

---

## The 3 integrity rails (all baked into `run_strategy_battery.run_battery`)

Code: `research/cross_oos/`. Spec: `research/INTEGRITY_RAILS_SPEC.md`. They make unlimited free search
safe (free compute + biased/ungated search = a mirage generator).

**Rail 1 — Write-once holdout** (`holdout.py`, `config/holdout.json`, ledger `research/holdout_ledger.jsonl`)
- Search is QUARANTINED to data `< holdout_start` (2025-01-01) by default. `--no-holdout-quarantine`
  to override (manual full-data only; never the loop).
- A PROMOTE candidate is evaluated on the 2025-26 holdout **ONCE** via `--holdout-eval` (single-use
  ledger; a burned config CANNOT be re-tested — needs a genuinely new hypothesis, no re-peeking).
- **CRITICAL:** in-search OOS (the time-split WITHIN the search period) is CONTAMINATED by
  strategy/factor selection and can pass while overfit. The quarantined holdout is the ONLY
  incorruptible arbiter. 2026-06-06: a candidate cleared DSR 0.986 + FDR bar + in-search OOS (0.77)
  yet FAILED the holdout (−1.21). **A candidate is NOT validated until it clears the holdout.**

**Rail 2 — FDR-aware promote bar + hypothesis registry** (`registry.py`, `adapter.promote_dsr`)
- Every battery run logs a FAMILY to `research/hypothesis_registry.jsonl`. The PROMOTE DSR bar rises
  with the cumulative count of distinct families: `promote_dsr = min(0.99, 1−(1−0.90)/√n_families)`
  (1→0.90, 9→0.967, 100→0.99). The within-family grid is already DSR-deflated (search_history.py);
  this corrects the ACROSS-family multiple testing. SCREEN stays 0.70.

**Rail 3 — Deployment-sanity** (`deployment.py`)
- Auto-FAILs a tier (regardless of DSR) if the strategy doesn't DEPLOY as designed: peak_concurrent
  ≥ max(3, 0.25×expected), n_trades ≥ 50, single_name_share ≤ 0.40, realized_vs_design ≥ 0.5.
- Caught the csm sector-tag bug (2 positions masquerading as a PROMOTE). `deployment_smoke(strategy,
  market)` is a fast pre-queue gate.

**Artifact schema** (`backtest/results/.../battery_*.json`):
`verdict` (final), `cross_oos.tier`/`tier_raw`, `deployment{passed,peak_concurrent,n_trades,...}`,
`holdout{passed,holdout_sharpe,...}`, `multiple_testing{n_families,promote_dsr_used}`,
`cross_oos.bundle{median_cpcv_sharpe,dsr,pbo,min_regime_sharpe,...}`, `time_split{in_sample,out_of_sample}`.

**Promotion path:** battery PROMOTE → holdout pass → #420 forward paper (`research/forward_evidence.py`)
→ human approval → staged candidate config. NEVER auto-promote. Live only at material AUM.

---

## Building a new strategy (sandbox)

1. File in `research/strategies/<name>.py` (sandbox; never `strategies/` directly). Must export a
   `BaseStrategy` subclass + module-level `PARAM_GRID`.
2. **MUST tag sectors** in signals: load `data/processed/sector_map_<market>.json` and set BOTH
   `Signal.sector` and `features["sector"]` (the engine reads `features["sector"]`). Omit → the
   `max_sector_concentration=2` cap collapses the book to ~2 "Unknown" positions → deployment-sanity
   FAIL. Templates: `cross_sectional_momentum.py` / `cross_sectional_factor.py` (cross-sectional
   factor books — the SHAPE that deploys on a broad universe; single-name technicals do NOT).
3. Validate: `deployment_smoke('<name>','shm')` then the battery command above.

**Strategies built this session:** `cross_sectional_momentum` (factor book, sector-fixed),
`cross_sectional_factor` (multi-factor zoo: mom/reversal/lowvol/52wk-high, grid-searches the blend),
`cross_sectional_lowvol_reversal` (Pass-3 clean recipe; FAILED holdout).

---

## Search orchestrator (multi-strategy sweep)

`scripts/run_search.py --market shm` — runs the rail-equipped battery over a list of price-based
strategies sequentially (nice'd, holdout-eval ON), logs `research/results/search_shm.log`, flags any
holdout-cleared PROMOTE to `research/results/search_shm_PROMOTES.txt`. Idempotent (skips done unless
`--force`). Run headless via systemd (battery runs are 20-90 min each).

---

## Sharadar survivorship-free data (refresh/extend)

Key: `~/.atlas-secrets.json` → `NASDAQ_DATA_LINK_API_KEY` (Sharadar SEP, $69/mo, download-and-cancel).
- **Download** (bulk, own-and-cancel): `python3 scripts/sharadar_download.py SEP TICKERS ACTIONS`
  → `data/sharadar/*.zip` (SEP ~1GB: 21,828 tickers, 15,556 delisted, 1998+; Web API export, no pkg).
- **Build a market**: `python3 scripts/ingest_sharadar_midsmall.py` (two-pass over SEP.zip → top-N
  liquid → `data/cache/shm/` + sector map from `TICKERS.sector`). Universe defs in `data/universes/`.
- `TICKERS` columns we use: `exchange, isdelisted, category, sector, scalemarketcap, first/lastpricedate`.
  Universe = Domestic Common Stock, major exchange (NASDAQ/NYSE/NYSEMKT), USD, mid/small band, INCLUDE
  delisted (survivorship), tag `currently_tradable` via `brokers.alpaca.tradable_assets.is_tradable`
  (live signals fire ONLY on currently-tradable; delisted are backtest-only).

To add a band/universe: clone `ingest_sharadar_midsmall.py` filters (e.g. `5 - Large`), write a new
`config/active/<market>.json` + `sector_map_<market>.json`.

---

## The 8-week trial (board decision 2026-06-06)

State: `atlas_state get --scope research-trial --key survivorship-search-2026-06` (+ pass1/2/3).
Memo: `ceo-board/memos/2026-06-06-survivorship-free-data-decision`. **Clock 2026-06-06 → 2026-08-01.**
- PASS = ≥1 strategy clears battery PROMOTE + write-once holdout + starts forward paper → triggers a
  capital-scaling decision.
- KILL = none by 2026-08-01 → cancel Sharadar, halt active research, declare "no edge at this scale".
- Status: pass1 (21 strats, 0 edge, only cross-sectional shape deploys); pass2 (factor zoo: momentum
  DEAD, low-vol+reversal alive in-search); pass3 (low-vol+reversal cleared DSR 0.986 but FAILED holdout).

---

## Golden rules (anti-patterns)

1. **Run on `shm` (survivorship-correct), not `sp500`.** sp500 results are biased mirages.
2. **A backtest tier is meaningless without the holdout + deployment-sanity.** Always `--holdout-eval`;
   never trust DSR/CPCV alone (DSR 0.986 still failed the holdout).
3. **Never re-peek the holdout** (single-use ledger). A burned config needs a genuinely NEW hypothesis.
4. **New strategies MUST tag `features["sector"]`** or they don't deploy.
5. **Pre-register before testing** (research/brain/hypotheses/); don't tune-to-rescue; accept the verdict.
6. **Never auto-promote to live config.** Human approval + forward paper + material AUM gate.
7. **Use systemd for long runs** (battery 20-90 min); never nohup/screen.
