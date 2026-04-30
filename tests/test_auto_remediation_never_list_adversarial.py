"""Adversarial NEVER-list sweep — Phase 3 hardening verification.

Proves empirically that every NEVER-listed path is UNWRITEABLE at the
multi-team domain enforcement layer (OS-tool layer) or at deny.yaml
gate-time. Defense-in-depth: either layer alone is sufficient, but both
catching the path is the gold standard.

Three layers tested per path:
  Layer 1 — deny.yaml file_globs:
      config/auto_fix_deny.yaml file_globs list, using the same
      _glob_match() implementation from core/triage.py (fnmatch-based).
      A match → triage returns ESCALATE before whitelist check.

  Layer 2 — Fix Worker domain.write (structural absence):
      /root/.pi/teams/config.yaml → teams.remediation.members[Fix Worker]
      .domain.write glob list. If the path does NOT match any write glob,
      the multi-team OS layer physically blocks the tool write.
      If the path DOES match domain.write, deny.yaml MUST catch it (depth).

  Layer 3 — Triage classifier (TriageClassifier.classify):
      End-to-end validation: pass an error with this file_path through the
      real classifier; assert classification == "ESCALATE" (never AUTO_FIX).

Known nuance: root-level files (e.g. kill_switch.py, halt.py) do NOT
match `**/kill_switch*.py` via this fnmatch implementation — `*/X` requires
a leading `/` but root-level paths have none. These files are still fully
blocked because (a) domain.write has no root-level `*.py` glob and (b)
triage default-deny fires. This is documented via tier=99 vs tier=0 in
Layer 3 results. Not a safety gap.

Runs cleanly with: pytest tests/test_auto_remediation_never_list_adversarial.py -xvs
"""
from __future__ import annotations

import fnmatch
import sys
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.triage import TriageClassifier  # noqa: E402

# ---------------------------------------------------------------------------
# NEVER paths — every path here MUST be blocked by at least one layer.
# ---------------------------------------------------------------------------

NEVER_PATHS_TO_VERIFY: list[str] = [
    # ── Trading-path code (domain.write structurally absent) ──────────────
    "brokers/alpaca/broker.py",
    "brokers/live_executor.py",
    "risk/budget_check.py",
    "regime/detector.py",
    "signals/momentum.py",
    "portfolio/allocator.py",
    "overlay/engine.py",
    "strategies/momentum_breakout.py",
    # ── Trading-path scripts (in domain.write scripts/** BUT in deny.yaml) ─
    "scripts/eod_settlement.py",
    "scripts/intraday_monitor.py",
    "scripts/sync_protective_orders.py",
    "scripts/execute_approved.py",
    "scripts/director_cron.py",
    "scripts/reconcile_ledger.py",
    "scripts/auto_reoptimize.py",
    "scripts/cli.py",
    # ── Specific files (pattern-matched in deny.yaml) ──────────────────────
    "kill_switch.py",           # **/kill_switch*.py (fnmatch gap for root — see module docstring)
    "live_executor.py",         # **/live_executor*.py (same root-level note)
    "plans/foo.py",             # plans/**
    "approve/foo.py",           # approve/**
    "halt.py",                  # **/halt*.py (same root-level note)
    "core/reconcile.py",
    # ── Trading config + state ─────────────────────────────────────────────
    "config/active/sp500.json",
    "config/active_config.json",
    "config/global_risk.json",
    "config/research_priorities.json",      # NEW per Step 1
    "config/auto_remediation.yaml",         # recursive protection
    "config/auto_fix_classes.yaml",
    "config/auto_fix_deny.yaml",
    "config/safety_critical_functions.txt",
    # ── Research dual-writes (NEW per Step 1) ────────────────────────────
    "research/best/momentum_breakout.json",
    "research/brain/strategies/momentum_breakout.md",
    # ── Live state ────────────────────────────────────────────────────────
    "data/atlas.db",
    "data/HALT",
    ".live_halt",
    "data/state/foo.json",
    "brokers/state/live_sp500.json",
    # ── Telegram bot ─────────────────────────────────────────────────────
    "services/telegram_bot.py",
    # ── DB / migrations ───────────────────────────────────────────────────
    "db/atlas_db.py",
    "db/schema.sql",
    "db/migrations/001_init.py",
    "scripts/migrations/2026-04-30-test.py",
    # ── Secrets ───────────────────────────────────────────────────────────
    ".atlas-secrets.json",
    ".env",
    ".env.production",
]

# Legitimately fixable — must be in domain.write AND not in deny.yaml.
WHITELIST_PATHS: list[str] = [
    "tests/test_foo.py",
    "docs/runbook.md",
    "research/test_strategy.py",        # research/** — OK; not under research/best/ or brain/strategies/
    "utils/helpers.py",
    "scripts/some_new_script.py",       # scripts/** — OK; not matched by any specific deny glob
]

# Root-level files whose **/X*.py deny.yaml glob has a known fnmatch limitation.
# These are blocked by structural absence (not in domain.write); the deny.yaml
# tier-0 NEVER list cannot fire because fnmatch.fnmatch("halt.py", "*/halt*.py")
# is False (no leading slash). This is expected behaviour — see module docstring.
ROOT_LEVEL_DENY_GAP: frozenset[str] = frozenset(["kill_switch.py", "live_executor.py", "halt.py"])


# ---------------------------------------------------------------------------
# Shared glob-match helper — mirrors TriageClassifier._glob_match exactly.
# ---------------------------------------------------------------------------

def _glob_match(path: str | None, glob: str) -> bool:
    """Replicated 1-for-1 from core.triage.TriageClassifier._glob_match."""
    if not path:
        return False
    if "**" in glob:
        prefix = glob.split("**")[0].rstrip("/")
        if prefix and not path.startswith(prefix):
            return False
        return fnmatch.fnmatch(path, glob.replace("**", "*"))
    return fnmatch.fnmatch(path, glob)


def _first_matching_glob(path: str, globs: List[str]) -> str | None:
    """Return the first glob pattern that matches *path*, or None."""
    return next((g for g in globs if _glob_match(path, g)), None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class TestOSLayerEnforcement:
    """Parametrized sweep over all NEVER-listed paths — 3-layer defense check."""

    @pytest.fixture(scope="class")
    def deny_globs(self) -> List[str]:
        deny_path = PROJECT_ROOT / "config" / "auto_fix_deny.yaml"
        with open(deny_path) as f:
            cfg = yaml.safe_load(f)
        globs = cfg["file_globs"]
        assert isinstance(globs, list), "deny.yaml file_globs must be a list"
        assert len(globs) > 30, "deny.yaml seems truncated — expected >30 entries"
        return globs

    @pytest.fixture(scope="class")
    def fix_worker_domain_write(self) -> List[str]:
        config_path = Path("/root/.pi/teams/config.yaml")
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        members: list[dict] = cfg["teams"]["remediation"]["members"]
        fix_worker = next(
            (m for m in members if m.get("name") == "Fix Worker"),
            None,
        )
        assert fix_worker is not None, "Fix Worker not found in teams.remediation.members"
        domain_write: list[str] = fix_worker["domain"]["write"]
        assert isinstance(domain_write, list), "Fix Worker domain.write must be a list"
        assert len(domain_write) > 10, "Fix Worker domain.write seems too short — expected >10 entries"
        return domain_write

    @pytest.fixture(scope="class")
    def classifier(self) -> TriageClassifier:
        """Real TriageClassifier instantiated from production config files."""
        return TriageClassifier()

    # ── Layer 1 + 2: defense-in-depth structural check ──────────────────────

    @pytest.mark.parametrize("path", NEVER_PATHS_TO_VERIFY)
    def test_never_path_is_blocked_layers_1_and_2(
        self,
        path: str,
        deny_globs: List[str],
        fix_worker_domain_write: List[str],
    ) -> None:
        """Each NEVER path must be blocked by domain.write absence OR deny.yaml match.

        Rule:
          - If path is NOT in domain.write (structural absence) → BLOCKED. ✅
          - If path IS in domain.write → deny.yaml MUST catch it (depth). ✅
          - If path IS in domain.write AND is NOT in deny.yaml → CRITICAL GAP ❌
        """
        deny_glob_hit = _first_matching_glob(path, deny_globs)
        deny_match = deny_glob_hit is not None

        domain_glob_hit = _first_matching_glob(path, fix_worker_domain_write)
        domain_match = domain_glob_hit is not None

        # Safety predicate: at least one layer blocks this path.
        # blocked = True if structurally absent OR caught by deny.yaml gate.
        blocked = (not domain_match) or deny_match

        # Diagnostics — always print regardless of pass/fail.
        if not domain_match:
            layer = "domain.write structural absence"
            if deny_match:
                print(
                    f"\n  ✅ {path}:\n"
                    f"     Layer 2 (domain.write): BLOCKED — not in any write glob\n"
                    f"     Layer 1 (deny.yaml):    also caught by '{deny_glob_hit}' (defense-in-depth)"
                )
            else:
                is_root_gap = path in ROOT_LEVEL_DENY_GAP
                note = (
                    " [known fnmatch root-level gap for **/X*.py — see module docstring]"
                    if is_root_gap
                    else ""
                )
                print(
                    f"\n  ✅ {path}:\n"
                    f"     Layer 2 (domain.write): BLOCKED — not in any write glob\n"
                    f"     Layer 1 (deny.yaml):    no match{note}"
                )
        elif deny_match:
            print(
                f"\n  ✅ {path}:\n"
                f"     Layer 2 (domain.write): IN scope via '{domain_glob_hit}'\n"
                f"     Layer 1 (deny.yaml):    BLOCKED by '{deny_glob_hit}' (defense-in-depth ✓)"
            )
        else:
            print(
                f"\n  🚨 CRITICAL SAFETY GAP: {path}\n"
                f"     Layer 2 (domain.write): IN scope via '{domain_glob_hit}' — Fix Worker CAN write it\n"
                f"     Layer 1 (deny.yaml):    NO MATCH — deny.yaml does NOT block it\n"
                f"     Fix Worker + triage whitelist together could AUTO_FIX this path!"
            )

        assert blocked, (
            f"CRITICAL SAFETY GAP: '{path}' matches Fix Worker domain.write glob "
            f"'{domain_glob_hit}' but is NOT blocked by any deny.yaml file_glob. "
            f"A hallucinating Fix Worker could autonomously modify this path. "
            f"Add it to config/auto_fix_deny.yaml immediately."
        )

    # ── Layer 3: triage classifier must return ESCALATE ─────────────────────

    @pytest.mark.parametrize("path", NEVER_PATHS_TO_VERIFY)
    def test_never_path_layer3_triage_escalates(
        self,
        path: str,
        classifier: TriageClassifier,
    ) -> None:
        """Triage classifier must return ESCALATE (not AUTO_FIX) for every NEVER path.

        Uses a broad error that would match many whitelist classes (AttributeError +
        SyntaxError patterns) to stress-test that deny.yaml or default-deny fires
        before any AUTO_FIX whitelist match can be reached.

        Root-level files (kill_switch.py, live_executor.py, halt.py) return
        ESCALATE via default_deny (tier=99) rather than the NEVER list (tier=0)
        because the **/X*.py fnmatch pattern cannot match root-level filenames.
        This is expected and documented — structural absence + default-deny provide
        the correct protection.
        """
        error = {
            "file_path": path,
            "message": "AttributeError: SyntaxError ruff test error in test file",
            "exc_type": "AttributeError",
            "function_name": None,
            "traceback": f"Traceback: {path}",
        }

        with (
            patch.object(TriageClassifier, "is_halt_active", return_value=False),
            patch.object(TriageClassifier, "is_market_hours_now", return_value=False),
        ):
            result = classifier.classify(error)

        is_escalate = result.classification == "ESCALATE"
        via_never_list = result.tier == 0
        via_default_deny = result.rule_id == "default_deny"

        if via_never_list:
            print(
                f"\n  ✅ {path}: ESCALATE via NEVER list (tier=0, rule_id={result.rule_id})"
            )
        elif via_default_deny:
            is_expected_gap = path in ROOT_LEVEL_DENY_GAP
            note = " [known root-level fnmatch gap — blocked by structural absence]" if is_expected_gap else ""
            print(
                f"\n  ✅ {path}: ESCALATE via default_deny (tier=99){note}"
            )
        else:
            print(
                f"\n  {'✅' if is_escalate else '🚨'} {path}: "
                f"{result.classification} | tier={result.tier} | rule_id={result.rule_id} | "
                f"reason={result.reason}"
            )

        assert result.classification == "ESCALATE", (
            f"SAFETY FAILURE: '{path}' classified as '{result.classification}' "
            f"(tier={result.tier}, rule_id={result.rule_id}) — expected ESCALATE. "
            f"Reason: {result.reason}"
        )

    # ── Positive test: whitelist paths ARE writeable ─────────────────────────

    @pytest.mark.parametrize("path", WHITELIST_PATHS)
    def test_whitelist_path_is_writeable(
        self,
        path: str,
        deny_globs: List[str],
        fix_worker_domain_write: List[str],
    ) -> None:
        """Legitimately fixable paths must be in domain.write AND not in deny.yaml.

        This is the positive counterpart to test_never_path_is_blocked_layers_1_and_2.
        If this test fails, the NEVER list is over-broad — a legitimate fix target
        has been accidentally blocked.
        """
        deny_glob_hit = _first_matching_glob(path, deny_globs)
        domain_glob_hit = _first_matching_glob(path, fix_worker_domain_write)

        in_domain = domain_glob_hit is not None
        not_in_deny = deny_glob_hit is None

        print(
            f"\n  {'✅' if (in_domain and not_in_deny) else '❌'} {path}:\n"
            f"     domain.write: {'IN (' + domain_glob_hit + ')' if in_domain else 'ABSENT ← blocked'}\n"
            f"     deny.yaml:    {'BLOCKED by ' + deny_glob_hit + ' ← over-broad deny' if deny_glob_hit else 'not matched ✓'}"
        )

        assert in_domain, (
            f"Whitelist path '{path}' is NOT in Fix Worker domain.write — "
            f"Fix Worker cannot write it even for legitimate fixes. "
            f"Add an appropriate glob to domain.write, or remove from WHITELIST_PATHS."
        )
        assert not_in_deny, (
            f"Whitelist path '{path}' matched deny.yaml glob '{deny_glob_hit}' — "
            f"deny.yaml is over-broad and blocks a legitimate fix target. "
            f"Review and narrow the deny.yaml pattern."
        )


# ---------------------------------------------------------------------------
# Module-level summary test — single test that prints a complete report.
# ---------------------------------------------------------------------------


class TestNeverListSummaryReport:
    """Prints a structured summary of all layer results.

    This is a single test (not parametrized) that gives a full tabular view
    for easy audit. Fails if ANY path has a gap.
    """

    def test_full_layer_sweep_report(self) -> None:
        """Full sweep: load configs, check all 3 layers, print report, fail on any gap."""
        # Load configs
        deny_path = PROJECT_ROOT / "config" / "auto_fix_deny.yaml"
        with open(deny_path) as f:
            deny_cfg = yaml.safe_load(f)
        deny_globs: list[str] = deny_cfg["file_globs"]

        config_path = Path("/root/.pi/teams/config.yaml")
        with open(config_path) as f:
            teams_cfg = yaml.safe_load(f)
        members: list[dict] = teams_cfg["teams"]["remediation"]["members"]
        fix_worker = next(m for m in members if m.get("name") == "Fix Worker")
        domain_write: list[str] = fix_worker["domain"]["write"]

        classifier = TriageClassifier()

        gaps: list[str] = []
        structural_absence_count = 0
        deny_only_count = 0
        both_layers_count = 0
        root_level_gap_count = 0

        print("\n" + "=" * 80)
        print("NEVER-LIST OS-LAYER ENFORCEMENT SWEEP REPORT")
        print("=" * 80)
        print(f"{'PATH':<50} {'LAYER 1 (deny)':<8} {'LAYER 2 (domain)':<10} {'LAYER 3 (triage)':<20} STATUS")
        print("-" * 100)

        for path in NEVER_PATHS_TO_VERIFY:
            deny_hit = _first_matching_glob(path, deny_globs)
            domain_hit = _first_matching_glob(path, domain_write)
            deny_match = deny_hit is not None
            domain_match = domain_hit is not None

            # Triage layer
            error = {
                "file_path": path,
                "message": "AttributeError: SyntaxError ruff test",
                "exc_type": "AttributeError",
                "function_name": None,
                "traceback": f"File {path}",
            }
            with (
                patch.object(TriageClassifier, "is_halt_active", return_value=False),
                patch.object(TriageClassifier, "is_market_hours_now", return_value=False),
            ):
                triage_result = classifier.classify(error)

            triage_ok = triage_result.classification == "ESCALATE"
            blocked = (not domain_match) or deny_match

            if not blocked:
                status = "🚨 CRITICAL GAP"
                gaps.append(path)
            elif not domain_match and deny_match:
                status = "✅ BOTH layers"
                both_layers_count += 1
            elif not domain_match and not deny_match:
                is_root_gap = path in ROOT_LEVEL_DENY_GAP
                status = "✅ absent (root-level fnmatch gap)" if is_root_gap else "✅ absent"
                structural_absence_count += 1
                if is_root_gap:
                    root_level_gap_count += 1
            else:
                # domain_match + deny_match
                status = "✅ deny.yaml depth"
                both_layers_count += 1

            triage_tier = f"tier={triage_result.tier}" if triage_ok else f"❌ {triage_result.classification}"

            deny_col = f"✅ {deny_hit[:20]}" if deny_match else "— no match"
            domain_col = "absent" if not domain_match else f"in {domain_hit[:15]}"
            print(f"{path:<50} {deny_col:<30} {domain_col:<20} {triage_tier:<20} {status}")

        print("-" * 100)
        print(f"\nSUMMARY:")
        print(f"  Total paths tested:          {len(NEVER_PATHS_TO_VERIFY)}")
        print(f"  Blocked by structural absence: {structural_absence_count}")
        print(f"    └─ with deny.yaml also:      {both_layers_count} (defense-in-depth)")
        print(f"    └─ root-level fnmatch gap:   {root_level_gap_count} (protected by absence + default-deny)")
        print(f"  Critical gaps found:          {len(gaps)}")
        if gaps:
            print(f"\n🚨 CRITICAL GAPS (require immediate fix):")
            for g in gaps:
                print(f"    - {g}")
        print("=" * 80 + "\n")

        assert not gaps, (
            f"CRITICAL SAFETY GAPS detected — {len(gaps)} NEVER-listed path(s) are BOTH "
            f"in Fix Worker domain.write AND NOT blocked by deny.yaml:\n"
            + "\n".join(f"  - {g}" for g in gaps)
            + "\n\nAdd these paths to config/auto_fix_deny.yaml immediately."
        )
