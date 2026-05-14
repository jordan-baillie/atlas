# Strategy Lifecycle — Remaining Sub-Phases (Deferred)

**Created**: 2026-05-06  
**Reason**: Foundations shipped (table, state machine, migration, tests, docs).  
Paper executor deferred to avoid shipping partial broker integration.  
**Depends on**: Phase 1 (foundations) — merged in same session.

> "Do NOT half-ship the paper-trading executor — better to defer with a clean spec than ship partial broker integration."  
> — Orchestrator instruction, 2026-05-06

---

## What was shipped in Phase 1 (foundations)

- [x] `strategy_lifecycle` + `strategy_lifecycle_history` tables in `db/schema.sql`
- [x] `db/atlas_db.py`: `get_lifecycle_state`, `set_lifecycle_state`, `list_lifecycle_states`
- [x] `monitor/strategy_lifecycle.py`: `PromotionState`, `get_state`, `transition`, `is_live`, `is_paper`, `list_state`
- [x] `scripts/migrations/2026-05-06-seed-strategy-lifecycle.py`: seeded 8 LIVE + 21 RESEARCH rows
- [x] `tests/test_strategy_lifecycle.py`: 35 tests (9 spec classes, all passing)
- [x] `docs/architecture/strategy-lifecycle.md`, `docs/runbooks/promote-strategy-paper-to-live.md`

---

## Sub-phase 1.2 — Paper Executor

### Pre-condition check

**Alpaca paper credentials status** (verified 2026-05-06):
- `.atlas-secrets.json` has `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` but `ALPACA_PAPER = "false"`
- No separate `ALPACA_PAPER_API_KEY` / `ALPACA_PAPER_SECRET_KEY` keys exist
- **Action needed**: obtain paper API credentials from Alpaca dashboard and add to secrets:
  ```json
  "ALPACA_PAPER_API_KEY": "...",
  "ALPACA_PAPER_SECRET_KEY": "...",
  "ALPACA_PAPER_BASE_URL": "https://paper-api.alpaca.markets"
  ```

### Acceptance criteria

- [ ] `config/active/<universe>.json` schema accepts `"mode": "paper"` (in addition to `"live"`, `"passive"`)
- [ ] `AlpacaBroker.__init__` accepts a `mode: str = "live"` parameter; when `mode="paper"`, connects to paper API using `ALPACA_PAPER_API_KEY` / `ALPACA_PAPER_SECRET_KEY` / `ALPACA_PAPER_BASE_URL`
- [ ] `LiveExecutor.__init__` accepts `mode: str = "live"`; passes through to broker
- [ ] `brokers/registry.py` `get_live_broker()` reads `config.get("mode", "live")` and passes to broker constructor
- [ ] `sync_protective_orders.py`, `intraday_monitor.py`, `eod_settlement.py` all call `get_live_broker(config)` without changes (they get paper broker transparently)
- [ ] Trades executed in paper mode are tagged `mode='paper'` in `trades` table (add `mode TEXT DEFAULT 'live'` column via migration)
- [ ] Unit test: mock paper broker, verify mode='paper' rows are tagged correctly

### Files to touch

| File | Change |
|------|--------|
| `brokers/alpaca/broker.py` | Add `mode` param to `__init__`; switch API creds based on mode |
| `brokers/registry.py` | Pass `config.get("mode", "live")` to `get_live_broker()` |
| `brokers/live_executor.py` | Add `mode` param; pass to broker |
| `config/active/*.json` | Schema change — add `"mode"` field |
| `db/schema.sql` | Add `mode TEXT DEFAULT 'live'` to `trades` table |
| `scripts/migrations/YYYY-MM-DD-add-mode-to-trades.py` | Migration |
| `tests/test_paper_executor.py` | NEW: unit tests for paper mode routing |

### Code patterns

```python
# brokers/alpaca/broker.py
class AlpacaBroker:
    def __init__(self, config: dict, mode: str = "live"):
        self._mode = mode
        if mode == "paper":
            api_key = secrets.get("ALPACA_PAPER_API_KEY")
            secret = secrets.get("ALPACA_PAPER_SECRET_KEY")
            base_url = secrets.get("ALPACA_PAPER_BASE_URL", "https://paper-api.alpaca.markets")
        else:
            api_key = secrets.get("ALPACA_API_KEY")
            secret = secrets.get("ALPACA_SECRET_KEY")
            base_url = secrets.get("ALPACA_BASE_URL", "https://api.alpaca.markets")
        # ... existing init logic
```

```python
# brokers/registry.py
def get_live_broker(config: dict) -> AlpacaBroker:
    mode = config.get("mode", "live")  # "live" | "paper" | "passive"
    if mode == "passive":
        raise ValueError("Cannot get live broker for passive universe")
    return AlpacaBroker(config, mode=mode)
```

### Estimated effort: 1 day

---

## Sub-phase 1.3 — Auto-Promotion Paper → Live

### Acceptance criteria

- [ ] `scripts/auto_promote_paper_to_live.py` exists and runs from cron
- [ ] Promotion gates (per strategy, per universe in PAPER state):
  - ≥ 30 calendar days in PAPER state (check `paper_start_date`)
  - Paper Sharpe ≥ 0.3 (computed from `SELECT * FROM trades WHERE mode='paper' AND universe=?`)
  - Paper Sharpe vs `research_best.sharpe` gap < 0.5
  - Paper trade count ≥ 10
  - No active divergence alert (`check_live_research_divergence.py` last 7 days clean)
- [ ] Passes → `transition(strategy, universe, PromotionState.LIVE, reason="auto_promote_paper_to_live", auto_promotion_id=run_id)`
- [ ] `config/active/<universe>.json` `mode` changed from `paper` to `live`
- [ ] Telegram notification: "✅ `{strategy}/{universe}` graduated to LIVE after `{N}d` paper phase. Paper Sharpe: `{sharpe:.2f}` vs research `{research_sharpe:.2f}`"
- [ ] `--dry-run` flag (default)
- [ ] Cron: `30 8 * * *` AEST (daily at 8:30 AM, before market open)

### Files to touch

| File | Change |
|------|--------|
| `scripts/auto_promote_paper_to_live.py` | NEW |
| `scripts/pi-cron.sh` | Add cron entry |
| `tests/test_auto_promote_paper_to_live.py` | NEW: 6 tests minimum |

### Key functions to reuse

```python
from monitor.strategy_lifecycle import transition, PromotionState, list_state
from db.atlas_db import get_db
from research.loop import _get_dsr_stats  # for DSR check

def _compute_paper_sharpe(strategy: str, universe: str) -> float:
    """Compute Sharpe from trades table WHERE mode='paper' AND universe=? AND strategy=?"""
    ...

def _check_promotion_gates(strategy: str, universe: str) -> tuple[bool, str]:
    """Returns (passes, reason). reason is human-readable for Telegram."""
    ...
```

### Estimated effort: 1 day

---

## Sub-phase 1.4 — Auto-Rollback Divergence Monitor

### Context

`scripts/check_live_research_divergence.py` (Rec 4) monitors divergence for
LIVE strategies. This sub-phase extends it to also handle PAPER strategies.

### Acceptance criteria

- [ ] `check_live_research_divergence.py` handles `state='PAPER'` rows from `strategy_lifecycle`
- [ ] For PAPER strategies: if Sharpe gap > 0.5 for 5 consecutive days → `transition(PAPER → RESEARCH)`
- [ ] For LIVE strategies: if persistent divergence → `transition(LIVE → PAPER)` (soft rollback, not RETIRE)
- [ ] Divergence alert state file tracks consecutive-day counts per (strategy, universe)
- [ ] Telegram alert on rollback: "⚠️ `{strategy}/{universe}` rolled back from `{state}` due to divergence (gap: `{gap:.2f}` Sharpe for `{n}` days)"
- [ ] Test: 5 consecutive divergence days triggers rollback; 4 days does NOT

### Files to touch

| File | Change |
|------|--------|
| `scripts/check_live_research_divergence.py` | Extend to handle PAPER state + soft-rollback for LIVE |
| `data/divergence_state.json` | Add per-(strategy, universe) consecutive-day counter |
| `tests/test_divergence_rollback.py` | NEW: 5 tests minimum |

### Estimated effort: 0.5 day

---

## Sub-phase 1.5 — Dashboard Controls Tab

### Acceptance criteria

- [ ] Strategy table in Controls tab shows `promotion_state` badge (RESEARCH / PAPER / LIVE / RETIRED)
- [ ] Badge colour coding: RESEARCH=blue, PAPER=yellow, LIVE=green, RETIRED=grey
- [ ] Clicking badge opens "Lifecycle History" modal showing `strategy_lifecycle_history` rows
- [ ] Manual transition modal: operator selects new state + types justification → calls new API endpoint
- [ ] New backend endpoint: `POST /api/strategy-lifecycle/transition` — validates operator, calls `transition(..., operator=username)`
- [ ] `GET /api/strategy-lifecycle` returns all rows from `strategy_lifecycle` as JSON
- [ ] PAPER strategies show `days_in_paper` and a mini-chart of paper vs live Sharpe (deferred if hard)

### Files to touch

| File | Change |
|------|--------|
| `services/api/` | NEW endpoint(s) for lifecycle read + transition |
| `dashboard-ui/src/components/Controls.tsx` | Lifecycle state badges + modal |
| `tests/test_lifecycle_api.py` | NEW: API endpoint tests |

### Note for frontend developer

The backend API shape should be:

```json
// GET /api/strategy-lifecycle
{
  "rows": [
    {
      "strategy": "momentum_breakout",
      "universe": "sp500",
      "state": "LIVE",
      "entered_state_at": "2026-05-06T...",
      "prev_state": null,
      "transition_reason": "Migration: pre-existing live strategy...",
      "paper_start_date": null,
      "paper_end_date": null,
      "auto_promotion_id": null,
      "notes": null
    }
  ]
}

// POST /api/strategy-lifecycle/transition
{
  "strategy": "mean_reversion",
  "universe": "sp500",
  "new_state": "RETIRED",
  "reason": "Suspended for 90 days — decommissioning",
  "operator": "alice"
}
```

### Estimated effort: 1.5 days (backend 0.5d + frontend 1d)

---

## Sub-phase 1.6 — Remaining Tests

Tests deferred from Phase 1 (foundations):

- [ ] `tests/test_paper_executor.py`: paper broker routing, paper order tagging, mode='paper' in trades
- [ ] `tests/test_auto_promote_paper_to_live.py`: full promotion gate test (6 cases minimum)
- [ ] `tests/test_divergence_rollback.py`: PAPER→RESEARCH rollback on 5 consecutive days
- [ ] `tests/test_lifecycle_api.py`: FastAPI endpoint tests for GET + POST lifecycle routes
- [ ] Pre-commit hook: add check that `strategy_lifecycle` row must exist before `config/active/*.json` `enabled=true` change (complements existing `.git/hooks/pre-commit` guard)

---

## Sub-phase 1.7 — Documentation ✅ COMPLETE (2026-05-14)

- [x] Update `docs/ARCHITECTURE.md` with full lifecycle (promotion + health, both machines)
- [x] Update `research/README.md` — add "Before Live" section explaining paper phase requirement
- [ ] Add to `scripts/pi-cron.sh` comments: paper-promotion and divergence-monitor cron entries
- [x] Update `tasks/audit_2026-05-06_followups.md` with paper-executor done entry (once shipped)

---

## Total estimated effort: ~4 days

| Sub-phase | Est | Complexity |
|-----------|-----|-----------|
| 1.2 Paper executor | 1d | Medium — broker abstraction layer |
| 1.3 Auto-promotion | 1d | Low — formula + cron |
| 1.4 Auto-rollback | 0.5d | Low — extend existing script |
| 1.5 Dashboard | 1.5d | Medium — frontend + backend |
| 1.6 Tests | included | — |
| 1.7 Docs | included | — |

**Critical path**: 1.2 (paper credentials) → 1.3 (uses paper trades) → 1.4 (uses promotion state)  
1.5 (dashboard) is parallel to 1.3/1.4.

---

## Sequencing for next session

1. **Verify Alpaca paper credentials** — get from `https://app.alpaca.markets/paper-trading` and add to `.atlas-secrets.json` as `ALPACA_PAPER_API_KEY` / `ALPACA_PAPER_SECRET_KEY` / `ALPACA_PAPER_BASE_URL`. Without this, 1.2 cannot be tested.
2. Ship 1.2 (paper executor) — isolated broker layer change, low risk
3. Ship 1.3 + 1.4 together — both touch `check_live_research_divergence.py`
4. Ship 1.5 (dashboard) — parallel, but needs 1.2 to be useful
