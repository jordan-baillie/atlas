"""Tests for regime-aware plan generation in brokers/plan.py.

All heavy dependencies (RegimeModel, build_multi_universe, PortfolioConstructor,
strategy execution, file I/O, SQLite) are mocked so the suite runs fast and
offline.

Test categories
---------------
* TestRegimeDisabled  — ``regime_enabled=False`` must behave identically to the
  original SP500-only flow and never touch the regime layer.
* TestRegimeEnabled   — ``regime_enabled=True`` exercises the full pipeline:
  RegimeModel → build_multi_universe → strategy filtering → signal tagging →
  PortfolioConstructor → plan enrichment.
* TestGracefulFallback — any exception in the regime pipeline falls back to
  SP500-only mode with a warning log.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from brokers.plan import TradePlanGenerator
from strategies.base import Signal


# ─────────────────────────────────────────────────────────────────────────────
# Test helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_config(regime_enabled: bool = False) -> dict:
    return {
        "market": "sp500",
        "version": "3.0-test",
        "risk": {
            "min_confidence": 0.0,
            "max_open_positions": 5,
            "max_daily_drawdown_pct": 0.1,
        },
        "regime_enabled": regime_enabled,
    }


def _make_portfolio(equity: float = 10_000.0) -> MagicMock:
    portfolio = MagicMock()
    portfolio.positions = []
    portfolio.atlas_positions = []
    portfolio.manual_positions = []
    portfolio.cash = equity * 0.5
    portfolio.equity.return_value = equity
    portfolio.check_risk_limits.return_value = (True, "")
    portfolio.check_daily_drawdown.return_value = (False, 0.0)
    portfolio.portfolio_summary.return_value = {
        "open_positions": [],
        "total_pnl": 0.0,
        "total_pnl_pct": 0.0,
    }
    return portfolio


def _make_signal(
    ticker: str = "AAPL",
    universe: str = "sp500",
    strategy: str = "momentum_breakout",
    confidence: float = 0.8,
) -> Signal:
    return Signal(
        ticker=ticker,
        strategy=strategy,
        direction="long",
        entry_price=100.0,
        stop_price=95.0,
        take_profit=115.0,
        position_size=10,
        position_value=1_000.0,
        risk_amount=50.0,
        confidence=confidence,
        rationale="Test signal",
        universe=universe,
    )


def _make_regime_classification(
    state: str = "bull_risk_on",
    universes: list = None,
    enabled_strategies: list = None,
    sizing: float = 1.0,
    max_positions: int = 5,
    date: str = "2026-01-01",
) -> MagicMock:
    """Build a MagicMock that quacks like RegimeClassification."""
    rc = MagicMock()
    rc.state = MagicMock()
    rc.state.value = state
    rc.active_universes = list(universes or ["sp500", "sector_etfs"])
    rc.enabled_strategies = list(enabled_strategies or ["all"])
    rc.sizing_multiplier = sizing
    rc.max_positions = max_positions
    rc.reasoning = f"Test reasoning for {state}"
    # Real string so brokers.plan's regime_gate strptime() succeeds.
    rc.date = date
    return rc


def _make_constructed_portfolio(signals: list = None) -> MagicMock:
    """Build a MagicMock that quacks like ConstructedPortfolio."""
    cp = MagicMock()
    cp.signals = list(signals or [])
    cp.rejected = []
    cp.reasoning = "test construction"
    cp.regime_state = "bull_risk_on"
    cp.sizing_multiplier = 1.0
    cp.total_positions = len(cp.signals)
    cp.universe_exposure = {}
    return cp


def _make_strategy(name: str, signals: list = None) -> MagicMock:
    strat = MagicMock()
    strat.name = name
    strat.generate_signals.return_value = list(signals or [])
    return strat


# Patch target constants — patch where the names are *used* (late imports inside
# brokers/plan.py methods resolve through the original module namespaces).
_PATCH_REGIME_MODEL     = "regime.model.RegimeModel"
_PATCH_BUILD_MULTI      = "universe.builder.build_multi_universe"
_PATCH_CONSTRUCTOR      = "portfolio.constructor.PortfolioConstructor"


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixture: suppress file-system and SQLite side-effects in generate_plan
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def patch_save_plan(monkeypatch):
    """Prevent every test from writing plan files to disk or hitting SQLite."""
    monkeypatch.setattr(TradePlanGenerator, "_save_plan", lambda self, plan, date: None)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: regime_enabled = False  (SP500-only path)
# ─────────────────────────────────────────────────────────────────────────────


class TestRegimeDisabled:
    """When regime_enabled is False, the regime layer must never be touched."""

    def test_does_not_instantiate_regime_model(self):
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=False))
        strat = _make_strategy("momentum_breakout")

        with patch(_PATCH_REGIME_MODEL) as mock_cls:
            gen.generate_regime_plan(
                strategies=[strat],
                prices={"AAPL": 100.0},
                trade_date="2026-01-01",
                equity=10_000.0,
                sp500_data={"AAPL": MagicMock()},
            )
            mock_cls.assert_not_called()

    def test_does_not_call_build_multi_universe(self):
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=False))
        strat = _make_strategy("momentum_breakout")

        with patch(_PATCH_BUILD_MULTI) as mock_build:
            gen.generate_regime_plan(
                strategies=[strat],
                prices={},
                trade_date="2026-01-01",
                equity=10_000.0,
                sp500_data={},
            )
            mock_build.assert_not_called()

    def test_runs_strategies_on_sp500_data(self):
        """Strategies must be called with the exact sp500_data dict."""
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=False))
        strat = _make_strategy("momentum_breakout")
        sp500_data = {"AAPL": MagicMock(), "MSFT": MagicMock()}

        gen.generate_regime_plan(
            strategies=[strat],
            prices={"AAPL": 100.0},
            trade_date="2026-01-01",
            equity=10_000.0,
            sp500_data=sp500_data,
        )

        strat.generate_signals.assert_called_once_with(sp500_data, 10_000.0, [])

    def test_returns_valid_plan_dict(self):
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=False))

        plan = gen.generate_regime_plan(
            strategies=[],
            prices={},
            trade_date="2026-01-15",
            equity=10_000.0,
        )

        assert plan["trade_date"] == "2026-01-15"
        assert plan["status"] == "PENDING_APPROVAL"
        assert "proposed_entries" in plan
        assert "risk_summary" in plan

    def test_no_regime_metadata_in_plan(self):
        """The plan must NOT contain regime fields when regime is disabled."""
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=False))

        plan = gen.generate_regime_plan(
            strategies=[],
            prices={},
            trade_date="2026-01-01",
            equity=10_000.0,
        )

        assert "regime_state" not in plan
        assert "active_universes" not in plan
        assert "sizing_multiplier" not in plan
        assert "regime_reasoning" not in plan

    def test_multiple_strategies_all_run(self):
        """All strategies are invoked when regime is disabled."""
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=False))
        strats = [
            _make_strategy("momentum_breakout"),
            _make_strategy("mean_reversion"),
            _make_strategy("trend_following"),
        ]
        sp500_data = {}

        gen.generate_regime_plan(
            strategies=strats,
            prices={},
            trade_date="2026-01-01",
            equity=10_000.0,
            sp500_data=sp500_data,
        )

        for s in strats:
            s.generate_signals.assert_called_once()

    def test_default_regime_enabled_is_false(self):
        """Config without regime_enabled key defaults to False (no regime layer)."""
        config = _make_config()
        del config["regime_enabled"]  # simulate key absence
        gen = TradePlanGenerator(_make_portfolio(), config)

        with patch(_PATCH_REGIME_MODEL) as mock_cls:
            gen.generate_regime_plan(
                strategies=[],
                prices={},
                trade_date="2026-01-01",
                equity=10_000.0,
            )
            mock_cls.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: regime_enabled = True  (regime-aware path)
# ─────────────────────────────────────────────────────────────────────────────


class TestRegimeEnabled:
    """Full regime-aware pipeline tests."""

    # ── helper to build standard patch stack ─────────────────────────────────

    def _regime_patches(self, regime_result, multi_data=None, constructed=None):
        """Return a context-manager triple for the three heavy imports."""
        from contextlib import ExitStack
        import contextlib

        multi_data = multi_data if multi_data is not None else {}
        constructed = constructed or _make_constructed_portfolio()

        stack = ExitStack()
        mock_model_cls  = stack.enter_context(patch(_PATCH_REGIME_MODEL))
        mock_build      = stack.enter_context(patch(_PATCH_BUILD_MULTI, return_value=multi_data))
        mock_ctor_cls   = stack.enter_context(patch(_PATCH_CONSTRUCTOR))

        mock_model_instance = MagicMock()
        mock_model_instance.classify_current.return_value = regime_result
        mock_model_instance.classify_and_record.return_value = regime_result
        mock_model_cls.return_value = mock_model_instance

        mock_ctor_cls.return_value.construct.return_value = constructed

        return stack, mock_model_cls, mock_build, mock_ctor_cls, mock_model_instance

    # ── individual tests ──────────────────────────────────────────────────────

    def test_calls_classify_current(self):
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=True))
        regime = _make_regime_classification()

        with patch(_PATCH_REGIME_MODEL) as mock_cls, \
             patch(_PATCH_BUILD_MULTI, return_value={}), \
             patch(_PATCH_CONSTRUCTOR) as mock_ctor:

            inst = MagicMock()
            inst.classify_current.return_value = regime
            inst.classify_and_record.return_value = regime
            mock_cls.return_value = inst
            mock_ctor.return_value.construct.return_value = _make_constructed_portfolio()

            gen.generate_regime_plan(
                strategies=[],
                prices={},
                trade_date="2026-01-01",
                equity=10_000.0,
            )

            # classify_current() is called at least once.  In production it
            # is invoked by both the regime_gate staleness check and the
            # downstream _run_regime_aware_plan path; this test only cares
            # that the model is consulted, not the exact call count.
            assert inst.classify_current.call_count >= 1

    def test_active_universes_passed_to_build_multi_universe(self):
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=True))
        universes = ["treasury_etfs", "gold_etfs", "defensive_etfs"]
        regime = _make_regime_classification(universes=universes)

        with patch(_PATCH_REGIME_MODEL) as mock_cls, \
             patch(_PATCH_BUILD_MULTI) as mock_build, \
             patch(_PATCH_CONSTRUCTOR) as mock_ctor:

            inst = MagicMock()
            inst.classify_current.return_value = regime
            inst.classify_and_record.return_value = regime
            mock_cls.return_value = inst
            mock_build.return_value = {}
            mock_ctor.return_value.construct.return_value = _make_constructed_portfolio()

            gen.generate_regime_plan(
                strategies=[],
                prices={},
                trade_date="2026-01-01",
                equity=10_000.0,
            )

            mock_build.assert_called_once_with(universes)

    def test_strategy_filtering_respects_regime_types(self):
        """Only strategies whose name appears in enabled_strategies are invoked."""
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=True))
        regime = _make_regime_classification(
            universes=["sp500"],
            enabled_strategies=["mean_reversion"],
        )
        mr_strat = _make_strategy("mean_reversion")
        tf_strat = _make_strategy("trend_following")
        universe_data = {"AAPL": MagicMock()}

        with patch(_PATCH_REGIME_MODEL) as mock_cls, \
             patch(_PATCH_BUILD_MULTI, return_value={"sp500": universe_data}), \
             patch(_PATCH_CONSTRUCTOR) as mock_ctor:

            inst = MagicMock()
            inst.classify_current.return_value = regime
            inst.classify_and_record.return_value = regime
            mock_cls.return_value = inst
            mock_ctor.return_value.construct.return_value = _make_constructed_portfolio()

            gen.generate_regime_plan(
                strategies=[mr_strat, tf_strat],
                prices={},
                trade_date="2026-01-01",
                equity=10_000.0,
            )

        mr_strat.generate_signals.assert_called_once()
        tf_strat.generate_signals.assert_not_called()

    def test_strategy_filtering_all_runs_all_strategies(self):
        """When enabled_strategies=['all'], every strategy runs."""
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=True))
        regime = _make_regime_classification(
            universes=["sp500"],
            enabled_strategies=["all"],
        )
        strats = [
            _make_strategy("momentum_breakout"),
            _make_strategy("mean_reversion"),
            _make_strategy("trend_following"),
            _make_strategy("connors_rsi2"),
        ]

        with patch(_PATCH_REGIME_MODEL) as mock_cls, \
             patch(_PATCH_BUILD_MULTI, return_value={"sp500": {}}), \
             patch(_PATCH_CONSTRUCTOR) as mock_ctor:

            inst = MagicMock()
            inst.classify_current.return_value = regime
            inst.classify_and_record.return_value = regime
            mock_cls.return_value = inst
            mock_ctor.return_value.construct.return_value = _make_constructed_portfolio()

            gen.generate_regime_plan(
                strategies=strats,
                prices={},
                trade_date="2026-01-01",
                equity=10_000.0,
            )

        for s in strats:
            s.generate_signals.assert_called_once()

    def test_signals_tagged_with_universe_name(self):
        """Every signal emitted by a strategy is tagged with the originating universe."""
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=True))
        regime = _make_regime_classification(universes=["sector_etfs"])

        raw_sig = _make_signal(ticker="XLK", universe="sp500")  # starts with wrong universe
        strat = _make_strategy("momentum_breakout", signals=[raw_sig])

        captured: list = []

        def capture_construct(signals, equity, existing_positions):
            captured.extend(signals)
            return _make_constructed_portfolio(signals=signals)

        with patch(_PATCH_REGIME_MODEL) as mock_cls, \
             patch(_PATCH_BUILD_MULTI, return_value={"sector_etfs": {"XLK": MagicMock()}}), \
             patch(_PATCH_CONSTRUCTOR) as mock_ctor:

            inst = MagicMock()
            inst.classify_current.return_value = regime
            inst.classify_and_record.return_value = regime
            mock_cls.return_value = inst
            mock_ctor.return_value.construct.side_effect = capture_construct

            gen.generate_regime_plan(
                strategies=[strat],
                prices={},
                trade_date="2026-01-01",
                equity=10_000.0,
            )

        assert len(captured) == 1, "Expected exactly one signal to reach the constructor"
        assert captured[0].universe == "sector_etfs", (
            f"Expected universe='sector_etfs', got '{captured[0].universe}'"
        )

    def test_signals_tagged_across_multiple_universes(self):
        """Signals from different universes get the correct universe tag each."""
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=True))
        regime = _make_regime_classification(universes=["sp500", "gold_etfs"])

        sig_sp500 = _make_signal("AAPL", "sp500")
        sig_gold  = _make_signal("GLD",  "sp500")  # wrong universe initially

        strat = _make_strategy("trend_following")
        # Return different signals depending on which universe data is passed
        sp500_data = {"AAPL": MagicMock()}
        gold_data  = {"GLD":  MagicMock()}

        def generate_by_data(data, equity, existing):
            if "AAPL" in data:
                return [sig_sp500]
            if "GLD" in data:
                return [sig_gold]
            return []

        strat.generate_signals.side_effect = generate_by_data

        captured: list = []

        def capture_construct(signals, equity, existing_positions):
            captured.extend(signals)
            return _make_constructed_portfolio(signals=signals)

        with patch(_PATCH_REGIME_MODEL) as mock_cls, \
             patch(_PATCH_BUILD_MULTI, return_value={"sp500": sp500_data, "gold_etfs": gold_data}), \
             patch(_PATCH_CONSTRUCTOR) as mock_ctor:

            inst = MagicMock()
            inst.classify_current.return_value = regime
            inst.classify_and_record.return_value = regime
            mock_cls.return_value = inst
            mock_ctor.return_value.construct.side_effect = capture_construct

            gen.generate_regime_plan(
                strategies=[strat],
                prices={},
                trade_date="2026-01-01",
                equity=10_000.0,
            )

        universes_seen = {s.universe for s in captured}
        assert "sp500" in universes_seen
        assert "gold_etfs" in universes_seen

    def test_plan_output_contains_regime_metadata(self):
        """Plan dict must carry regime_state, active_universes, sizing_multiplier,
        and regime_reasoning when regime is enabled."""
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=True))
        regime = _make_regime_classification(
            state="transition_uncertain",
            universes=["sector_etfs", "treasury_etfs"],
            sizing=0.5,
        )

        with patch(_PATCH_REGIME_MODEL) as mock_cls, \
             patch(_PATCH_BUILD_MULTI, return_value={}), \
             patch(_PATCH_CONSTRUCTOR) as mock_ctor:

            inst = MagicMock()
            inst.classify_current.return_value = regime
            inst.classify_and_record.return_value = regime
            mock_cls.return_value = inst
            mock_ctor.return_value.construct.return_value = _make_constructed_portfolio()

            plan = gen.generate_regime_plan(
                strategies=[],
                prices={},
                trade_date="2026-01-01",
                equity=10_000.0,
            )

        assert plan["regime_state"] == "transition_uncertain"
        assert plan["active_universes"] == ["sector_etfs", "treasury_etfs"]
        assert plan["sizing_multiplier"] == 0.5
        assert plan["regime_reasoning"] == regime.reasoning

    def test_classify_and_record_is_called(self):
        """classify_and_record() must be called to persist today's regime."""
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=True))
        regime = _make_regime_classification()

        with patch(_PATCH_REGIME_MODEL) as mock_cls, \
             patch(_PATCH_BUILD_MULTI, return_value={}), \
             patch(_PATCH_CONSTRUCTOR) as mock_ctor:

            inst = MagicMock()
            inst.classify_current.return_value = regime
            inst.classify_and_record.return_value = regime
            mock_cls.return_value = inst
            mock_ctor.return_value.construct.return_value = _make_constructed_portfolio()

            gen.generate_regime_plan(
                strategies=[],
                prices={},
                trade_date="2026-01-01",
                equity=10_000.0,
            )

            inst.classify_and_record.assert_called_once()

    def test_portfolio_constructor_receives_regime_classification(self):
        """PortfolioConstructor must be instantiated with the regime classification
        and with universe_limits resolved from the active config (task #358)."""
        from portfolio.limits import resolve_universe_limits

        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=True))
        regime = _make_regime_classification()

        with patch(_PATCH_REGIME_MODEL) as mock_cls, \
             patch(_PATCH_BUILD_MULTI, return_value={}), \
             patch(_PATCH_CONSTRUCTOR) as mock_ctor_cls:

            inst = MagicMock()
            inst.classify_current.return_value = regime
            inst.classify_and_record.return_value = regime
            mock_cls.return_value = inst
            mock_ctor_cls.return_value.construct.return_value = _make_constructed_portfolio()

            gen.generate_regime_plan(
                strategies=[],
                prices={},
                trade_date="2026-01-01",
                equity=10_000.0,
            )

        # Constructor is instantiated exactly once.
        mock_ctor_cls.assert_called_once()
        kwargs = mock_ctor_cls.call_args.kwargs
        # Regime classification is passed through.
        assert kwargs.get("regime_classification") is regime
        # Task #358: universe_limits must be passed and equal to the result of
        # resolve_universe_limits() applied to the generator's own config.
        # This guarantees the regime-aware plan path consults config-driven
        # caps rather than the bare hardcoded UNIVERSE_LIMITS.
        assert "universe_limits" in kwargs, (
            "PortfolioConstructor must receive universe_limits in the "
            "regime-aware plan path (task #358)."
        )
        assert kwargs["universe_limits"] == resolve_universe_limits(gen.config)

    def test_strategies_run_on_each_universe(self):
        """A strategy must be called once per universe, not just once globally."""
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=True))
        regime = _make_regime_classification(universes=["sp500", "sector_etfs", "gold_etfs"])
        strat = _make_strategy("trend_following")

        with patch(_PATCH_REGIME_MODEL) as mock_cls, \
             patch(_PATCH_BUILD_MULTI, return_value={
                 "sp500":       {"AAPL": MagicMock()},
                 "sector_etfs": {"XLK":  MagicMock()},
                 "gold_etfs":   {"GLD":  MagicMock()},
             }), \
             patch(_PATCH_CONSTRUCTOR) as mock_ctor:

            inst = MagicMock()
            inst.classify_current.return_value = regime
            inst.classify_and_record.return_value = regime
            mock_cls.return_value = inst
            mock_ctor.return_value.construct.return_value = _make_constructed_portfolio()

            gen.generate_regime_plan(
                strategies=[strat],
                prices={},
                trade_date="2026-01-01",
                equity=10_000.0,
            )

        # 3 universes × 1 strategy = 3 calls
        assert strat.generate_signals.call_count == 3


# ─────────────────────────────────────────────────────────────────────────────
# Tests: graceful fallback when regime layer raises
# ─────────────────────────────────────────────────────────────────────────────


class TestGracefulFallback:
    """Regime model failures must not crash the plan run."""

    def test_fallback_on_classify_current_error(self):
        """ValueError from classify_current → falls back to SP500-only, no raise."""
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=True))
        strat = _make_strategy("momentum_breakout")

        with patch(_PATCH_REGIME_MODEL) as mock_cls:
            mock_cls.return_value.classify_current.side_effect = ValueError(
                "No macro indicators in database — run ingest first."
            )

            plan = gen.generate_regime_plan(
                strategies=[strat],
                prices={},
                trade_date="2026-01-01",
                equity=10_000.0,
                sp500_data={"AAPL": MagicMock()},
            )

        assert plan["status"] == "PENDING_APPROVAL"

    def test_fallback_plan_has_no_regime_metadata(self):
        """A fallback plan must NOT contain regime fields."""
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=True))

        with patch(_PATCH_REGIME_MODEL) as mock_cls:
            mock_cls.return_value.classify_current.side_effect = RuntimeError("DB error")

            plan = gen.generate_regime_plan(
                strategies=[],
                prices={},
                trade_date="2026-01-01",
                equity=10_000.0,
            )

        assert "regime_state"    not in plan
        assert "active_universes" not in plan
        assert "sizing_multiplier" not in plan
        assert "regime_reasoning" not in plan

    def test_fallback_runs_strategies_on_sp500_data(self):
        """After regime failure the strategy is still called with the sp500_data."""
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=True))
        strat = _make_strategy("momentum_breakout")
        sp500_data = {"AAPL": MagicMock()}

        with patch(_PATCH_REGIME_MODEL) as mock_cls:
            mock_cls.return_value.classify_current.side_effect = ConnectionError(
                "SQLite unavailable"
            )

            gen.generate_regime_plan(
                strategies=[strat],
                prices={},
                trade_date="2026-01-01",
                equity=10_000.0,
                sp500_data=sp500_data,
            )

        strat.generate_signals.assert_called_once_with(sp500_data, 10_000.0, [])

    def test_fallback_on_build_multi_universe_error(self):
        """build_multi_universe failure → falls back gracefully."""
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=True))
        regime = _make_regime_classification()

        with patch(_PATCH_REGIME_MODEL) as mock_cls, \
             patch(_PATCH_BUILD_MULTI, side_effect=IOError("DB unavailable")):

            inst = MagicMock()
            inst.classify_current.return_value = regime
            mock_cls.return_value = inst

            plan = gen.generate_regime_plan(
                strategies=[],
                prices={},
                trade_date="2026-01-01",
                equity=10_000.0,
            )

        assert plan["status"] == "PENDING_APPROVAL"
        assert "regime_state" not in plan

    def test_classify_and_record_failure_does_not_abort_plan(self):
        """classify_and_record() raising must not prevent the plan from being returned."""
        gen = TradePlanGenerator(_make_portfolio(), _make_config(regime_enabled=True))
        regime = _make_regime_classification()

        with patch(_PATCH_REGIME_MODEL) as mock_cls, \
             patch(_PATCH_BUILD_MULTI, return_value={}), \
             patch(_PATCH_CONSTRUCTOR) as mock_ctor:

            inst = MagicMock()
            inst.classify_current.return_value = regime
            inst.classify_and_record.side_effect = RuntimeError("write failed")
            mock_cls.return_value = inst
            mock_ctor.return_value.construct.return_value = _make_constructed_portfolio()

            plan = gen.generate_regime_plan(
                strategies=[],
                prices={},
                trade_date="2026-01-01",
                equity=10_000.0,
            )

        # Plan was returned despite the classify_and_record failure.
        assert plan["regime_state"] == regime.state.value
