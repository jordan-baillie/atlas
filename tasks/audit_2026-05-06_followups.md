# Audit 2026-05-06 Follow-up Tasks

Tracks deferred items from the research-system-audit-2026-05-06. All immediate
gate fixes (Rec 1.1-1.4, 1.6) were shipped in commit A of the same session.

## Pending

- [ ] **Audit Rec 1.5 — Paper-trade executor (sub-phases 1.2–1.5)**: paper executor broker plumbing, auto-promotion cron, auto-rollback, dashboard Controls tab. Full spec in `tasks/strategy_lifecycle_remaining.md`. Est 1–2 days. **Pre-condition RESOLVED** (2026-05-06): `ALPACA_PAPER_API_KEY` + `ALPACA_PAPER_SECRET_KEY` + `ALPACA_PAPER_ENDPOINT` are all present in `.atlas-secrets.json`. Next step: fix `execute_approved.py` paper mode gate (Issue 2 in Phase B section below), then per-strategy routing (Issue 1), then pick sp500 candidate (short_term_mr/sp500 Sharpe=1.27 is best candidate).


## Phase B — Paper-trading dogfood activation (2026-05-06 audit)

### Status: ❌ BLOCKED — two design issues prevent safe activation

**Investigated 2026-05-06.** No clean candidate found. Phase B activation deferred.

---

### Issue 1: Universe-level mode vs. (strategy, universe)-level lifecycle state

`trading.mode` in `config/active/<universe>.json` is **universe-scoped**.  
`lifecycle_state` in `strategy_lifecycle` table is **(strategy, universe)-scoped**.

If we flip any universe that contains LIVE strategies to `mode=paper`, those LIVE
strategies start routing to the paper broker — a silent real-money regression.

#### Full audit table (as of 2026-05-06)

| Universe | LIVE | RESEARCH | current `mode` | Safe to flip? | Best RESEARCH Sharpe |
|----------|------|----------|----------------|----------------|----------------------|
| commodity_etfs | 3 | 1 | passive | ❌ (3 LIVE) | trend_following=0.337 ❌ |
| defensive_etfs | 0 | 1 | passive | ✅ only safe | mean_reversion=0.2159 ❌ |
| gold_etfs | 1 | 2 | passive | ❌ (1 LIVE) | momentum_breakout=0.334 ❌ |
| sector_etfs | 2 | 1 | passive | ❌ (2 LIVE) | connors_rsi2=0.299 ❌ |
| sp500 | 2 | 16 | live | ❌ (2 LIVE) | short_term_mr=1.27 ✅ |

**Only safe universe** (`defensive_etfs`): has no LIVE strategies, so mode-flip is
safe. BUT its one RESEARCH combo (`mean_reversion`) has Sharpe=0.2159 — fails the
≥0.5 gate. Also: all strategies are disabled (`enabled: false, weight: 0`) and
explicitly marked "pending re-research or retirement" in the config `_audit` note.
Even if activated, no signals would be generated.

**sp500 has 8+ qualifying RESEARCH combos** (opening_gap=0.60, short_term_mr=1.27,
consecutive_down_days=0.70, mean_reversion=0.57, trend_following=0.66…) but
flipping sp500 to `mode=paper` would break `connors_rsi2/sp500` and
`momentum_breakout/sp500` which are LIVE.

#### Proposed design fix — per-strategy mode routing in `execute_approved.py`

Rather than universe-level mode, split each plan's entries/exits by the lifecycle
state of the originating strategy and route to separate executors:

```python
# scripts/execute_approved.py — after loading the plan

from monitor.strategy_lifecycle import is_paper as _is_paper

def _split_by_mode(entries: list, universe: str) -> tuple[list, list]:
    """Return (live_entries, paper_entries) based on lifecycle state."""
    live_es, paper_es = [], []
    for e in entries:
        if _is_paper(e.get("strategy", ""), universe):
            paper_es.append(e)
        else:
            live_es.append(e)
    return live_es, paper_es

# Replace current "if mode != 'live': return" block with:
live_entries, paper_entries = _split_by_mode(entries, market_id)
live_exits,   paper_exits   = _split_by_mode(exits,   market_id)

if live_entries or live_exits:
    _execute_with_executor(config, plan, live_entries, live_exits, mode="live", ...)

if paper_entries or paper_exits:
    paper_cfg = {**config, "trading": {**config["trading"], "mode": "paper"}}
    _execute_with_executor(paper_cfg, plan, paper_entries, paper_exits, mode="paper", ...)
```

This allows sp500 to simultaneously run LIVE strategies on real money and PAPER
strategies on the virtual account, without any universe-level config changes.

Files to touch: `scripts/execute_approved.py` (primary change — refactor main into
`_execute_with_executor(config, plan, entries, exits, mode)`; add split logic above;
remove the `if mode != "live": return` early bail).

---

### Issue 2: `execute_approved.py` missing paper mode gate (infrastructure gap)

Even if we had a clean candidate with a safe universe, paper execution would fail.
`execute_approved.py` line 72 has:

```python
if mode != "live":
    log.info("Trading mode is '%s', not 'live' — skipping", mode)
    return
```

This bails BEFORE creating the executor. The `LiveExecutor` (commit `4e850f30`)
already fully supports `mode="paper"` — routes DB writes to `paper_trades`, uses
`ALPACA_PAPER_API_KEY`/`ALPACA_PAPER_SECRET_KEY`. Paper credentials ARE present in
`.atlas-secrets.json` (`ALPACA_PAPER_API_KEY`, `ALPACA_PAPER_SECRET_KEY`,
`ALPACA_PAPER_ENDPOINT` all confirmed present).

**Minimal fix** (separate from Issue 1 design fix):
```python
# execute_approved.py line 72 — change:
if mode != "live":
# to:
if mode not in ("live", "paper"):
```

This unblocks paper mode routing through the existing executor code path.
This fix is LOW RISK and should be landed before Issue 1 (it's ~2 lines).

---

### Action items (in order)

1. **Fix `execute_approved.py`** — change `mode != "live"` → `mode not in ("live", "paper")` (~2 lines)
2. **Implement per-strategy mode routing** in `execute_approved.py` (see Issue 1 fix above)
3. **Re-sweep `defensive_etfs`** with regime-aware strategies (current `mean_reversion` Sharpe=0.22 is below bar; universe needs fresh research sweep)
4. **OR**: once Issue 2 fix lands, pick any qualifying sp500 RESEARCH combo (e.g. `short_term_mr/sp500` Sharpe=1.27, 294 trades) and activate via `transition(strategy, universe, PromotionState.PAPER)` — no config file change needed since executor routes by lifecycle state
5. Estimated unblock: 1 day of implementation work (Issues 1+2)

---

### Credentials status (verified 2026-05-06)

- `ALPACA_PAPER_API_KEY`: ✅ present in `.atlas-secrets.json`
- `ALPACA_PAPER_SECRET_KEY`: ✅ present
- `ALPACA_PAPER_ENDPOINT`: ✅ present (overrides remaining spec doc which said creds missing)
- Note: `tasks/strategy_lifecycle_remaining.md` Sub-phase 1.2 pre-condition check is OUTDATED — credentials were added prior to this session; update that doc accordingly.

## Done in this session (lifecycle foundations — 2026-05-06)

- [x] **1.1 — `strategy_lifecycle` + `strategy_lifecycle_history` tables**: schema in `db/schema.sql`; helpers `get_lifecycle_state`, `set_lifecycle_state`, `list_lifecycle_states` in `db/atlas_db.py`.
- [x] **`monitor/strategy_lifecycle.py`** (NEW, separate from `monitor/lifecycle.py` health machine): `PromotionState` enum, `transition()` with graph enforcement, `get_state`, `is_live`, `is_paper`, `list_state`.
- [x] **Migration `scripts/migrations/2026-05-06-seed-strategy-lifecycle.py`**: seeded production DB with 8 LIVE + 21 RESEARCH combos. Idempotent, `--dry-run` default.
- [x] **1.6 (partial) — `tests/test_strategy_lifecycle.py`**: 35 tests covering schema, transitions, history, paper dates, disallowed transitions, migration script. All passing.
- [x] **1.7 (partial) — Documentation**: `docs/architecture/strategy-lifecycle.md` (why two machines, state diagram, persistence, Python API); `docs/runbooks/promote-strategy-paper-to-live.md` (manual ops today + future automated workflow).
- [x] **`tasks/strategy_lifecycle_remaining.md`**: detailed deferred spec for sub-phases 1.2–1.5 including Alpaca paper creds status, file ownership table, code patterns, effort estimates.

## Done in this session

- [x] **Rec 1.1** — DSR gate: per-strategy variance (was cross-strategy, inflated to >3.0 sanity cap every session). Fixed in `research/loop.py` `_get_dsr_stats(strategy, market)`.
- [x] **Rec 1.2** — IS Sharpe floor raised from `> 0` to `>= 0.5` in `_sanity_check`. OOS Sharpe floor raised from `> 0` to `>= 0.3` in `_run_oos_validation`.
- [x] **Rec 1.3** — OOS trade-count floor 10 → 30 in both `_run_oos_validation` and `keep_or_discard`.
- [x] **Rec 1.4** — CAGR degradation gate (trivially passes at negative CAGR) replaced by absolute OOS CAGR ≥ 5% floor.
- [x] **Rec 1.6** — Pre-commit hook blocks direct edits to `config/active/*.json` without auto_promote audit trail. Bypass: `BYPASS_RESEARCH_GATE="reason" git commit` or `git commit --no-verify`.
