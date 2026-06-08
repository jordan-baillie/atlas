# Research Integrity Rails — Spec (3 components)

> Mandated by board memo `ceo-board/memos/2026-06-05-atlas-research-strategy-free-compute` (5-0).
> Context: research compute is FREE/unlimited, so the binding constraint is **false discovery**, not
> bandwidth. Free unlimited search against a fixed gate (DSR≥0.90) => P(a strategy passes by chance)→1.
> These three rails make unlimited search safe. They are BLOCKING prerequisites for re-activating the
> automated research loop at scale. Build order = board order: **Rail 3 → Rail 1 → Rail 2** (cheapest /
> highest-proven first; Rail 3 just reversed a false csm PROMOTE this session).
>
> All three plug into existing code: `scripts/run_strategy_battery.py`, `research/cross_oos/adapter.py`
> (`SCREEN_DSR=0.70`, `PROMOTE_DSR=0.90`, `assemble_bundle(search_burden=...)`),
> `research/cross_oos/search_history.py` (`search_burden()`), and `scripts/validate_oos.compute_split_dates`.

---

## Rail 3 — Deployment-sanity assertions  ✅ IMPLEMENTED 2026-06-05

> Shipped: `research/cross_oos/deployment.py` (`deployment_sanity`), wired into
> `scripts/run_strategy_battery.py` (auto-FAILs the tier; writes a `deployment` block + `tier_raw`
> to the artifact), tests `research/cross_oos/tests/test_deployment.py` (6/6 pass; 53 existing cross_oos
> tests still green). Verified end-to-end: fixed csm -> peak_concurrent 14 / 11 sectors / PASS;
> the pre-fix 2-position artifact auto-FAILs (`test_two_position_artifact_fails`). Build order note
> below was the plan; Rail 3 is now done, Rails 1 & 2 remain.


**Why:** a battery tier is meaningless unless the strategy actually trades the book it was designed to.
This session csm "PROMOTEd" at DSR 0.926 while a sector-tag bug capped it to **2 concurrent positions**;
properly deployed (~14) it FAILED. A human caught it; at 1,000 runs/day no human will. (See
`tasks/lessons.md` 2026-06-05.)

**Gap today:** the battery records `total_trades` but never checks peak concurrency, deploy breadth, or
that the realized book matches design. Artifacts pass silently.

**Design:** a pure function `deployment_sanity(trades, primary_config, strategy_meta) -> dict` in a new
`research/cross_oos/deployment.py`, called in `run_strategy_battery.py` right after the primary backtest,
BEFORE `evaluate_tiers` is trusted. Computes from `prim["trades"]`:
- `peak_concurrent` and `avg_concurrent` (event-sweep over entry/exit dates — same method used in the
  2026-06-05 diagnosis).
- `n_trades`, `trades_per_year`.
- `sector_spread` = # distinct sectors held; `max_sector_share` = max fraction of position-days in one sector.
- `single_name_share` = max fraction of position-days in one ticker.
- `realized_vs_design` = peak_concurrent / expected_positions, where expected = `min(top_n or max_positions,
  sector_cap × n_sectors)`.

**Auto-FAIL gates (any => tier forced to FAIL regardless of DSR), thresholds pre-registered here:**
| Check | Fail when | Rationale |
|---|---|---|
| `peak_concurrent` | `< max(3, 0.25 × expected_positions)` | book is not deploying as designed (the csm trap) |
| `n_trades` | `< 50` over the full window | too few trades → degenerate / luck |
| `single_name_share` | `> 0.40` of position-days | accidental single-name bet masquerading as a book |
| `realized_vs_design` | `< 0.5` | engine constraints silently throttling the strategy |

**Integration:** add `out["deployment"]=deployment_sanity(...)`; if it fails, set `out["verdict"]="FAIL"`,
`out["cross_oos"]["tier"]="FAIL"`, and record `out["deployment"]["forced_fail_reasons"]`. Print a loud
banner. The thresholds live in `deployment.py` as named constants (frozen, pre-registered).

**Acceptance:** (a) re-running the buggy pre-fix csm auto-FAILs on `peak_concurrent`/`realized_vs_design`;
(b) the fixed ~14-name csm passes deployment-sanity (then FAILs on DSR, correctly, for a different reason);
(c) a healthy multi-name strategy passes. Add a unit test with synthetic trade lists for each case.

---

## Rail 1 — Write-once holdout partition  ✅ IMPLEMENTED 2026-06-05

> Shipped: `config/holdout.json` (pinned `holdout_start=2025-01-01`, ~17mo, locked);
> `research/cross_oos/holdout.py` (`evaluate_holdout` runs the frozen primary on the holdout ONCE,
> `holdout_gate` pure logic, single-use append-only ledger `research/holdout_ledger.jsonl`);
> `scripts/run_strategy_battery.py` quarantines search to data < holdout_start by default
> (`--no-holdout-quarantine` to override) and, with `--holdout-eval`, downgrades a PROMOTE that
> fails the holdout to FAIL. Tests `research/cross_oos/tests/test_holdout.py` (7/7; 60 cross_oos
> tests green). Verified: search max date 2024-12-31 (holdout quarantined); holdout eval runs +
> single-use ledger blocks re-peek.


**Why:** today every config is scored on an OOS window recomputed from the SAME data each run
(`compute_split_dates` = last 20%). Over thousands of free tests, the search implicitly optimizes against
that window — it is not a true holdout. A genuine holdout is the single strongest defense against
overfitting at scale.

**Design:**
1. **Pin the boundary once, persist it.** New file `config/holdout.json`:
   `{ "holdout_start": "YYYY-MM-DD", "created_at": ..., "rationale": "...", "locked": true }`.
   Boundary = a fixed recent window (board: ~12–18 months). Written ONCE; never auto-recomputed.
2. **Quarantine during search.** In `run_strategy_battery.py`, after `vo.load_data`, slice
   `data_search = {k: v[v.index < holdout_start]}` and run the ENTIRE sweep + CPCV/PBO/DSR + the existing
   IS/OOS split on `data_search` only. The loop physically cannot read holdout rows during search.
3. **Touch the holdout ONCE, at promotion.** New `research/cross_oos/holdout.py::evaluate_holdout(strategy,
   primary_config, market) -> dict` runs the frozen primary config on `data[v.index >= holdout_start]` and
   returns `{holdout_sharpe, holdout_cagr, holdout_pf, holdout_trades, degradation_vs_search_pct}`. Called
   only when a candidate reaches PROMOTE (not on every SCREEN/FAIL — keeps it un-peeked).
4. **Holdout gate.** PROMOTE is only confirmed if the holdout result clears a pre-registered bar
   (`holdout_sharpe > 0` AND `degradation_vs_search_pct > −50%` AND passes deployment-sanity on the holdout).
   A PROMOTE that fails the holdout is downgraded to FAIL and the candidate is burned (cannot be re-promoted
   without a fresh holdout window — prevents holdout-peeking by retry).
5. **Single-use accounting.** `holdout.py` logs every holdout evaluation (strategy, config hash, timestamp)
   to `research/holdout_ledger.jsonl`. A config hash may be holdout-evaluated **once**; repeats are rejected
   (you cannot iterate against the holdout).

**Integration:** `run_strategy_battery.py` gains `--holdout-eval` (off by default; the automated loop calls
it only on PROMOTE candidates). Default battery runs are search-only and never see the holdout.

**Acceptance:** (a) a normal battery run reads zero holdout rows (assert max date < holdout_start in the
search slice); (b) `evaluate_holdout` refuses a second eval of the same config hash; (c) a strategy that
PROMOTEs in-search but degrades on the holdout is downgraded to FAIL with the holdout numbers recorded.

---

## Rail 2 — Hypothesis registry + search-count-aware promotion bar  ✅ IMPLEMENTED 2026-06-05

> Shipped: `research/cross_oos/registry.py` (append-only `hypothesis_registry.jsonl`,
> `distinct_families`, `family_of`); `research/cross_oos/adapter.promote_dsr(n_families)` =
> `min(0.99, 1-(1-0.90)/sqrt(n))` (1->0.90, 4->0.95, 9->0.967, 100->0.99); wired into
> `run_strategy_battery.py` — the FDR-aware bar is passed to `evaluate_tiers` and every run is logged
> to the registry. Decision (avoids double-counting): the within-family config search stays handled
> by the effective-N DSR (`search_history.py`); the ACROSS-family burden is corrected at the
> promotion BAR only. Tests `research/cross_oos/tests/test_registry.py` (5/5; 65 cross_oos tests
> green). Verified: registry logged a run, artifact carries a `multiple_testing` block, base bar 0.90
> at n_families=1 (regression-safe). ALL THREE RAILS NOW IMPLEMENTED.


**Why:** the battery's effective-N DSR already deflates by the **grid** (`search_burden.n_trials` ≈ 12 per
run) — but NOT across the **thousands of distinct strategies/hypotheses** an industrialized loop runs. Test
enough distinct ideas against a fixed DSR=0.90 and one passes by chance. The promotion bar must scale with
the *cumulative* count of distinct hypotheses ever tested.

**Design:**
1. **Persistent registry.** New `research/hypothesis_registry.jsonl` (append-only). Every battery run logs
   one record: `{ts, strategy, config_hash, family, market, grid_size, tier, dsr, holdout_touched}`.
   `family` = a coarse hypothesis class (e.g. "cross_sectional_momentum", "pairs", "news_sentiment") so the
   correction counts independent IDEAS, not just configs. Extend the existing
   `research/cross_oos/search_history.py` (which already has `search_burden()`) to read this registry.
2. **Cumulative burden into the DSR.** Today `run_strategy_battery.py` builds a local `burden` with
   `n_trials = grid_size`. Add `n_families_tested = registry.distinct_families()` and feed an effective
   cumulative trial count into `search_burden` so `assemble_bundle`'s effective-N DSR deflates by the global
   multiple-testing context, not just this run's grid. (The DSR machinery in `adapter.py` already consumes
   `search_burden.n_trials` + `sr_variance` — extend the count, no new math.)
3. **FDR-aware promotion bar.** Replace the constant `PROMOTE_DSR=0.90` with a function
   `promote_dsr(n_families)` in `adapter.py`: e.g. a Benjamini-Hochberg / Šidák-style raise —
   `promote_dsr = 1 − (1 − 0.90) / sqrt(max(1, n_families))` (capped, e.g., ≤ 0.99). 1 family → 0.90;
   100 families → ~0.99. Pre-register the exact form here before turning on the loop. SCREEN_DSR stays 0.70
   (screening is allowed to be permissive; PROMOTION is what must be FDR-controlled).
4. **Forward confirmation as the non-retrofittable backstop.** Even an FDR-corrected PROMOTE must clear the
   #420 forward live-paper clock before any live weight — registry counting can't fully model adaptive
   search, so forward (truly unseen) evidence is the final gate. (This rail records the requirement; #420 is
   the mechanism.)

**Integration:** `run_strategy_battery.py` appends to the registry every run and reads
`n_families` to set the effective burden + promote bar via the new `promote_dsr(n_families)`.
`evaluate_tiers(bundle, promote_dsr=promote_dsr(n_families))`.

**Acceptance:** (a) with 1 family tested, the bar is 0.90 (today's behavior — regression-safe); (b) after
N synthetic families, the bar rises monotonically toward the cap; (c) a config that PROMOTEd at 0.90 with 1
family no longer PROMOTEs once the registry shows it was 1-of-many near-identical search attempts; (d) the
registry survives restarts (append-only file).

---

## How the three compose (the promotion pipeline after the rails)

```
battery run (search slice only, holdout quarantined)         [Rail 1 quarantine]
  -> deployment_sanity()  --fail--> FAIL                       [Rail 3]
  -> cross-OOS battery, DSR deflated by cumulative families    [Rail 2 burden]
  -> evaluate_tiers(promote_dsr = f(n_families))               [Rail 2 bar]
       -> FAIL/SCREEN: log to registry, done
       -> PROMOTE candidate:
            -> evaluate_holdout() ONCE  --fail--> FAIL (burned) [Rail 1 holdout]
            -> deployment_sanity(on holdout) --fail--> FAIL     [Rail 3 again]
            -> #420 forward live-paper clock (human-gated)      [Rail 2 backstop]
            -> human approval -> staged candidate config
  -> append hypothesis_registry.jsonl                          [Rail 2 registry]
```

**Net:** generation + screening run unlimited and free; PROMOTION is gated by an un-peeked holdout, an
FDR-aware bar that scales with how much we've searched, deployment-sanity, forward confirmation, and human
approval. Unlimited free search becomes a structural advantage instead of an overfitting generator.

## Non-goals / guards
- Do NOT relax SCREEN to compensate for a stricter PROMOTE (screening stays permissive on purpose).
- Do NOT auto-promote anything (human gate stays — free compute ≠ free attention/capital).
- Holdout window is pinned and rare-rotated (only when materially more data exists, logged); never rotated
  to rescue a candidate.
- Registry counts FAMILIES (ideas), not just configs, to avoid both under- and over-counting the burden.
