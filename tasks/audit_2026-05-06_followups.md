# Audit 2026-05-06 Follow-up Tasks

Tracks deferred items from the research-system-audit-2026-05-06. All immediate
gate fixes (Rec 1.1-1.4, 1.6) were shipped in commit A of the same session.

## Pending

- [ ] **Audit Rec 1.5 — Paper-trade executor (sub-phases 1.2–1.5)**: paper executor broker plumbing, auto-promotion cron, auto-rollback, dashboard Controls tab. Full spec in `tasks/strategy_lifecycle_remaining.md`. Est 4 days. Pre-condition: obtain Alpaca paper API credentials from `https://app.alpaca.markets/paper-trading` (not currently in `.atlas-secrets.json`).

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
