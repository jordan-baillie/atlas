"""Tests for per-strategy paper/live routing in scripts/execute_approved.py.

Phase B: execute_approved.py now splits plan entries/exits by lifecycle state
when universe mode is "live", routing PAPER-state strategies to the paper
broker executor and all others to the live broker executor.

Coverage:
    1. passive mode → no executor instantiated
    2. live mode + no paper strategies → 1 live executor call
    3. live mode + mixed lifecycle → 2 executor calls (correct split)
    4. paper mode (universe-level) → 1 paper executor, all entries
    5. only paper entries → only paper executor, no live call
    6. only live entries → only live executor, no paper call
    7. split preserves order within each side
    8. exit entries without strategy field → route to live (safe default)
    9. import failure in _split_by_lifecycle → all entries fallback to live

Patch strategy:
    get_active_config, TradePlanGenerator, LiveExecutor are all lazy-imported
    inside functions in execute_approved.py.  We must patch at their SOURCE
    modules, NOT at "scripts.execute_approved.XYZ" (which aren't bound at
    module level).

    _run_executor and _is_market_halted ARE module-level functions, so they
    can be patched via patch.object(mod, "...").
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import scripts.execute_approved as mod

from brokers.routing_policy import BrokerRoutingPolicy


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_config(mode: str, live_enabled: bool = True) -> dict:
    """Build a minimal config dict. live_enabled=True for non-passive modes."""
    return {"trading": {"mode": mode, "live_enabled": live_enabled, "auto_approve": False}}


def _make_entry(ticker: str, strategy: str = "momentum_breakout") -> dict:
    return {"ticker": ticker, "strategy": strategy, "position_size": 10}


def _make_exit(ticker: str) -> dict:
    """Exits typically don't carry a strategy field."""
    return {"ticker": ticker, "reason": "signal_exit", "exit_price": 100.0}


def _approved_plan(entries: list, exits: list | None = None) -> dict:
    return {
        "status": "APPROVED",
        "proposed_entries": entries,
        "proposed_exits": exits or [],
        "overlay_context": {},
    }


def _mock_plan_gen(plan: dict) -> MagicMock:
    pg = MagicMock()
    pg.return_value.load_plan.return_value = plan
    pg.return_value.approve_plan.return_value = plan
    return pg


# ── Common patch targets ──────────────────────────────────────────────────────
# get_active_config → imported inside main() via "from utils.config import ..."
_PATCH_CONFIG = "utils.config.get_active_config"
# TradePlanGenerator → imported inside main() via "from brokers.plan import ..."
_PATCH_PLAN_GEN = "brokers.plan.TradePlanGenerator"
# LiveExecutor → imported inside _run_executor() via "from brokers.live_executor import ..."
_PATCH_EXECUTOR = "brokers.live_executor.LiveExecutor"
# is_paper → imported inside _split_by_lifecycle() via "from monitor.strategy_lifecycle import ..."
_PATCH_IS_PAPER = "monitor.strategy_lifecycle.is_paper"


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: passive mode skips entirely — no executor at all
# ─────────────────────────────────────────────────────────────────────────────

def test_passive_mode_skips_entirely():
    """Universe mode='passive' → main() returns immediately, no executor created."""
    with (
        patch.object(mod, "_is_market_halted", return_value=(False, "", "")),
        patch(_PATCH_CONFIG, return_value=_make_config("passive")),
        patch(_PATCH_PLAN_GEN) as mock_pg,
        patch(_PATCH_EXECUTOR) as mock_executor_cls,
    ):
        mock_pg.return_value.load_plan.return_value = _approved_plan(
            [_make_entry("AAPL")]
        )
        with patch("sys.argv", ["execute_approved.py", "--market", "sp500"]):
            mod.main()

    # No executor should have been constructed
    mock_executor_cls.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: live mode, no PAPER-state strategies → single live executor call
# ─────────────────────────────────────────────────────────────────────────────

def test_live_mode_with_no_paper_strategies_routes_all_live():
    """All strategies in LIVE/RESEARCH lifecycle → 1 executor with mode='live'."""
    entries = [_make_entry("AAPL", "momentum_breakout"), _make_entry("MSFT", "connors_rsi2")]
    plan = _approved_plan(entries)

    configs_seen: list[dict] = []

    mock_exec = MagicMock()
    mock_exec.connect.return_value = True
    mock_exec.execute_plan.return_value = {
        "successful_entries": 2, "successful_exits": 0,
        "total_entries": 2, "total_exits": 0,
    }

    def fake_executor_cls(cfg):
        configs_seen.append(cfg)
        return mock_exec

    with (
        patch.object(mod, "_is_market_halted", return_value=(False, "", "")),
        patch.object(mod, "_notify_execution"),
        patch(_PATCH_CONFIG, return_value=_make_config("live")),
        patch(_PATCH_PLAN_GEN) as mock_pg,
        patch(_PATCH_EXECUTOR, side_effect=fake_executor_cls),
        patch(_PATCH_IS_PAPER, return_value=False),
    ):
        mock_pg.return_value.load_plan.return_value = plan
        with patch("sys.argv", ["execute_approved.py", "--market", "sp500", "--dry-run"]):
            mod.main()

    # Exactly one executor, mode=live
    assert mock_exec.connect.call_count == 1
    assert len(configs_seen) == 1
    assert configs_seen[0]["trading"]["mode"] == "live"

    # All 2 entries sent to live executor
    executed_plan = mock_exec.execute_plan.call_args[0][0]
    assert len(executed_plan["proposed_entries"]) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: live mode + mixed lifecycle → 2 executor calls with correct split
# ─────────────────────────────────────────────────────────────────────────────

def test_live_mode_splits_by_lifecycle():
    """Mixed LIVE + PAPER lifecycle → live executor gets LIVE entries, paper gets PAPER."""
    entries = [
        _make_entry("AAPL", "short_term_mr"),       # PAPER lifecycle
        _make_entry("MSFT", "momentum_breakout"),    # LIVE lifecycle
        _make_entry("NVDA", "connors_rsi2"),         # LIVE lifecycle
    ]
    plan = _approved_plan(entries)

    configs_seen: list[tuple[str, str]] = []
    entries_seen: list[list] = []

    def fake_run_executor(config, pl, entr, exts, market_id, trade_date, dry_run, label):
        configs_seen.append((label, config["trading"]["mode"]))
        entries_seen.append(entr)
        return {
            "successful_entries": len(entr), "successful_exits": 0,
            "total_entries": len(entr), "total_exits": 0,
        }

    def fake_is_paper(strategy, universe):
        return strategy == "short_term_mr"

    with (
        patch.object(mod, "_is_market_halted", return_value=(False, "", "")),
        patch.object(mod, "_notify_execution"),
        patch.object(mod, "_run_executor", side_effect=fake_run_executor),
        patch(_PATCH_CONFIG, return_value=_make_config("live")),
        patch(_PATCH_PLAN_GEN) as mock_pg,
        patch(_PATCH_IS_PAPER, side_effect=fake_is_paper),
    ):
        mock_pg.return_value.load_plan.return_value = plan
        with patch("sys.argv", ["execute_approved.py", "--market", "sp500", "--dry-run"]):
            mod.main()

    # Two executor calls
    assert len(configs_seen) == 2
    labels = {lbl for lbl, _ in configs_seen}
    assert "[live]" in labels
    assert "[paper]" in labels

    live_idx = next(i for i, (lbl, _) in enumerate(configs_seen) if lbl == "[live]")
    paper_idx = next(i for i, (lbl, _) in enumerate(configs_seen) if lbl == "[paper]")

    live_tickers = {e["ticker"] for e in entries_seen[live_idx]}
    paper_tickers = {e["ticker"] for e in entries_seen[paper_idx]}

    assert live_tickers == {"MSFT", "NVDA"}
    assert paper_tickers == {"AAPL"}

    # Paper executor received config with mode="paper"
    assert configs_seen[paper_idx][1] == "paper"
    assert configs_seen[live_idx][1] == "live"


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: universe mode="paper" → all entries routed to paper regardless of lifecycle
# ─────────────────────────────────────────────────────────────────────────────

def test_paper_mode_routes_all_paper_regardless_of_lifecycle():
    """Universe mode='paper' → 1 executor with mode='paper', all entries included."""
    entries = [
        _make_entry("AAPL", "short_term_mr"),       # would be PAPER lifecycle
        _make_entry("MSFT", "momentum_breakout"),    # would be LIVE lifecycle
    ]
    plan = _approved_plan(entries)

    configs_seen: list[tuple[str, str]] = []
    entries_seen: list[list] = []

    def fake_run_executor(config, pl, entr, exts, market_id, trade_date, dry_run, label):
        configs_seen.append((label, config["trading"]["mode"]))
        entries_seen.append(entr)
        return {
            "successful_entries": len(entr), "successful_exits": 0,
            "total_entries": len(entr), "total_exits": 0,
        }

    with (
        patch.object(mod, "_is_market_halted", return_value=(False, "", "")),
        patch.object(mod, "_notify_execution"),
        patch.object(mod, "_run_executor", side_effect=fake_run_executor),
        patch(_PATCH_CONFIG, return_value=_make_config("paper")),
        patch(_PATCH_PLAN_GEN) as mock_pg,
    ):
        mock_pg.return_value.load_plan.return_value = plan
        with patch("sys.argv", ["execute_approved.py", "--market", "sp500", "--dry-run"]):
            mod.main()

    # Exactly one executor, paper mode
    assert len(configs_seen) == 1
    label, mode = configs_seen[0]
    assert label == "[paper]"
    assert mode == "paper"

    # All 2 entries sent to the single paper executor
    assert len(entries_seen[0]) == 2
    assert {e["ticker"] for e in entries_seen[0]} == {"AAPL", "MSFT"}


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: only paper entries → only paper executor called, no live executor
# ─────────────────────────────────────────────────────────────────────────────

def test_only_paper_entries_no_live_call():
    """All entries are PAPER lifecycle → only paper executor called."""
    entries = [
        _make_entry("AAPL", "short_term_mr"),
        _make_entry("GOOG", "short_term_mr"),
    ]
    plan = _approved_plan(entries)
    executor_labels: list[str] = []

    def fake_run_executor(config, pl, entr, exts, market_id, trade_date, dry_run, label):
        executor_labels.append(label)
        return {
            "successful_entries": len(entr), "successful_exits": 0,
            "total_entries": len(entr), "total_exits": 0,
        }

    with (
        patch.object(mod, "_is_market_halted", return_value=(False, "", "")),
        patch.object(mod, "_notify_execution"),
        patch.object(mod, "_run_executor", side_effect=fake_run_executor),
        patch(_PATCH_CONFIG, return_value=_make_config("live")),
        patch(_PATCH_PLAN_GEN) as mock_pg,
        patch(_PATCH_IS_PAPER, return_value=True),
    ):
        mock_pg.return_value.load_plan.return_value = plan
        with patch("sys.argv", ["execute_approved.py", "--market", "sp500", "--dry-run"]):
            mod.main()

    assert "[paper]" in executor_labels
    assert "[live]" not in executor_labels


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: only live entries → only live executor called, no paper executor
# ─────────────────────────────────────────────────────────────────────────────

def test_only_live_entries_no_paper_call():
    """All entries are LIVE/RESEARCH lifecycle → only live executor called."""
    entries = [
        _make_entry("AAPL", "momentum_breakout"),
        _make_entry("MSFT", "connors_rsi2"),
    ]
    plan = _approved_plan(entries)
    executor_labels: list[str] = []

    def fake_run_executor(config, pl, entr, exts, market_id, trade_date, dry_run, label):
        executor_labels.append(label)
        return {
            "successful_entries": len(entr), "successful_exits": 0,
            "total_entries": len(entr), "total_exits": 0,
        }

    with (
        patch.object(mod, "_is_market_halted", return_value=(False, "", "")),
        patch.object(mod, "_notify_execution"),
        patch.object(mod, "_run_executor", side_effect=fake_run_executor),
        patch(_PATCH_CONFIG, return_value=_make_config("live")),
        patch(_PATCH_PLAN_GEN) as mock_pg,
        patch(_PATCH_IS_PAPER, return_value=False),
    ):
        mock_pg.return_value.load_plan.return_value = plan
        with patch("sys.argv", ["execute_approved.py", "--market", "sp500", "--dry-run"]):
            mod.main()

    assert "[live]" in executor_labels
    assert "[paper]" not in executor_labels


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: split preserves original ordering within each side
# ─────────────────────────────────────────────────────────────────────────────

def test_split_preserves_order():
    """_split_by_lifecycle preserves relative ordering within live and paper sides."""
    entries = [
        _make_entry("A", "short_term_mr"),     # PAPER
        _make_entry("B", "momentum_breakout"),  # LIVE
        _make_entry("C", "short_term_mr"),     # PAPER
        _make_entry("D", "connors_rsi2"),      # LIVE
        _make_entry("E", "short_term_mr"),     # PAPER
    ]

    def fake_is_paper(strategy, universe):
        return strategy == "short_term_mr"

    with patch(_PATCH_IS_PAPER, side_effect=fake_is_paper):
        live_es, paper_es = BrokerRoutingPolicy(_make_config("live"), "sp500").split_entries_by_lifecycle(entries)

    assert [e["ticker"] for e in live_es] == ["B", "D"]
    assert [e["ticker"] for e in paper_es] == ["A", "C", "E"]


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: exit entries missing strategy field → route to live (safe default)
# ─────────────────────────────────────────────────────────────────────────────

def test_split_exits_without_strategy_go_to_live():
    """Exit entries that lack a 'strategy' field should route to live (safe default)."""
    exits = [_make_exit("AAPL"), _make_exit("MSFT")]

    # Even if is_paper would return True for a non-empty strategy,
    # empty strategy string skips the lifecycle check entirely
    with patch(_PATCH_IS_PAPER, return_value=True):
        live_es, paper_es = BrokerRoutingPolicy(_make_config("live"), "sp500").split_entries_by_lifecycle(exits)

    assert len(live_es) == 2
    assert len(paper_es) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: import failure in _split_by_lifecycle → all entries fallback to live
# ─────────────────────────────────────────────────────────────────────────────

def test_split_by_lifecycle_import_failure_routes_all_to_live():
    """If strategy_lifecycle can't be imported, all entries fallback to live."""
    entries = [_make_entry("AAPL", "short_term_mr"), _make_entry("MSFT", "momentum_breakout")]

    # Simulate ImportError by replacing the module with None in sys.modules
    with patch.dict("sys.modules", {"monitor.strategy_lifecycle": None}):
        live_es, paper_es = BrokerRoutingPolicy(_make_config("live"), "sp500").split_entries_by_lifecycle(entries)

    assert len(live_es) == 2
    assert len(paper_es) == 0
