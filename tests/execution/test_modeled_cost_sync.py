"""Drift guard: atlas's MODELED_COST_BPS must stay in sync with crucible's canonical table.

The G6 slippage bar = SLIPPAGE_MULT × MODELED_COST_BPS[book]. atlas and crucible each compute
G6 independently (separate processes/repos) and atlas cannot import crucible, so the per-book
modeled cost is necessarily DUPLICATED. A silent divergence would let the two systems disagree on
whether a book is live-eligible. This test fails loudly on any drift.

crucible is the canonical source of truth; if it is not present on disk (CI without the sibling
repo) the cross-check is skipped, but the SLIPPAGE_MULT invariant is still asserted.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from atlas.execution import gates

CRUCIBLE_EVIDENCE = Path("/root/crucible/forward/evidence.py")


def _parse_literal(path: Path, name: str):
    """Extract a module-level literal assignment (MODELED_COST_BPS / SLIPPAGE_MULT) by AST,
    without importing crucible (which is not on atlas's path)."""
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path}")


def test_slippage_mult_matches_canonical():
    if not CRUCIBLE_EVIDENCE.exists():
        pytest.skip("crucible not present on disk")
    assert gates.SLIPPAGE_MULT == _parse_literal(CRUCIBLE_EVIDENCE, "SLIPPAGE_MULT")


def test_modeled_cost_table_matches_canonical():
    if not CRUCIBLE_EVIDENCE.exists():
        pytest.skip("crucible not present on disk")
    canonical = _parse_literal(CRUCIBLE_EVIDENCE, "MODELED_COST_BPS")
    # Every book atlas scores must use the SAME modeled cost crucible uses — no drift.
    for book, cost in gates.MODELED_COST_BPS.items():
        assert book in canonical, f"{book} registered in atlas but not in crucible (canonical)"
        assert cost == canonical[book], (
            f"modeled-cost DRIFT for {book}: atlas={cost} vs crucible={canonical[book]}")
    # And vice-versa: crucible must not register a book atlas silently omits.
    for book in canonical:
        assert book in gates.MODELED_COST_BPS, (
            f"{book} registered in crucible (canonical) but missing from atlas gates.py")


def test_amihud_registered_conservative_leg():
    """amihud's asymmetric design (long 30/side, short 7.5/side) is registered at the CONSERVATIVE
    tightest leg so a single fill-based bar cannot false-PASS the cheap leg (frozen-spec faithful)."""
    assert gates.MODELED_COST_BPS["amihud_illiq_tranched_v3"] == 7.5
