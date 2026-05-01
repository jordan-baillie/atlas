"""Regression test for Bug A — missing strategies in DEFAULT_STRATEGIES.

Validated-strategies audit 2026-05-01 found connors_rsi2 and short_term_mr
were missing from research/autoresearch_nightly.DEFAULT_STRATEGIES, so the
nightly sweep never refreshed their research_best rows.
"""
import pytest


def test_connors_rsi2_in_default_strategies():
    from research.autoresearch_nightly import DEFAULT_STRATEGIES
    assert "connors_rsi2" in DEFAULT_STRATEGIES, (
        "Bug A regression: connors_rsi2 missing from nightly sweep"
    )


def test_short_term_mr_in_default_strategies():
    from research.autoresearch_nightly import DEFAULT_STRATEGIES
    assert "short_term_mr" in DEFAULT_STRATEGIES, (
        "Bug A regression: short_term_mr missing from nightly sweep"
    )


def test_no_default_strategy_orphaned_from_registry():
    """Every name in DEFAULT_STRATEGIES must exist in strategy_evaluator's registry."""
    from research.autoresearch_nightly import DEFAULT_STRATEGIES
    from scripts.strategy_evaluator import STRATEGY_REGISTRY
    for s in DEFAULT_STRATEGIES:
        assert s in STRATEGY_REGISTRY, f"{s} in DEFAULT_STRATEGIES but not in STRATEGY_REGISTRY"
