# Unified Auto-Promotion Pipeline

## Problem

Two disconnected promotion paths exist:
1. **sweep.py `_check_promotions()`** — writes candidate file + Telegram alert, stops there
2. **research_promote.py** — full pipeline (stage → validate → Telegram buttons → human approve → promote) but never called by sweep

Result: sweep finds improvements, writes candidates to `config/candidates/`, nobody picks them up. Human has to manually run `research_promote.py` or copy files.

## Goal

Single promotion flow. Auto-approve. No human in the loop. But properly validated so bad params never reach active config.

## Design

### What validation is already happening BEFORE promotion

The sweep already applies serious filters before anything reaches `_check_promotions()`:

1. **Solo backtest** — param must beat baseline Sharpe (per-strategy)
2. **Combined portfolio test** — param change must not degrade portfolio Sharpe by > 0.02
3. **Multiple-param accumulation** — improvements compound across the grid; only the best value per param survives
4. **Sharpe delta > 0.05 threshold** — only strategies with meaningful improvement get promoted

This is already more rigorous than a single OOS test. The combined test IS the regression check — it runs the full portfolio with all enabled strategies.

### What's missing

1. **No version control** — candidate is written but active config never updated
2. **No rollback safety** — if a promotion is bad, no way to detect + revert
3. **No rate limiting** — could promote the same strategy every cycle (every ~1h)
4. **No audit trail** — promotions not recorded in brain or journal
5. **No portfolio-level sanity check** — individual strategy improves but what about max drawdown, total trade count, etc.?

### The unified flow

```
sweep.py finds improvement (Sharpe delta > threshold)
    │
    ▼
auto_promote(strategy, improved_params, metrics)
    │
    ├── Gate 1: Cooldown (min 24h between promotions per strategy)
    ├── Gate 2: Regression check (candidate config vs active config)
    │           • Run full portfolio backtest with candidate params
    │           • No metric degrades > 10% (Sharpe, CAGR, Sortino, PF)
    │           • Max drawdown doesn't increase > 3pp
    │           • Trade count doesn't drop > 20% (avoid degenerate configs)
    ├── Gate 3: Sanity bounds
    │           • Portfolio Sharpe must remain > 0.0 (absolute floor)
    │           • CAGR must remain > 0% (not losing money)
    │           • At least 20 trades (statistical significance)
    │
    ├── ON PASS:
    │   ├── Version current active → config/versions/
    │   ├── Write new active config → config/active/sp500.json
    │   ├── Record in brain (decisions/ + experiments/)
    │   ├── Send Telegram: "✅ Auto-promoted {strategy} — Sharpe +X.XX"
    │   │   with [↩️ Rollback] button
    │   └── Log promotion to config/promotion_log.json
    │
    └── ON FAIL:
        ├── Keep candidate in config/candidates/ for inspection
        ├── Send Telegram: "⚠️ {strategy} promotion blocked: {reason}"
        └── Record in brain (decisions/)
```

### Rollback mechanism

```
config/promotion_log.json — append-only log:
[
  {
    "timestamp": "...",
    "strategy": "mean_reversion",
    "prev_version": "v2.2",
    "new_version": "v2.3",
    "prev_config_path": "config/versions/sp500_v2.2.json",
    "delta_sharpe": 0.07,
    "auto": true
  }
]

Telegram [↩️ Rollback] button → restores prev_config_path to active
```

### What to delete / retire

- `_check_promotions()` in sweep.py — replaced by `auto_promote()`
- `research_promote.py` stage/validate/promote functions — consolidated into new module
- Keep `research_promote.py` watchdog + reject for manual use
- Telegram approval callback stays (repurposed for rollback)

## Implementation

### New file: `research/promoter.py` (~200 lines)

Single module, single responsibility. Functions:

```python
def auto_promote(
    strategy: str,
    improved_params: dict,
    initial_sharpe: float,
    final_sharpe: float,
    improvements: list,
    market: str = "sp500",
) -> dict:
    """Unified auto-promotion. Returns {promoted: bool, reason: str, version: str}."""

def _check_cooldown(strategy: str) -> bool:
    """24h per-strategy cooldown."""

def _regression_check(candidate_config: dict, market: str) -> dict:
    """Full portfolio backtest comparison. Returns {pass: bool, comparisons: dict}."""

def _sanity_check(metrics: dict) -> dict:
    """Absolute floors: Sharpe > 0, CAGR > 0, trades >= 20."""

def _do_promote(candidate_config: dict, market: str, metadata: dict) -> str:
    """Version + write active + log. Returns new version string."""

def _notify(result: dict) -> None:
    """Telegram notification with rollback button."""

def rollback(market: str) -> dict:
    """Restore previous version from promotion_log.json."""
```

### Changes to sweep.py (~10 lines)

Replace:
```python
if cycle_results:
    _check_promotions(cycle_results)
```

With:
```python
if cycle_results:
    from research.promoter import auto_promote
    for cr in cycle_results:
        if cr["improvements"]:
            auto_promote(
                strategy=cr["strategy"],
                improved_params=cr["improved_params"],
                initial_sharpe=cr["initial_sharpe"],
                final_sharpe=cr["final_sharpe"],
                improvements=cr["improvements"],
                market=cr["market"],
            )
```

### Changes to telegram_bot.py (~20 lines)

- Repurpose `research:{id}:approve:{market}` → `research:{version}:rollback:{market}`
- Add `handle_rollback_callback` that calls `promoter.rollback()`

### Changes to brain/writer.py (~10 lines)

- Add `record_promotion()` to write to `brain/decisions/`

### Files touched

| File | Change |
|------|--------|
| `research/promoter.py` | NEW — unified promotion logic |
| `research/sweep.py` | Replace `_check_promotions` call → `auto_promote` |
| `services/telegram_bot.py` | Add rollback callback handler |
| `research/brain/writer.py` | Add `record_promotion()` |
| `scripts/research_promote.py` | Keep for manual use, mark stage/promote as deprecated |

## Timing estimate

- `_regression_check()` runs a single full-portfolio backtest: ~30-60s
- Runs once per strategy per cycle (not per param)
- Won't bottleneck the sweep — it already takes minutes per strategy

## What this does NOT do

- No OOS validation in the auto path (too slow, ~2h). OOS is for periodic audits.
- No approval buttons — fully automatic. Rollback button is the safety valve.
- No cross-market promotion (SP500 only for now, ASX is paused).

## Risk mitigation

1. **Bad param promoted**: Rollback button in Telegram, or `promoter.rollback('sp500')` CLI
2. **Cascade of bad promotions**: 24h cooldown + regression gate means max 1 bad promotion per day per strategy
3. **Config corruption**: Every promotion versions the previous config first
4. **Silent degradation**: Promotion log + brain records make audit trail easy
