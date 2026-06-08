# Gate-1b Pre-Registration — Combined Value+Momentum+Quality Factor on `shm`

**Status:** PRE-REGISTERED (locked 2026-06-08, BEFORE the rails battery is run)
**Predecessors:** csm momentum (FAIL, real-but-weak), value+quality (FAIL, real-but-weak, clean 11-sector deployment).
**This is the disciplined "use the learnings" swing — ONE new family, tested ONCE. NOT a scour.**

## Hypothesis
The two real-but-individually-weak signals we have now measured on `shm` — price **momentum** and fundamental **value(+quality)** — are the canonical COMPLEMENTARY pair (Asness-Moskowitz-Pedersen 2013). Combining orthogonal weak factors is the textbook robustness move. If a deployable edge exists at this scale, the diversified composite is its most plausible form.

**Orthogonality verified on the search window** (2017-2024, 91 monthly snapshots, holdout untouched): corr(mom, value) = **-0.195**, corr(mom, quality) = +0.02, corr(value, quality) = -0.094. Genuinely orthogonal → combination is justified, not redundant.

## Construction (FROZEN, no tuning)
Cross-sectionally each rebalance, winsorize(1/99)+z-score each leg:
- **value_z** = mean z of [1/pe, 1/pb, fcf/marketcap]  (SF1 ARQ, point-in-time, datekey+1 lag)
- **quality_z** = mean z of [roe, roa, grossmargin, -de]
- **momentum_z** = z of (close[t-21]/close[t-126] - 1)   (12-1 month, csm horizon)
- **fund_z** = mean(value_z, quality_z);  **combined** = w_mom·momentum_z + (1-w_mom)·fund_z
- Default **w_mom = 0.34** → ≈ equal thirds (mom 0.34, value 0.33, quality 0.33). No weight search beyond the pre-declared grid.
- Long **top quintile**, monthly rebalance, sector-tagged, long-only.
- **Sizing/stops:** Atlas house model (ATR stop mult 3.0, risk 0.5%/trade) — identical to csm + value_quality. Primary exit = monthly rank; ATR stop = backstop.

## Validation
```
python3 scripts/run_strategy_battery.py --strategy cross_sectional_value_momentum --market shm \
  --grid-size 12 --max-positions 35 --select default --holdout-eval \
  --output-path backtest/results/search/battery_cross_sectional_value_momentum_shm.json
```
All 3 rails (holdout quarantine + FDR-aware promote bar + deployment-sanity). Grid = {w_mom, top_pct, atr_stop_mult}.

## PASS / KILL (FROZEN)
**PASS** (→ stage paper candidate, forward-track, NO live money) requires ALL:
- TIER = **PROMOTE** (clears FDR bar, now ≈0.98 at n_families≈24), AND
- Write-once **HOLDOUT = PASS**, AND median_cpcv ≥ 0.5, AND DSR ≥ 0.90, AND
- No IS→OOS Sharpe sign-flip, AND deployment-sanity PASS (peak ≥5, ≥8 sectors).

**KILL** (→ CLOSE the Atlas equity edge-search; the combine-the-learnings swing exhausted) if ANY fails.

## VERDICT 2026-06-08: TIER = FAIL → KILL (equity edge-search CLOSED)
Primary (default w_mom 0.34): CPCV median **0.347** (the BEST primary of any of the 24 families — diversification helped, exactly as theory predicts — but still < 0.5), DSR eff-N **0.635** (< 0.90), PBO **0.656** (high → overfit-prone), frac+ 0.80. Best grid cfg7 (w_mom 0.34, top_pct 0.2) cpcv 0.315. Holdout NOT burned (failed in-search tier). Artifact: `backtest/results/search/battery_cross_sectional_value_momentum_shm.json`.

**The diversified composite of all our real-but-weak orthogonal signals (value+quality+momentum) is the strongest result obtained — and it still fails the bar.** This is the cleanest possible confirmation of the failure diagnostic (`research/brain/hypotheses/edge_bound_diagnostic_2026-06-08.md`): we are EDGE-bound, not gate-bound, across every signal axis AND their best combination. Atlas equity edge-search is honestly exhausted at this scale. Attention to Hermes.

**Honest prior:** LOW. After 24 nulls the FDR bar is ~0.98; combining two weak signals improves robustness but rarely manufactures a strong edge where the components were weak. This run's value is decisive closure either way — it is the LAST principled equity probe, not the start of a scour. If it fails, the disciplined conclusion is "no deployable equity edge at this scale," and attention stays on Hermes.
