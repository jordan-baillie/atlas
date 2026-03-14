---
name: atlas-brain
description: "Navigate and query the Atlas research knowledge base. Search prior experiment results, check closed decisions, review confirmed patterns, and record new findings. Use when asked about prior research, checking if something was already tested, querying strategy performance history, or recording experiment outcomes."
---

# Atlas Brain — Research Knowledge Base

Navigate, query, and contribute to Atlas institutional memory.

---

## Knowledge Locations

| Source | Path | Format | Contains |
|--------|------|--------|----------|
| **Session Memory** | `memory/SUMMARY.md` | Markdown | System state, architecture decisions, known issues, critical procedures |
| **Experiment Results** | `research/results/*.tsv` | TSV | Per-strategy experiment history (sharpe, trades, params, keep/discard) |
| **Backtest Artifacts** | `backtest/results/*.json` | JSON | Full backtest outputs, OOS validations, reoptimization results |
| **Operational Lessons** | `tasks/lessons.md` | Markdown | 35+ lessons organized by domain |
| **Config History** | `config/versions/*.json` | JSON | Pre-promotion config snapshots |
| **Promotion Log** | `config/promotion_log.json` | JSON | Config promotion audit trail |
| **Research Queue** | `research/queue/` | JSON | Pending experiment definitions |
| **Equity Curves** | `logs/equity_curve_*.json` | JSON | Daily equity and PnL tracking |
| **Trade Plans** | `plans/plan_*.json` | JSON | Generated trade plans with signals, entries, exits |

---

## Querying Prior Results

### "Has this been tested before?"

```bash
# Check experiment results for a strategy
ls research/results/ | grep -i "<strategy_name>"
cat research/results/<strategy_name>.tsv | head -20

# Check backtest artifacts
ls backtest/results/ | grep -i "<topic>"

# Search memory
grep -i "<topic>" memory/SUMMARY.md

# Search lessons
grep -i "<topic>" tasks/lessons.md
```

### "What were the best results for strategy X?"

```bash
# Read TSV — look at 'keep' rows (best-so-far snapshots)
cd /root/atlas
python3 -c "
import csv
with open('research/results/<strategy>.tsv') as f:
    reader = csv.DictReader(f, delimiter='\t')
    keeps = [r for r in reader if r.get('status') == 'keep']
    if keeps:
        best = keeps[-1]  # latest keep is current best
        print(f'Best Sharpe: {best[\"sharpe\"]}')
        print(f'Trades: {best[\"trades\"]}')
        print(f'CAGR: {best[\"cagr_pct\"]}%')
        print(f'Params: {best.get(\"params_changed\", \"baseline\")}')
    else:
        print('No keep results — only baseline exists')
"
```

### "What strategies have been researched?"

```bash
# List all research results
ls research/results/*.tsv | sed 's|.*/||;s|\.tsv||' | sort

# Current list (30 strategies tested):
# adx_trend_pullback, bb_squeeze, connors_rsi2, consecutive_down_days,
# demark_sequential, dividend_capture, donchian_breakout, gap_and_go,
# heikin_ashi_reversal, inside_bar_nr7, keltner_reversion,
# lower_band_reversion, macd_divergence, mean_reversion,
# momentum_breakout, monthly_rotation, opening_gap, overnight_return,
# pead_earnings_drift, put_call_vix_proxy, relative_strength_pullback,
# rsi_divergence, short_term_mr, stochastic_oversold, trend_following,
# triple_rsi, volume_climax, williams_percent_r
```

### "What did the last OOS validation show?"

```bash
# Find recent OOS validation files
ls -lt backtest/results/oos_*.json | head -5

# Summarize via tool
# Tool: atlas_artifacts_summarize
# Params: { "path": "backtest/results/<oos_file>.json", "kind": "validate_oos" }
```

---

## Strategy Performance Summary

### How to build a current performance table

```python
import sys; sys.path.insert(0, '/root/atlas')
import csv
from pathlib import Path

results_dir = Path('research/results')
for tsv in sorted(results_dir.glob('*.tsv')):
    with open(tsv) as f:
        reader = csv.DictReader(f, delimiter='\t')
        rows = list(reader)
    keeps = [r for r in rows if r.get('status') == 'keep']
    latest = keeps[-1] if keeps else rows[-1] if rows else None
    if latest:
        name = tsv.stem
        sharpe = float(latest.get('sharpe', 0))
        trades = int(latest.get('trades', 0))
        cagr = float(latest.get('cagr_pct', 0))
        pf = float(latest.get('pf', 0))
        print(f"{name:30s}  Sharpe={sharpe:6.3f}  Trades={trades:4d}  CAGR={cagr:6.1f}%  PF={pf:5.2f}")
```

### Currently enabled strategies (SP500 v3.0)

| Strategy | Role | Notes |
|----------|------|-------|
| momentum_breakout | Trend-following breakout | High signal volume (460 trades), position contention risk |
| mean_reversion | Counter-trend | Profits from panic/high-VIX, core alpha source |
| trend_following | Trend continuation | SMA-200 filtered, Sharpe +47% with filter |
| opening_gap | Gap exploitation | 0.94 correlated with MR — same cluster |
| sector_rotation | Rotation | Needs rebalance-aware engine, 0 solo trades currently |
| short_term_mr | Fast mean-reversion | Highest signal volume (697 trades), contention risk |
| connors_rsi2 | RSI-2 counter-trend | 0.95 correlated with MR — same cluster |

---

## Closed Decisions

These have been decided — don't re-open without strong new evidence.

| Decision | Verdict | Evidence |
|----------|---------|----------|
| VIX regime filter | **Rejected** — destroys MR alpha | MR Sharpe drops with VIX filter; MR profits from panic |
| Config blending | **Rejected** — pick best, don't average | 50/50 blend = 4.5% less CAGR, zero robustness gain |
| Solo vs combined sweeps | **Combined only** for promotion | Solo metrics unreliable at $4K equity |
| Moomoo vs Alpaca (US) | **Alpaca** — commission-free | Moomoo has ASX order block, $6/trade US |
| max_positions 10 vs 15 | **15** — +13% Sharpe | Control test outperformed all dormant strategy additions |
| Sector rotation | **Deferred** | Needs rebalance-aware engine |
| Paper trading layer | **Removed** — broker is source of truth | LivePortfolio reads broker directly |
| SMA-200 filter | **Adopted** for all SP500 strategies | Clean A/B: Sharpe +47%, DD -1.2pp |

---

## Confirmed Patterns

Tested and verified — use these as priors for future experiments:

| Pattern | Strength | Implication |
|---------|----------|-------------|
| MR profits from panic (high VIX) | Strong (multiple tests) | Never filter VIX for MR-containing portfolios |
| Position contention > strategy quality | Strong (4/4 dormant fail combined) | Test combined before promoting any strategy |
| Volume filter 1.5x threshold | Moderate (single sweep) | Use 1.5x as default; below = minimal, above = too few trades |
| Correlation clusters (MR/connors/OG) | Strong (0.94-0.95) | These are one bet — allocate as cluster |
| Control arms win | Strong (max_pos test) | Simple changes outperform complex ones |
| Fee drag at low equity | Strong (multiple markets) | Use $0 commission for metric comparison |

---

## Recording New Findings

### When to record

Record to brain when:
- An experiment produces a **non-obvious** result
- A **decision is closed** (won't be revisited)
- A **pattern is confirmed** across multiple tests
- A **lesson is learned** from an incident

### How to record

1. **Experiment result**: Append to the strategy's TSV file in `research/results/`
2. **System-level finding**: Update `memory/SUMMARY.md`
3. **Operational lesson**: Update `tasks/lessons.md`
4. **Decision closure**: Add to Closed Decisions in `memory/SUMMARY.md`

### Recording format for memory/SUMMARY.md

```markdown
## New Finding (YYYY-MM-DD)

**What was tested:** [hypothesis]
**Result:** [metrics — Sharpe, CAGR, trades, etc.]
**Conclusion:** [what this means for the system]
**Action:** [what changes, if any]
```

---

## Anti-Pattern Checks

Before accepting a research conclusion, verify:

| Check | Why |
|-------|-----|
| Was it tested combined, not just solo? | Solo pass ≠ portfolio pass (#7) |
| Were there enough trades (>15)? | Few trades = degenerate solution (#2) |
| Did the hypothesis come before the data? | Post-hoc reasoning = unreliable (#27) |
| Is the failure infrastructure or hypothesis? | 8 infra bugs contaminated 15+ experiments (#17) |
| Was the filter_test format correct? | Missing params = silent no-op (#8, #31) |
| Was it at $0 commission? | Fee drag makes $4K solo tests useless (#16) |
