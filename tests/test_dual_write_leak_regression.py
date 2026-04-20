"""Regression tests for dual-write leak fixes (Task #192).

Covers commits 3e3d53a5 (reconcile_ledger+positions) and d70ecc52
(backfill_orphan_trades.py). Protects against re-regression of:

- Bug A: reconcile_ledger hardcoding strategy='reconciled' instead of
         calling _lookup_strategy() in the record_trade_entry call.
- Bug B: reconcile_ledger filtering out state-file-only tickers (e.g. XLY)
         that are held by the market but not in the universe definition.
- Bug C: reconcile_positions --fix writing JSON only, not SQLite
         (dual-write to atlas_db.record_trade_entry was missing).

Test layout
-----------
  TestReconcileLedgerUsesRealStrategy   (Bug A — source shape checks)
  TestLookupStrategyPriority            (Bug A — functional unit tests)
  TestReconcileLedgerAcceptsStateFileTickers (Bug B — source shape checks)
  TestReconcilePositionsFixWritesSQLite (Bug C — source ordering checks)
  TestBackfillOrphanTradesIdempotent    (backfill script — guard + functional)
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import db.atlas_db as _adb
from db.atlas_db import init_db


# ─── Shared fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Point atlas_db at a throw-away temp DB so tests never touch production."""
    db_path = str(tmp_path / "test_dual_write.db")
    monkeypatch.setattr(_adb, "_db_path_override", db_path)
    init_db()
    yield
    # Restore to ensure subsequent test modules are not affected
    monkeypatch.setattr(_adb, "_db_path_override", None)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1 — Bug A: reconcile_ledger must use _lookup_strategy in the backfill
# ═══════════════════════════════════════════════════════════════════════════════

class TestReconcileLedgerUsesRealStrategy:
    """Source shape checks that protect Bug A from regressing.

    Bug A (AMD strategy drift): before the fix, reconcile_ledger passed
    strategy="reconciled" literally to record_trade_entry, permanently
    losing the real strategy name (e.g. 'momentum_breakout').  After the
    fix, record_trade_entry receives the result of _lookup_strategy().
    """

    def test_record_trade_entry_uses_lookup_strategy_not_hardcoded(self):
        """record_trade_entry strategy= arg is _lookup_strategy(...), not 'reconciled'.

        Shape check: within the backfill section (section 4), the
        record_trade_entry call must use _lookup_strategy() for the
        strategy keyword — not a hardcoded 'reconciled' string.
        """
        src = (PROJECT / "scripts" / "reconcile_ledger.py").read_text()

        # Locate the backfill section (section 4 header comment)
        backfill_marker = "# 4. Broker has position NOT in ledger"
        backfill_idx = src.index(backfill_marker)

        # Find the record_trade_entry call within the backfill section
        record_idx = src.index("record_trade_entry(", backfill_idx)

        # Extract the call block (400 chars covers all kwargs)
        call_block = src[record_idx: record_idx + 400]

        # Must use _lookup_strategy as the strategy value, not a literal
        assert "_lookup_strategy(" in call_block, (
            "Bug A: strategy= kwarg inside record_trade_entry must be "
            "_lookup_strategy(...), not a hardcoded string"
        )

    def test_strategy_reconciled_not_hardcoded_in_backfill(self):
        """record_trade_entry call must NOT contain the literal strategy='reconciled'.

        Ensures the old broken pattern cannot reappear silently.
        """
        src = (PROJECT / "scripts" / "reconcile_ledger.py").read_text()

        backfill_idx = src.index("# 4. Broker has position NOT in ledger")
        record_idx = src.index("record_trade_entry(", backfill_idx)
        call_block = src[record_idx: record_idx + 400]

        assert 'strategy="reconciled"' not in call_block, (
            'Bug A: strategy="reconciled" must NOT be hardcoded in the '
            "record_trade_entry call; use _lookup_strategy() instead"
        )
        assert "strategy='reconciled'" not in call_block, (
            "Bug A: strategy='reconciled' must NOT be hardcoded in the "
            "record_trade_entry call; use _lookup_strategy() instead"
        )

    def test_lookup_strategy_function_exists_in_module(self):
        """_lookup_strategy helper must be defined in reconcile_ledger.py.

        Guards against the helper being renamed or deleted, which would
        silently re-introduce Bug A.
        """
        src = (PROJECT / "scripts" / "reconcile_ledger.py").read_text()
        assert "def _lookup_strategy(" in src, (
            "Bug A: _lookup_strategy() helper must be defined in "
            "scripts/reconcile_ledger.py"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2 — Bug A: _lookup_strategy priority logic (unit tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestLookupStrategyPriority:
    """Functional unit tests for _lookup_strategy's three-tier priority.

    Priority: broker state file (strategy != 'unknown') > plan file scan
              > 'reconciled' fallback (last resort).

    These tests directly import the helper and assert its return value.
    """

    def test_case_a_returns_state_strategy_when_non_unknown(self):
        """Case A: state_positions contains a real (non-unknown) strategy.

        The helper must return that strategy immediately without scanning
        plan files or falling back.
        """
        from scripts.reconcile_ledger import _lookup_strategy

        state_positions = {
            "AMD": {
                "strategy": "momentum_breakout",
                "shares": 2,
                "entry_price": 178.5,
            }
        }
        result = _lookup_strategy("AMD", "sp500", state_positions)
        assert result == "momentum_breakout", (
            "Case A: non-unknown strategy from state_positions must be returned directly"
        )

    def test_case_a_does_not_return_unknown_strategy(self, tmp_path):
        """Case A guard: strategy='unknown' in state must NOT be returned.

        When the state file says 'unknown', the helper must fall through
        to plan files (Case B) or 'reconciled' fallback (Case C).
        """
        from scripts.reconcile_ledger import _lookup_strategy

        # No plan files → will hit 'reconciled' fallback
        (tmp_path / "plans").mkdir()

        with patch("scripts.reconcile_ledger.PROJECT", tmp_path):
            result = _lookup_strategy(
                "AMD", "sp500", {"AMD": {"strategy": "unknown"}}
            )

        assert result != "unknown", (
            "Case A: strategy='unknown' must not be returned; "
            "must fall through to plan scan or 'reconciled'"
        )
        assert result == "reconciled", (
            "Case A (fall-through): should reach 'reconciled' when state is "
            "'unknown' and no plan files exist"
        )

    def test_case_b_falls_back_to_plan_file_when_state_unknown(self, tmp_path):
        """Case B: state has 'unknown' → plan file scan returns real strategy.

        Creates a tmp plan file with AMD → mtf_momentum and patches PROJECT
        so _lookup_strategy reads it.
        """
        from scripts.reconcile_ledger import _lookup_strategy

        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        plan_file = plans_dir / "plan_sp500_20260410.json"
        plan_file.write_text(
            json.dumps(
                {
                    "proposed_entries": [
                        {
                            "ticker": "AMD",
                            "strategy": "mtf_momentum",
                            "entry_price": 178.5,
                        }
                    ]
                }
            )
        )

        with patch("scripts.reconcile_ledger.PROJECT", tmp_path):
            result = _lookup_strategy(
                "AMD", "sp500", {"AMD": {"strategy": "unknown"}}
            )

        assert result == "mtf_momentum", (
            "Case B: strategy from plan file must be returned when state has 'unknown'"
        )

    def test_case_b_uses_newest_plan_first(self, tmp_path):
        """Case B: multiple plan files → newest (sorted descending) takes priority.

        An older plan (04-01) has 'old_strategy', a newer plan (04-10) has
        'new_strategy'. The helper must pick the newer one.
        """
        from scripts.reconcile_ledger import _lookup_strategy

        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()

        (plans_dir / "plan_sp500_20260401.json").write_text(
            json.dumps(
                {"proposed_entries": [{"ticker": "AMD", "strategy": "old_strategy"}]}
            )
        )
        (plans_dir / "plan_sp500_20260410.json").write_text(
            json.dumps(
                {"proposed_entries": [{"ticker": "AMD", "strategy": "new_strategy"}]}
            )
        )

        with patch("scripts.reconcile_ledger.PROJECT", tmp_path):
            result = _lookup_strategy("AMD", "sp500", {})

        assert result == "new_strategy", (
            "Case B: newest plan file (sorted descending) must take priority "
            "over older plans"
        )

    def test_case_c_fallback_returns_reconciled_with_warning(self, tmp_path, caplog):
        """Case C: no state strategy + no plan match → 'reconciled' + WARNING logged.

        This is the last-resort fallback.  A WARNING must be emitted so that
        audit tooling can find positions with unresolved strategies.
        """
        from scripts.reconcile_ledger import _lookup_strategy

        # Empty plans dir — no matching files
        (tmp_path / "plans").mkdir()

        with caplog.at_level(logging.WARNING):
            with patch("scripts.reconcile_ledger.PROJECT", tmp_path):
                result = _lookup_strategy("AMD", "sp500", {})

        assert result == "reconciled", (
            "Case C: must return 'reconciled' when all lookups fail"
        )
        # A warning must be logged so auditors can detect this
        warning_messages = [r.message for r in caplog.records
                            if r.levelno >= logging.WARNING]
        assert any("reconciled" in msg for msg in warning_messages), (
            "Case C: a WARNING containing 'reconciled' must be logged "
            "when the fallback is reached"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3 — Bug B: state-file tickers accepted even when outside universe
# ═══════════════════════════════════════════════════════════════════════════════

class TestReconcileLedgerAcceptsStateFileTickers:
    """Source shape checks that protect Bug B from regressing.

    Bug B (XLY excluded): before the fix, the broker-position filter used
    only `universe_tickers`.  Tickers tracked in `live_{market}.json` but
    absent from the universe definition (e.g. sector ETFs like XLY) were
    silently skipped → never backfilled.  After the fix, the allow-set is
    the UNION of universe_tickers and state_tickers.
    """

    def test_source_computes_state_tickers_from_state_file(self):
        """reconcile_ledger.py must derive state_tickers from the live JSON file.

        Verifies that the variable `state_tickers` exists and is populated
        from the `live_{market_id}.json` path.
        """
        src = (PROJECT / "scripts" / "reconcile_ledger.py").read_text()
        assert "state_tickers" in src, (
            "Bug B: 'state_tickers' variable must be computed from "
            "brokers/state/live_{market_id}.json"
        )
        # The state file path must reference live_{market_id}.json
        assert "live_{market_id}.json" in src, (
            "Bug B: code must load the live_{market_id}.json state file "
            "to build state_tickers"
        )

    def test_source_allow_set_is_union_of_universe_and_state(self):
        """Broker filter allow-set must be (universe_tickers or set()) | state_tickers.

        This exact expression (or logically equivalent) is required so that
        state-file-only tickers like XLY pass the filter.
        """
        src = (PROJECT / "scripts" / "reconcile_ledger.py").read_text()
        assert "| state_tickers" in src, (
            "Bug B: allow-set must include '| state_tickers' (union operator) "
            "so that state-file-only tickers are not filtered out"
        )
        assert "(universe_tickers or set()) | state_tickers" in src, (
            "Bug B: allow-set expression must be exactly "
            "'(universe_tickers or set()) | state_tickers'"
        )

    def test_source_broker_filter_uses_allow_not_universe_alone(self):
        """Broker position filter must use _allow, not universe_tickers directly.

        Verifies that: (1) `_allow` is defined, (2) `_allow` is constructed
        from the union, (3) the broker_map filter uses `in _allow`.
        """
        src = (PROJECT / "scripts" / "reconcile_ledger.py").read_text()

        # _allow must be defined
        assert "_allow" in src, (
            "Bug B: _allow variable must be defined to hold the combined allow-set"
        )

        # _allow must be used in the broker_map filter
        assert "in _allow" in src, (
            "Bug B: broker position filter must use 'in _allow' "
            "(not 'in universe_tickers' alone)"
        )

        # Ordering: _allow must be defined before being used in the filter
        allow_def_idx = src.index("_allow =")
        in_allow_idx = src.index("in _allow")
        assert allow_def_idx < in_allow_idx, (
            "Bug B: _allow must be defined before it is used in the "
            "broker position filter"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4 — Bug C: reconcile_positions --fix must dual-write to SQLite
# ═══════════════════════════════════════════════════════════════════════════════

class TestReconcilePositionsFixWritesSQLite:
    """Source ordering checks that protect Bug C from regressing.

    Bug C (JSON-only fix): before the fix, reconcile_positions --fix called
    save_internal_state() but never wrote to SQLite.  After the fix, the
    same block also calls atlas_db.record_trade_entry() for each new position,
    guarded by an existence check and wrapped in try/except (non-fatal).
    """

    def test_fix_block_ordering_save_before_record_trade_entry(self):
        """fix block: save_internal_state must come before record_trade_entry.

        Verifies the required call ordering within the
        `if fix and result["discrepancies"] and not dry_run:` block:
          1. save_internal_state(  — preserves old behaviour
          2. record_trade_entry(   — new dual-write (added by Bug C fix)
        """
        src = (PROJECT / "scripts" / "reconcile_positions.py").read_text()

        fix_block_idx = src.index(
            'if fix and result["discrepancies"] and not dry_run:'
        )
        save_idx = src.index("save_internal_state(", fix_block_idx)
        dw_idx = src.index("record_trade_entry(", save_idx)

        assert fix_block_idx < save_idx < dw_idx, (
            "Bug C: within the fix block, save_internal_state must be called "
            "BEFORE record_trade_entry. "
            f"Indices: fix_block={fix_block_idx}, save={save_idx}, "
            f"record_trade_entry={dw_idx}"
        )

    def test_fix_block_dual_write_wrapped_in_try_except(self):
        """record_trade_entry dual-write must be inside try/except (non-fatal).

        The dual-write is a best-effort operation — JSON is the source of
        truth for live positions.  A failure here must not crash the fix.
        """
        src = (PROJECT / "scripts" / "reconcile_positions.py").read_text()

        fix_block_idx = src.index(
            'if fix and result["discrepancies"] and not dry_run:'
        )
        dw_idx = src.index("record_trade_entry(", fix_block_idx)

        # The last `try:` before the record_trade_entry call must be within
        # the fix block (index > fix_block_idx)
        try_idx = src.rindex("try:", 0, dw_idx)
        assert try_idx > fix_block_idx, (
            "Bug C: record_trade_entry dual-write must be inside a try/except "
            "block so that DB failures are non-fatal"
        )

    def test_fix_block_existence_check_before_insert(self):
        """fix block: must query existing open trades before inserting.

        Prevents duplicate rows when --fix is run more than once.
        """
        src = (PROJECT / "scripts" / "reconcile_positions.py").read_text()

        fix_block_idx = src.index(
            'if fix and result["discrepancies"] and not dry_run:'
        )
        dw_idx = src.index("record_trade_entry(", fix_block_idx)

        # The section between fix_block and record_trade_entry must contain
        # a SELECT ... WHERE status='open' guard
        block = src[fix_block_idx:dw_idx]
        assert "status='open'" in block, (
            "Bug C: dual-write section must query existing open trades "
            "(WHERE status='open') before inserting to prevent duplicates"
        )
        assert "_existing" in block, (
            "Bug C: dual-write section must maintain an '_existing' set "
            "of already-tracked tickers to skip"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5 — backfill_orphan_trades idempotency
# ═══════════════════════════════════════════════════════════════════════════════

class TestBackfillOrphanTradesIdempotent:
    """Regression tests for scripts/backfill_orphan_trades.py idempotency.

    Running the script a second time against an already-synced DB must not
    create duplicate rows.  The INSERT path is guarded by an existence check
    (load_sqlite_open_trades() is called first; 'if not sqlite_rows:' gates
    the _do_insert call).
    """

    def test_source_insert_guarded_by_sqlite_rows_check(self):
        """Source: _do_insert is only called when sqlite_rows is empty.

        The `if not sqlite_rows:` guard ensures that a ticker already tracked
        in SQLite is never re-inserted on a subsequent run.
        """
        src = (PROJECT / "scripts" / "backfill_orphan_trades.py").read_text()

        run_idx = src.index("def run(")
        insert_call_idx = src.index("_do_insert(", run_idx)

        # `if not sqlite_rows:` must appear immediately before _do_insert
        guard_idx = src.rindex("if not sqlite_rows:", run_idx, insert_call_idx)
        assert run_idx < guard_idx < insert_call_idx, (
            "Idempotency: _do_insert must be guarded by 'if not sqlite_rows:' "
            "to prevent re-inserting tickers already tracked in SQLite"
        )

    def test_source_load_sqlite_open_trades_before_inserts(self):
        """Source: load_sqlite_open_trades() is called before any _do_insert().

        The full SQLite state snapshot is loaded up-front so that the
        'if not sqlite_rows:' guard has accurate data.
        """
        src = (PROJECT / "scripts" / "backfill_orphan_trades.py").read_text()

        run_idx = src.index("def run(")
        # Find end of run() body (next top-level function)
        run_body_end = src.index("\ndef ", run_idx + 10)
        run_body = src[run_idx:run_body_end]

        load_idx = run_body.index("load_sqlite_open_trades(")
        insert_idx = run_body.index("_do_insert(")

        assert load_idx < insert_idx, (
            "Idempotency: load_sqlite_open_trades() must be called before "
            "any _do_insert() so the guard has accurate data"
        )

    def test_functional_idempotent_no_duplicates(self, tmp_path):
        """Functional: running run() twice inserts AMD exactly once into SQLite.

        Setup:
          - Isolated tmp atlas.db (via autouse _isolate_db fixture)
          - One broker state file (live_sp500.json) with AMD
          - Empty SQLite trades table

        Run 1: AMD is missing → _do_insert creates 1 row.
        Run 2: AMD already tracked → no INSERT, still 1 row.
        """
        from scripts.backfill_orphan_trades import run, load_sqlite_open_trades

        # Set up tmp broker state directory
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "live_sp500.json").write_text(
            json.dumps(
                {
                    "positions": [
                        {
                            "ticker": "AMD",
                            "strategy": "mtf_momentum",
                            "entry_price": 178.5,
                            "shares": 5,
                            "entry_date": "2026-04-10T10:00:00",
                            "stop_price": 169.58,
                        }
                    ]
                }
            )
        )

        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()

        with patch("scripts.backfill_orphan_trades.BROKER_STATE_DIR", state_dir), \
                patch("scripts.backfill_orphan_trades.PLANS_DIR", plans_dir):

            # ── First run: AMD not yet in SQLite → INSERT ──────────────────
            rc1 = run(dry_run=False, quiet=True)
            assert rc1 == 0, f"First run must succeed (rc=0), got rc={rc1}"

            open_after_run1 = load_sqlite_open_trades()
            assert "AMD" in open_after_run1, (
                "AMD must appear in SQLite after the first run"
            )
            assert len(open_after_run1["AMD"]) == 1, (
                f"Exactly 1 AMD row expected after run 1, "
                f"got {len(open_after_run1['AMD'])}"
            )

            # ── Second run: AMD already tracked → no INSERT ────────────────
            rc2 = run(dry_run=False, quiet=True)
            assert rc2 == 0, f"Second run must succeed (rc=0), got rc={rc2}"

            open_after_run2 = load_sqlite_open_trades()

        assert "AMD" in open_after_run2, (
            "AMD must still be in SQLite after the second run"
        )
        assert len(open_after_run2["AMD"]) == 1, (
            f"Idempotency violated: AMD must appear exactly once even after "
            f"running twice, got {len(open_after_run2['AMD'])} rows"
        )

    def test_functional_dry_run_reports_then_apply_then_no_changes(self, tmp_path):
        """Functional: dry-run shows N pending changes; apply; dry-run shows 0.

        Validates the full idempotency cycle:
          dry-run → pending changes N > 0
          apply   → AMD inserted
          dry-run → 0 pending changes
        """
        from scripts.backfill_orphan_trades import run, load_sqlite_open_trades
        import io
        import contextlib

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "live_sp500.json").write_text(
            json.dumps(
                {
                    "positions": [
                        {
                            "ticker": "NVDA",
                            "strategy": "momentum_breakout",
                            "entry_price": 450.0,
                            "shares": 3,
                            "entry_date": "2026-04-10T10:00:00",
                            "stop_price": 427.5,
                        }
                    ]
                }
            )
        )

        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()

        with patch("scripts.backfill_orphan_trades.BROKER_STATE_DIR", state_dir), \
                patch("scripts.backfill_orphan_trades.PLANS_DIR", plans_dir):

            # Dry-run 1: NVDA is missing → pending INSERT
            buf1 = io.StringIO()
            with contextlib.redirect_stdout(buf1):
                rc_dry1 = run(dry_run=True, quiet=False)
            output1 = buf1.getvalue()
            assert "INSERT" in output1, (
                "Dry-run (before apply) must report a pending INSERT for NVDA"
            )

            # Apply: NVDA inserted for real
            rc_apply = run(dry_run=False, quiet=True)
            assert rc_apply == 0

            open_trades = load_sqlite_open_trades()
            assert "NVDA" in open_trades, "NVDA must be inserted after apply run"

            # Dry-run 2: NVDA already tracked → 0 changes
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                rc_dry2 = run(dry_run=True, quiet=False)
            output2 = buf2.getvalue()

        # After apply, a second dry-run should show "0 changes"
        assert "0 changes" in output2, (
            f"After apply, dry-run must report '0 changes' (idempotent). "
            f"Got: {output2!r}"
        )
