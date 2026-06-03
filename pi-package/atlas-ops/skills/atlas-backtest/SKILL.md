---
name: atlas-backtest
description: "Run, interpret, and record Atlas backtests. Covers CLI and atlas_jobs_run workflows, result interpretation, comparison with baselines, brain recording, and the full OOS validation pipeline. Use when asked to backtest, evaluate strategies, compare results, or run validation."
type: reference
---

# Atlas Backtesting

Strategy testing, result interpretation, and knowledge recording workflows.

---

## Pre-Flight Checklist

Before running ANY backtest:

1. **Check brain for prior results** — avoid re-testing what's already known
2. **Verify data freshness** — stale cache produces misleading results
3. **Confirm config version** — know what you're testing against

```bash
# 1. Prior results
ls research/results/ | grep -i "<strategy_or_topic>"

# 2. Data freshness
ls -lt data/cache/sp500/ | head -3
# If >24h old:
cd /root/atlas && python3 scripts/cli.py -m sp500 ingest

# 3. Config version
python3 -c "import json; print(json.load(open('config/active/sp500.json'))['version'])"
```

---

## Running Backtests

### Method 1: CLI (quick, synchronous)

```bash
cd /root/atlas && python3 scripts/cli.py -m sp500 backtest
# NOTE: the backtest subcommand has no --days/--date flags. The walk-forward
# window is config-driven (config/active/<market>.json -> backtest.*). Use
# Method 3 (ResearchSession) below when you need to vary the window per run.
```

### Method 2: atlas_jobs_run (async, tracked)

```
Tool: atlas_jobs_run
Params: {
  "job": "cli_backtest",
  "args": { "market": "sp500" }
}
```

Then poll for completion:
```
Tool: atlas_jobs_get
Params: { "runId": "<run_id_from_above>", "includeStdoutTail": true }
```

### Method 3: ResearchSession (interactive experimentation)

```python
import sys; sys.path.insert(0, '/root/atlas')
from research.loop import ResearchSession

s = ResearchSession('mean_reversion', 'sp500')

# Establish baseline
baseline = s.baseline()
print(f"Baseline: Sharpe={baseline['sharpe']:.3f}, CAGR={baseline['cagr_pct']:.1f}%")

# Try a parameter change
result = s.experiment({'rsi_period': 7}, 'shorter RSI window')
print(f"Experiment: Sharpe={result['sharpe']:.3f}")

# Keep or discard
if result['sharpe'] > baseline['sharpe']:
    s.keep()    # advances best-known params
else:
    s.discard() # reverts to previous best

# View history
print(s.history())
```

### Method 4: Quick Screen (<10 seconds)

```python
import sys; sys.path.insert(0, '/root/atlas')
from research.quick_screen import screen_strategy
from utils.config import get_active_config

config = get_active_config('sp500')
result = screen_strategy('mean_reversion', config, market='sp500')
print(result)
# ScreenResult(alive=True, signal_count=142, sharpe=0.38, ...)
```

Use this to quickly check if a strategy idea generates any signals at all before committing to a full backtest.

---

## Interpreting Results

### Key Metrics

| Metric | Good | Acceptable | Poor | Notes |
|--------|------|------------|------|-------|
| Sharpe | > 1.0 | 0.5–1.0 | < 0.5 | Solo metrics unreliable at $4K equity (#30) |
| CAGR % | > 15% | 5–15% | < 5% | Compare to SPY baseline |
| Max Drawdown % | < 10% | 10–20% | > 20% | Critical for live trading |
| Profit Factor | > 2.0 | 1.5–2.0 | < 1.5 | Cap at 4.0 for scoring (#2) |
| Trade Count | > 50 | 20–50 | < 20 | Min 15 for statistical significance |
| Win Rate % | > 50% | 40–50% | < 40% | MR strategies can be profitable at 40% |

### Critical Interpretation Rules

1. **Solo vs Combined**: Solo backtest metrics at $4K equity are unreliable due to fee drag. Always run combined portfolio test for promotion decisions. Solo is only useful for relative rankings.

2. **Sharpe inflation**: If Sharpe > 3.0 on a single-strategy test, suspect degenerate optimization (lesson #2). Check trade count — if < 15, the result is meaningless.

3. **OOS degradation**: Compare in-sample vs out-of-sample. CAGR degradation > 50% is a red flag. Sharpe degradation > 40% suggests overfitting.

4. **Walk-forward consistency**: Window win rate should be > 50%. If most windows are profitable but one catastrophic window dominates, the strategy is fragile.

---

## Summarizing Artifacts

After a backtest completes, summarize the result file:

```
Tool: atlas_artifacts_summarize
Params: { "path": "backtest/results/<result_file>.json" }
```

For OOS validation results:
```
Tool: atlas_artifacts_summarize
Params: { "path": "backtest/results/<oos_file>.json", "kind": "validate_oos" }
```

### Comparing Two Results

```
Tool: atlas_artifacts_compare
Params: {
  "leftPath": "backtest/results/<baseline>.json",
  "rightPath": "backtest/results/<candidate>.json"
}
```

---

## OOS Validation Pipeline (Cross-OOS battery)

Required before any config promotion (lesson #6). As of 2026-06, `scripts/validate_oos.py`
is a **strategy-agnostic cross-OOS battery** (ported from Midas #102) that REPLACES the old
hand-rolled 3-test heuristic. The authoritative verdict is a declarative hard-gate table
(`research/cross_oos/adapter.py::ATLAS_DEFAULT_GATES`) where a **missing measurement == FAIL**
(you cannot pass a battery you didn't run). It reuses the existing BacktestEngine output — it
does not replace the backtester.

`make_strategies()` is now driven by `scripts.strategy_evaluator.STRATEGY_REGISTRY`, so the
battery validates **any enabled strategy** in the config (momentum_breakout, mtf_momentum,
connors_rsi2, sector_rotation, short_term_mr, …), not just the four originally hardcoded.

### Running OOS Validation

```
Tool: atlas_jobs_run
Params: {
  "job": "validate_oos",
  "args": {
    "configPath": "config/candidates/<candidate>.json",
    "outputPath": "backtest/results/oos_<label>.json"
  }
}
```
CLI: `python3 scripts/validate_oos.py --config-path <cfg> --output-path <out> --grid-size 10`

### The battery (4 axes + multiple-testing control)

| Axis / control | What | Default gate |
|---|---|---|
| **CPCV (cross-TIME)** | Combinatorial Purged CV (purge+embargo) over the daily return series → distribution of OOS Sharpes | median ann. Sharpe ≥ 0.5; ≥ 55% of paths net-positive |
| **PBO (cross-CONFIG)** | Probability of Backtest Overfitting via CSCV over a grid of `--grid-size` perturbed configs | PBO ≤ 0.50 |
| **DSR** | Deflated Sharpe vs the best-of-N noise bar (N = grid size) | DSR ≥ 0.90 |
| **Leave-one-ticker-group-out (cross-TICKER)** | Drop each of 5 random ticker groups; remaining book must stay positive + concentration | top ticker < 50% of net; LOO net Sharpe > 0 on all holdouts |
| **Regime stratification (cross-REGIME)** | Stratify trade PnL by each trade's `entry_regime` | min regime Sharpe ≥ −0.5; ≤ 60% of PnL from one regime |
| **Forward holdout** | IS/OOS time split (last 20%) | OOS net PnL > 0; CAGR degradation ≤ 50% |

> Equities note: the crypto-specific 10 bps/side cost-stress gate is dropped — Atlas backtests
> already run net of realistic commissions, so the whole battery runs on net returns.

### Interpreting results

```python
import json
v = json.load(open('backtest/results/oos_<label>.json'))

# Authoritative section
co = v['cross_oos']
print('verdict:', co['verdict'])                 # 'PASS' | 'FAIL'
print('gates:', {k: g['status'] for k, g in co['gate_checks'].items()})
b = co['bundle']
print('CPCV median Sharpe:', b['median_cpcv_sharpe'], '| PBO:', b['pbo'], '| DSR:', b['dsr'])
print('top_ticker_frac:', b['top_group_frac'], '| min_regime_sharpe:', b['min_regime_sharpe'])

# summary.overall_verdict == 'PASS' iff the battery passes.
print(v['summary']['overall_verdict'])
```

**Back-compat:** the legacy keys `test1_time_period_split`, `test2_perturbation` (now
`robust` is projected from PBO+DSR), `test3_walkforward_consistency`, and
`summary.overall_verdict` are still emitted so `research/promoter.py`,
`scripts/auto_reoptimize.py`, and `atlas_risk_check_reopt_promotion` keep working.
Use `atlas_risk_check_reopt_promotion` for automated gate checking.

---

## Reoptimization Workflow

Full universe re-optimization with coordinate descent:

```
Tool: atlas_jobs_run
Params: { "job": "reoptimize_full_universe" }
```

This produces:
- `backtest/results/reoptimization_full_universe.json` — scores and params
- `config/config_candidate_reoptimized_*.json` — staged candidate config

Then validate the candidate:
```
Tool: atlas_jobs_run
Params: {
  "job": "validate_oos",
  "args": { "configPath": "config/config_candidate_reoptimized_<ts>.json" }
}
```

Then check promotion gate:
```
Tool: atlas_risk_check_reopt_promotion
Params: {
  "candidatePath": "config/config_candidate_reoptimized_<ts>.json",
  "validationPath": "backtest/results/<oos_result>.json"
}
```

---

## Research Results Format

### TSV Results (research/results/)

Each strategy gets a `.tsv` file with experiment history:

```tsv
timestamp	sharpe	trades	max_dd_pct	pf	cagr_pct	params_changed	status	description
2026-03-10T15:41:29	0.2975	214	5.22	2.6966	9.71		keep	baseline
2026-03-10T15:42:34	0.2975	214	5.22	2.6966	9.71	breakout_period=10	discard	breakout_period: None→10
```

Columns: timestamp, sharpe, trades, max_dd_pct, pf (profit factor), cagr_pct, params_changed, status (keep/discard), description.

### Recording to Brain

After significant findings, record to brain:

```bash
# Append to memory/SUMMARY.md with the finding
# Format: what was tested, what was found, what it means for the system
```

---

## Common Pitfalls

| Pitfall | Prevention |
|---------|-----------|
| Re-running an experiment that's already in brain/ | Check `research/results/` and `memory/SUMMARY.md` first |
| Testing on stale data | Always check cache mtime before running |
| Promoting based on solo backtest | Run combined portfolio test (#7) |
| Optimizing to degenerate solution | Check trade count > 15, cap PF at 4.0 (#2) |
| Ignoring OOS degradation | CAGR drop > 50% = reject (#6) |
| VIX filtering MR portfolio | Destroys alpha (#5) |
| Trusting high Sharpe on few trades | Sharpe > 3.0 with < 20 trades = degenerate |
