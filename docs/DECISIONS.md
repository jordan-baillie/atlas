# Atlas — Decision Log
*Chronological record of significant architectural and operational decisions*

---

## 2026-02-18 — Config v9.1 → v9.2: Full SP500 Baseline Optimization

**Decision:** Run coordinate descent reoptimization after data-refresh degradation (CAGR -0.35%, Sharpe -0.30).

**Outcome:** v9.2: CAGR +11.21%, Sharpe +0.67, PF 1.30, MaxDD 7.76%. Accepted.

**Context:** The original scoring function allowed `inf` PF scores from 3-4 trade windows, producing degenerate low-trade solutions. Critical fix: min 15 trades, PF capped at 4.0.

**Config:** `config/versions/config_v9.1_pre_reoptimization.json` → v9.2 active.

---

## 2026-02-18 — Config v9.3 Rejected (Param Blending)

**Decision:** Reject v9.3 (50/50 blend of v9.2 and defaults).

**Rationale:** Identical perturbation stability as v9.2 (mean CAGR 2.67% vs 2.66%) but sacrificed 4.5% CAGR. The parameter landscape has a single ridge — blending moves toward a lower point without stability gain.

**Lesson:** Blending is not a robustness technique when the landscape has one ridge.

---

## 2026-02-19 — Config v9.4: Parallel Reopt with Robust Scoring

**Decision:** Re-optimize with parallel coordinate descent (4 workers) and new scoring function.

**Outcome:** CAGR 8.81%, Sharpe 0.42, 338 trades (was 133), 72.7% WF win rate.

**Config:** `config/versions/config_v9.3_robust.json` → archived; v9.4 used as basis.

---

## 2026-02-27 — Moomoo AU API Cannot Trade ASX Stocks

**Decision:** Abandon ASX live trading via Moomoo. Pivot to SP500 as primary live market.

**Finding:** `"Securities account X does not support trading AU.APX through API"` — server-side block. Moomoo AU (FUTUAU) can VIEW ASX positions but cannot PLACE ASX orders.

**Action:** SP500 becomes primary live market. ASX continues as backtest/research only until IBKR integration.

---

## 2026-02-27 — Paper Trading Layer Removed

**Decision:** Remove paper trading abstraction. Live broker becomes sole source of truth.

**Rationale:** With a live broker available, maintaining a parallel paper portfolio creates state divergence risk. All portfolio queries (`cmd_status`, `cmd_plan`, EOD settlement, dashboard) now read from `LivePortfolio` which connects to the broker.

**What stays:** `paper_engine/engine.py` for `TradePlanGenerator` and backtest infra. `backtest/engine.py` unaffected.

**Impact:** `paper_engine/state/live_{market}.json` becomes the equity history file. Old multi-position paper state files deprecated.

---

## 2026-02-27 — SP500 v2.0 Promoted (Initial US Optimization)

**Decision:** Optimize SP500 from scratch. Accept v2.0 results.

**Outcome:** CAGR 15.69% (+3.79pp vs baseline), Sharpe 1.040 (+0.250), MaxDD 5.39%, OOS Sharpe 1.225 (ratio 1.18 vs IS).

**Key finding:** RSI(14) outperformed RSI(2) on this data — contrary to Connors research. Z-score provides sufficient short-term signal; RSI(2) generated noisy signals.

**Config:** `config/versions/sp500_v2.0_optimized.json`.

---

## 2026-02-27 — Multi-Market Architecture + Timezone-Aware Scheduling

**Decision:** Make all scripts market-agnostic via `--market` flag. Derive cron schedules from `MarketProfile`.

**Changes:** `reoptimize_parallel.py`, `eod_settlement.py`, `intraday_monitor.py`, `pi-cron.sh` all market-aware. DST handled via IANA timezone library.

**Cron:** US Friday session falls on Saturday AEST — crons use `2-6` (Tue-Sat) for overnight US jobs, `1-5` for premarket.

---

## 2026-02-27 — SP500 First Live Orders (Moomoo US Market)

**Decision:** Begin live trading SP500. Initial allocation $4,000.

**First positions:** ON (3×@$68.16), CHTR (1×@$228.38) via trend_following.

**Infrastructure fixes discovered:** Moomoo ticker format (US.XXXX), price rounding, LIMIT orders work outside market hours, plan uses paper portfolio for sizing not full broker equity.

---

## 2026-02-27 — Continuous Research Pipeline Built

**Decision:** Implement autonomous wave-based research system with queue, journal, and Pi-agent-driven execution.

**Architecture:** `research/queue.json` → `research/experiments/` → `research/journal.json`. Append-only journal, file-locking, status machine (queued → claimed → running → passed/failed).

**Cron:** Mon-Fri 09:00 AEST (both markets closed in this window).

---

## 2026-02-28 — IBKR Broker Integration

**Decision:** Integrate IBKR as a second broker for ASX (and future markets).

**Implementation:** `brokers/ibkr/broker.py` using `ib_insync` via IB Gateway Docker (`ghcr.io/gnzsnz/ib-gateway:stable`), not IBeam REST (abandoned — session auth loop).

**Broker registry:** `brokers/registry.py` is sole source of truth — no hardcoded broker imports elsewhere. Switching broker = change `trading.broker` in config.

---

## 2026-02-28 — ASX Reopt Promoted to v9.3 (Wave 1 asx_reopt)

**Decision:** Promote ASX reoptimization candidate (new features: SMA-200 filter, IBS confirmation, configurable RSI period).

**Impact:** Sharpe 0.44→0.60, CAGR 9.1%→11.3%, DD 9.7%→7.1%, PF 1.25→1.37.

**Config:** `config/versions/asx_v9.3.json` → active.

---

## 2026-03-01 — SMA-200 Filter Promoted to SP500 v2.1

**Decision:** Promote `wave1_cross_mkt` (SMA-200 trend filter A/B test). Applied to all 3 active SP500 strategies.

**Impact:** Sharpe 0.59→0.87 (+47%), CAGR 10.1%→11.7%, DD 6.6%→5.3%, PF 1.38→1.66. Trades reduced 443→270 (quality over quantity).

**Mechanism:** Filtering entries below 200-day MA avoids buying into downtrends. Previous coord descent rejected it (reduced trades too aggressively), but clean A/B toggle confirmed the quality gain overwhelms quantity loss.

**Config:** `config/versions/sp500_v2.1.json`.

---

## 2026-03-01 — VIX Filter Permanently Rejected

**Decision:** Do not test VIX regime filters on this portfolio.

**Rationale:** Mean Reversion profits from HIGH-VIX panic entries — filtering those out destroys alpha. All 4 VIX thresholds (20/25/30/35) degraded Sharpe. VIX filter may work for trend-only portfolios.

**Rule:** If MR is in the portfolio, VIX filter is off the table.

---

## 2026-03-01 — IBKR REST API (IBeam) Abandoned

**Decision:** Use `ib_insync` + IB Gateway Docker instead of IBeam REST API.

**Rationale:** IBeam post-login session always returned `authenticated=False` (known IBeam bug — browser auth cookie not inherited by REST session). IB Gateway with IBC handles automated login reliably.

**Impact:** Requires `ib_insync` library. 2FA once per week. VPS must keep IB Gateway Docker running.

---

## 2026-03-02 — Wave 1 Dormant Strategy Theme CLOSED

**Decision:** Close Wave 1 dormant strategy activation theme. No dormant strategy added to live portfolio.

**Finding:** All 4 tested dormant strategies (momentum_breakout, short_term_mr, bb_squeeze, sector_rotation) are individually profitable after optimization but degrade the combined portfolio due to position contention at max_positions=10.

**Root cause:** High-signal-volume strategies (MB: 460 trades, STMR: 697 trades) flood the position pool, crowding out proven MR/TF/OG signals.

**Next step:** Position allocation pools (Task #52) built as the unlock mechanism. Enable when adding any new strategy.

---

## 2026-03-02 — SP500 v2.2: max_open_positions 10 → 15

**Decision:** Increase SP500 max_open_positions from 10 to 15 (MR+TF+OG only).

**Validation:** OOS Sharpe 0.962 vs IS 0.534 (+80%), 76% WF windows profitable, perturbation: 2/10 collapses.

**Expected improvement:** Sharpe 0.868 → 0.983 (+13%), CAGR 11.7% → 13.3%.

**Config:** `config/versions/sp500_v2.2.json`.

---

## 2026-03-02 — ASX IBKR Constraints: TF-only, Deferred Full Activation

**Decision:** Deploy only trend_following on ASX via IBKR at current equity ($3,999). Defer MR and OG.

**Finding:** IBKR $6/order + ASX $500 minimum parcel → $12 round-trip (2.4% drag). ASX combo backtest with IBKR fees: CAGR -3.70%, Sharpe -1.046. TF-only backtest: CAGR 8.46%, Sharpe 0.455.

**Threshold to revisit:** Account equity > $10,000, or re-optimize strategies specifically for $6 fee + $500 parcel regime.

**Config:** `config/versions/asx_ibkr_tf_only_v1.0.json`.

---

## 2026-03-02 — HK (SEHK) Market Added

**Decision:** Add HK as third market via IBKR. Paper mode pending full validation.

**Initial backtest:** 58 trades, Sharpe 0.82, WR 56.9%, PF 2.36, MaxDD 2.7%, CAGR 6.7%.

**Status:** live_enabled=false. OOS validation needed before going live.

**Config:** `config/active/hk.json`, IBKR client_id=12, port 4001.

---

## 2026-03-02 — Position Allocation Pool System (Task #52)

**Decision:** Implement config-driven per-strategy allocation pools. Disabled by default.

**Architecture:** `utils/allocation.py` — `StrategyAllocationPool` class. Hard pool (strict cap) and soft pool (cap + overflow) modes. Integrated into backtest engine, plan generator, live portfolio.

**Activation trigger:** When momentum_breakout is re-enabled and single strategy takes >60% of all trades.

**Config schema:**
```json
"allocation": { "enabled": false, "mode": "hard_pool", "pools": { "trend_following": {"max_positions": 5}, ... } }
```

---

## 2026-03-02 — Full Codebase Audit + Critical Bug Fixes

**Decision:** Run comprehensive audit and fix all CRITICAL issues via swarm.

**Fixes applied:**
- C1: Look-ahead bias (trailing stop/max_loss_cap now use T-1 close)
- C3: `LivePortfolio.update_positions()` added
- C4: `get_today_deals()` added to BrokerAdapter + IBKRBroker
- C5: IBKR account ID moved from config to secrets
- H1: Sector concentration enforced in backtest engine
- H3: MTF trailing stop tracks highest_high since entry (was always False)
- H4: PaperPortfolio commission model matches backtest (flat_fee_threshold added)
- H9: Stop-loss exits use MARKET orders
- H10: Moomoo unlock failure now fatal for live accounts
- H8/M15: Atomic writes for parquet cache and paper state files

**Open audit items (deferred):** H2 (WF indexing), M1 (strategy registry), M3 (config validation), M6/M14 (minor metrics), M9 (CSRF), L3-L10 (low priority).
