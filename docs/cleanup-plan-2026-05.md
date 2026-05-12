# Atlas Cleanup Plan — 2026-05

*Authored by user directive on 2026-05-12. See git tag `pre-cleanup-2026-05-12`.*
*Tier 1a executed: c40b2095e9b11b1e8226c7342ba36332358defbb. Subsequent tiers gated on dwell + verification.*

## Principles

- **Delete by safety class, not by line count.**
- **Attic, not graveyard** — `git mv` to `_attic/2026-05/<category>/`, never `rm`.
- 14-day dwell minimum before considering permanent removal.
- Tier ordering is non-negotiable. Do NOT skip ahead.

## Pre-flight (executed 2026-05-12)

1. Tag baseline: `git tag pre-cleanup-2026-05-12`
2. Capture baseline outputs to /tmp/:
   - `pytest tests/ -q --timeout=30` (related tests: 3 failed, 111 passed)
   - `python3 scripts/verify_dual_write.py` (4/6 PASS — sp500 equity mismatch pre-existing)
   - `python3 scripts/cli.py status` (1 open pos CAT, 25 closed)
3. Create `_attic/2026-05/{scripts,strategies,markets,research,docs,plans,snapshots,dashboard,reports}/` with README documenting recovery procedure.

## Tier 1a — Dated one-off scripts (executed 2026-05-12)

Move forensic scripts that ran once for a past incident with no active
cron, systemd, python-import, or test-file-hard-dependency references.

**Candidate list (12 nominated):**

| Script | Moved? | Reason if held |
|--------|--------|----------------|
| scripts/backfill_trades.py | ✅ moved | — |
| scripts/cleanup_dummy_ohlcv.py | ✅ moved | — |
| scripts/retro_attach_tp_legs.py | ✅ moved | — |
| scripts/fix4_resweep_contaminated.sh | 🟡 held | systemd: atlas-fix4-resweep.service (disabled/inactive) |
| scripts/fix5_mr_sp500_sweep.sh | 🟡 held | systemd: atlas-fix5-mr-sp500-sweep.service (disabled/inactive) |
| scripts/resweep_2026_04_28.sh | 🟡 held | systemd: atlas-resweep-20260428.service (/etc/systemd only) |
| scripts/backfill_errors_from_logs.py | 🟡 held | test import: test_backfill_errors_from_logs.py (module-level) |
| scripts/backfill_orphan_trades.py | 🟡 held | test import: test_backfill_orphan_trades_universe.py, test_dual_write_leak_regression.py, test_trade_invariants.py |
| scripts/backfill_regime_gap_apr2026.py | 🟡 held | test subprocess: test_regime_gap_backfill.py |
| scripts/backfill_vix.py | 🟡 held | test path-read: test_vix_tmp_race.py |
| scripts/migrate_to_oco.py | 🟡 held | test importlib: test_migrate_to_oco.py |
| scripts/dual_write_d1_rollback.sh | 🟡 held | test path-check: test_journal_d1_cutover.py (bash -n) |

**Note on held-back scripts with test dependencies**: The 4-check audit
(cron/systemd/python-import) explicitly excludes the `tests/` directory.
However, 6 of the 12 candidates have test files that directly import or
path-reference them. Moving those scripts would reduce the pytest pass
count, violating the acceptance criterion. They are held back to preserve
the acceptance criterion. These should be moved in a future tier
alongside their test files (or after the tests are updated to not
hard-reference the script path).

**Pre-move audit checks** (all 4 must pass per file):
1. File exists.
2. Not referenced in `scripts/atlas.crontab`.
3. Not referenced in any `/etc/systemd/system/atlas-*.{service,timer}` or `/root/atlas/systemd/`.
4. Not imported by active Python code outside `_attic/`, `tests/`, `__pycache__/`.

**Additional conservative check applied**: No test file hard-dependency
(direct import, path read, subprocess call) that would reduce pytest pass count.

**Post-move verification:**
- pytest (related test files): 3 failed / 111 passed = UNCHANGED
- verify_dual_write.py: 4/6 PASS = UNCHANGED
- cli.py status: 1 open pos CAT = UNCHANGED

## Tier 1b / 1c / 2 / 3 / 4 — NOT YET DEFINED IN THIS REPO

The user's full 4-tier plan was not passed through to the orchestrator on 2026-05-12;
only pre-flight + Tier 1a were specified. Subsequent tiers must be documented here
BEFORE execution.

Required tier-template fields (for future tiers):
- Safety class (what makes this safe to move)
- Candidate list (explicit paths)
- Pre-move audit checks (file-exists, cron-ref, systemd-ref, python-import, doc-ref, etc.)
- Post-move verification commands
- Acceptance criteria
- Dwell period

**Suggested next steps for Tier 1b (9 held-back scripts):**
- For the 3 systemd-held scripts: confirm the services are truly dead (disabled + inactive),
  remove the unit files from `/etc/systemd/system/` and `systemd/`, then move the scripts.
- For the 6 test-held scripts: either (a) move script + co-located test file together,
  or (b) update the test to not hard-reference the script path, then move.

## Recovery

- Single file: `git mv _attic/2026-05/<dir>/<file> <original-path>/`
- Whole tier: `git checkout pre-cleanup-2026-05-12 -- <path>`

## Permanent removal (future)

After 2026-05-26, if no incident referenced anything in `_attic/2026-05/`,
candidates may be `git rm`'d in a separate commit. Always retain the tag
`pre-cleanup-2026-05-12` indefinitely as the recovery anchor.
