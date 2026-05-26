"""
portfolio/tests/test_limits_config.py — Tests for config-driven per-universe
deployment limits (task #358).

Covers:
1. Default behavior matches the hardcoded ``UNIVERSE_LIMITS``.
2. Config override changes ``max_positions`` / ``max_pct_equity`` for the
   targeted universe only.
3. Invalid override values are rejected/ignored safely — current defaults
   are preserved (no silent risk escalation).
4. ``max_open_positions`` (a regime-level cap) does NOT silently override
   per-universe deployment limits.
5. End-to-end: ``PortfolioConstructor`` honors the resolved overrides.

Run with::

    cd /root/atlas && python3 -m pytest portfolio/tests/test_limits_config.py -v
"""
from __future__ import annotations

import logging

import pytest

from portfolio.constructor import PortfolioConstructor
from portfolio.limits import (
    UNIVERSE_LIMITS,
    _DEFAULT_LIMIT,
    get_limit,
    resolve_universe_limits,
)
from regime.model import RegimeClassification
from regime.states import RegimeState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_signal(
    ticker: str,
    universe: str = "sp500",
    confidence: float = 0.75,
    entry: float = 100.0,
    stop: float | None = None,
    size: int = 10,
):
    """Return a minimal Signal-compatible object."""
    from strategies.base import Signal

    _stop = stop if stop is not None else round(entry * 0.95, 2)
    return Signal(
        ticker=ticker,
        strategy="test_strategy",
        direction="long",
        entry_price=entry,
        stop_price=_stop,
        take_profit=entry * 1.1,
        position_size=size,
        position_value=round(entry * size, 2),
        risk_amount=round((entry - _stop) * size, 2),
        confidence=confidence,
        rationale="test",
        universe=universe,
    )


def make_sp500_regime(max_positions: int = 10) -> RegimeClassification:
    """A permissive regime so the per-universe cap is the binding constraint."""
    return RegimeClassification(
        state=RegimeState.BULL_RISK_ON,
        scores={"composite": 0.7},
        active_universes=["sp500"],
        sizing_multiplier=1.0,
        max_positions=max_positions,
        enabled_strategies=["all"],
        reasoning="test",
        model_version="v1",
        date="2026-01-01",
    )


# ---------------------------------------------------------------------------
# 1. Defaults match current behavior when config has no overrides
# ---------------------------------------------------------------------------


class TestDefaultsPreserved:
    def test_resolve_none_returns_defaults(self):
        resolved = resolve_universe_limits(None)
        # Deep-equal to hardcoded defaults; deep-copied (so callers can't
        # mutate the module-level table by accident).
        assert resolved == UNIVERSE_LIMITS
        assert resolved is not UNIVERSE_LIMITS
        for universe, limit in UNIVERSE_LIMITS.items():
            assert resolved[universe] is not limit

    def test_resolve_empty_config_returns_defaults(self):
        assert resolve_universe_limits({}) == UNIVERSE_LIMITS

    def test_resolve_no_risk_block_returns_defaults(self):
        assert resolve_universe_limits({"trading": {"mode": "live"}}) == UNIVERSE_LIMITS

    def test_resolve_no_universe_limits_block_returns_defaults(self):
        assert (
            resolve_universe_limits({"risk": {"max_open_positions": 10}})
            == UNIVERSE_LIMITS
        )

    def test_resolve_empty_universe_limits_block_returns_defaults(self):
        cfg = {"risk": {"universe_limits": {}}}
        assert resolve_universe_limits(cfg) == UNIVERSE_LIMITS

    def test_sp500_default_unchanged(self):
        """Live SP500 caps must remain (5, 0.60) when nothing is overridden."""
        resolved = resolve_universe_limits({})
        assert resolved["sp500"] == {"max_positions": 5, "max_pct_equity": 0.60}

    def test_get_limit_without_overrides_matches_module_table(self):
        for universe, limit in UNIVERSE_LIMITS.items():
            assert get_limit(universe) == limit
        # Unknown universe falls back to the generic default.
        assert get_limit("does_not_exist") == _DEFAULT_LIMIT


# ---------------------------------------------------------------------------
# 2. Config overrides change limits for the targeted universe only
# ---------------------------------------------------------------------------


class TestConfigOverrides:
    def test_override_max_positions_only(self):
        cfg = {
            "risk": {
                "universe_limits": {
                    "sp500": {"max_positions": 8},
                },
            },
        }
        resolved = resolve_universe_limits(cfg)
        assert resolved["sp500"]["max_positions"] == 8
        # max_pct_equity not specified — keeps the default
        assert resolved["sp500"]["max_pct_equity"] == 0.60

    def test_override_max_pct_equity_only(self):
        cfg = {"risk": {"universe_limits": {"sp500": {"max_pct_equity": 0.75}}}}
        resolved = resolve_universe_limits(cfg)
        assert resolved["sp500"]["max_pct_equity"] == 0.75
        # max_positions not specified — keeps the default
        assert resolved["sp500"]["max_positions"] == 5

    def test_override_both_keys(self):
        cfg = {
            "risk": {
                "universe_limits": {
                    "sp500": {"max_positions": 9, "max_pct_equity": 0.50},
                },
            },
        }
        resolved = resolve_universe_limits(cfg)
        assert resolved["sp500"] == {"max_positions": 9, "max_pct_equity": 0.50}

    def test_override_isolated_to_target_universe(self):
        """Overriding sp500 must leave every other universe untouched."""
        cfg = {
            "risk": {
                "universe_limits": {
                    "sp500": {"max_positions": 9, "max_pct_equity": 0.50},
                },
            },
        }
        resolved = resolve_universe_limits(cfg)
        for u, default in UNIVERSE_LIMITS.items():
            if u == "sp500":
                continue
            assert resolved[u] == default, f"Universe {u} should be unchanged"

    def test_override_multiple_universes(self):
        cfg = {
            "risk": {
                "universe_limits": {
                    "sp500":       {"max_positions": 7},
                    "sector_etfs": {"max_pct_equity": 0.40},
                },
            },
        }
        resolved = resolve_universe_limits(cfg)
        assert resolved["sp500"]["max_positions"] == 7
        assert resolved["sp500"]["max_pct_equity"] == 0.60  # default kept
        assert resolved["sector_etfs"]["max_positions"] == 3  # default kept
        assert resolved["sector_etfs"]["max_pct_equity"] == 0.40

    def test_override_unknown_universe_accepted(self, caplog):
        """Forward-staging a future universe is allowed but logged."""
        cfg = {
            "risk": {
                "universe_limits": {
                    "future_market": {"max_positions": 4, "max_pct_equity": 0.35},
                },
            },
        }
        with caplog.at_level(logging.INFO, logger="portfolio.limits"):
            resolved = resolve_universe_limits(cfg)
        assert resolved["future_market"] == {
            "max_positions": 4, "max_pct_equity": 0.35,
        }
        # Known universes stay at their defaults.
        for u, default in UNIVERSE_LIMITS.items():
            assert resolved[u] == default


# ---------------------------------------------------------------------------
# 3. Invalid override values are rejected/ignored safely
# ---------------------------------------------------------------------------


class TestInvalidValuesRejected:
    """Every malformed input must keep the existing hardcoded default —
    we never want a broken config to silently raise live deployment caps."""

    def _expect_sp500_unchanged(self, cfg) -> dict:
        resolved = resolve_universe_limits(cfg)
        assert resolved["sp500"] == UNIVERSE_LIMITS["sp500"]
        return resolved

    def test_universe_limits_not_a_mapping(self, caplog):
        cfg = {"risk": {"universe_limits": [1, 2, 3]}}
        with caplog.at_level(logging.WARNING, logger="portfolio.limits"):
            resolved = resolve_universe_limits(cfg)
        assert resolved == UNIVERSE_LIMITS
        assert any("must be a mapping" in r.message for r in caplog.records)

    def test_universe_override_not_a_mapping(self, caplog):
        cfg = {"risk": {"universe_limits": {"sp500": "10"}}}
        with caplog.at_level(logging.WARNING, logger="portfolio.limits"):
            self._expect_sp500_unchanged(cfg)
        assert any("sp500 must be a mapping" in r.message for r in caplog.records)

    @pytest.mark.parametrize(
        "bad_value",
        [
            0,            # below lo bound — would block universe
            -1,           # negative
            51,           # above hi bound (50)
            "5",          # string
            5.5,          # float not int
            True,         # bool subclass of int — must be rejected
            None,
        ],
    )
    def test_invalid_max_positions_rejected(self, bad_value, caplog):
        cfg = {
            "risk": {
                "universe_limits": {
                    "sp500": {"max_positions": bad_value},
                },
            },
        }
        with caplog.at_level(logging.WARNING, logger="portfolio.limits"):
            self._expect_sp500_unchanged(cfg)
        assert any("max_positions invalid" in r.message for r in caplog.records)

    @pytest.mark.parametrize(
        "bad_value",
        [
            0.0,          # exclusive lo bound
            -0.1,         # negative
            1.5,          # above 1.0
            "0.5",        # string
            True,         # bool — must be rejected
            None,
        ],
    )
    def test_invalid_max_pct_equity_rejected(self, bad_value, caplog):
        cfg = {
            "risk": {
                "universe_limits": {
                    "sp500": {"max_pct_equity": bad_value},
                },
            },
        }
        with caplog.at_level(logging.WARNING, logger="portfolio.limits"):
            self._expect_sp500_unchanged(cfg)
        assert any("max_pct_equity invalid" in r.message for r in caplog.records)

    def test_partial_invalid_keeps_other_key(self, caplog):
        """If max_positions is bad but max_pct_equity is good, only the
        good half is applied; the bad half keeps its default."""
        cfg = {
            "risk": {
                "universe_limits": {
                    "sp500": {"max_positions": -1, "max_pct_equity": 0.50},
                },
            },
        }
        with caplog.at_level(logging.WARNING, logger="portfolio.limits"):
            resolved = resolve_universe_limits(cfg)
        assert resolved["sp500"]["max_positions"] == 5      # default kept
        assert resolved["sp500"]["max_pct_equity"] == 0.50  # override applied

    def test_max_positions_boundary_values_accepted(self):
        cfg_lo = {"risk": {"universe_limits": {"sp500": {"max_positions": 1}}}}
        cfg_hi = {"risk": {"universe_limits": {"sp500": {"max_positions": 50}}}}
        assert resolve_universe_limits(cfg_lo)["sp500"]["max_positions"] == 1
        assert resolve_universe_limits(cfg_hi)["sp500"]["max_positions"] == 50

    def test_max_pct_equity_boundary_values(self):
        # Upper boundary is inclusive (100% allowed for single-market accounts).
        cfg_hi = {"risk": {"universe_limits": {"sp500": {"max_pct_equity": 1.0}}}}
        assert resolve_universe_limits(cfg_hi)["sp500"]["max_pct_equity"] == 1.0

    def test_non_string_universe_key_ignored(self, caplog):
        cfg = {"risk": {"universe_limits": {123: {"max_positions": 4}}}}
        with caplog.at_level(logging.WARNING, logger="portfolio.limits"):
            resolved = resolve_universe_limits(cfg)
        assert resolved == UNIVERSE_LIMITS

    def test_unknown_extra_key_in_override_warns_but_keeps_valid_keys(self, caplog):
        """Unknown keys in the override dict must be ignored *with a warning*
        — so a typo like ``max_postions`` doesn't silently fail to apply.
        Valid keys in the same block are still honored."""
        cfg = {
            "risk": {
                "universe_limits": {
                    "sp500": {
                        "max_positions": 6,
                        "unknown_key": "should be ignored",
                    },
                },
            },
        }
        with caplog.at_level(logging.WARNING, logger="portfolio.limits"):
            resolved = resolve_universe_limits(cfg)
        assert resolved["sp500"]["max_positions"] == 6
        assert "unknown_key" not in resolved["sp500"]
        assert any(
            "unknown key" in r.message and "unknown_key" in r.message
            for r in caplog.records
        ), (
            "Expected a WARNING naming the unknown key so typos surface in "
            "logs instead of silently doing nothing."
        )

    def test_typo_in_override_key_warns_and_keeps_default(self, caplog):
        """Concrete typo scenario: ``max_postions`` (missing 'i') must not
        silently raise the cap and must produce a warning."""
        cfg = {
            "risk": {
                "universe_limits": {
                    "sp500": {"max_postions": 25},  # typo: should be max_positions
                },
            },
        }
        with caplog.at_level(logging.WARNING, logger="portfolio.limits"):
            resolved = resolve_universe_limits(cfg)
        # Default kept — nothing was applied.
        assert resolved["sp500"] == UNIVERSE_LIMITS["sp500"]
        assert any(
            "max_postions" in r.message and "unknown" in r.message.lower()
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# 4. max_open_positions does NOT silently override the universe limit
# ---------------------------------------------------------------------------


class TestMaxOpenPositionsIsolation:
    """`risk.max_open_positions` is an account-wide cap consumed by the
    regime model — it must NOT be confused with per-universe deployment
    limits.  Setting only `max_open_positions` should leave every universe
    cap at its hardcoded default."""

    def test_max_open_positions_does_not_change_universe_limits(self):
        cfg = {
            "risk": {
                "max_open_positions": 25,
                "max_risk_per_trade_pct": 0.005,
                # No universe_limits block.
            },
        }
        resolved = resolve_universe_limits(cfg)
        assert resolved == UNIVERSE_LIMITS

    def test_overall_cap_independent_of_universe_cap(self):
        """End-to-end: with sp500 cap held at 5 and overall regime
        max_positions = 25, only 5 sp500 signals should be selected."""
        regime = make_sp500_regime(max_positions=25)
        cfg = {"risk": {"max_open_positions": 25}}  # no universe_limits
        constructor = PortfolioConstructor(
            regime_classification=regime,
            universe_limits=resolve_universe_limits(cfg),
        )
        signals = [make_signal(f"T{i}", universe="sp500") for i in range(12)]
        result = constructor.construct(signals, equity=10_000_000)
        assert len(result.signals) == UNIVERSE_LIMITS["sp500"]["max_positions"]


# ---------------------------------------------------------------------------
# 5. End-to-end: PortfolioConstructor honors the resolved overrides
# ---------------------------------------------------------------------------


class TestConstructorEndToEnd:
    def test_default_constructor_unchanged(self):
        """No universe_limits arg → hardcoded behavior."""
        regime = make_sp500_regime(max_positions=10)
        constructor = PortfolioConstructor(regime_classification=regime)
        signals = [make_signal(f"T{i}", universe="sp500") for i in range(8)]
        result = constructor.construct(signals, equity=10_000_000)
        assert len(result.signals) == UNIVERSE_LIMITS["sp500"]["max_positions"]  # 5

    def test_override_raises_position_cap(self):
        """With override max_positions=8 and regime cap=10, 8 should pass."""
        regime = make_sp500_regime(max_positions=10)
        cfg = {"risk": {"universe_limits": {"sp500": {"max_positions": 8}}}}
        constructor = PortfolioConstructor(
            regime_classification=regime,
            universe_limits=resolve_universe_limits(cfg),
        )
        signals = [make_signal(f"T{i}", universe="sp500") for i in range(10)]
        result = constructor.construct(signals, equity=10_000_000)
        assert len(result.signals) == 8

    def test_override_lowers_position_cap(self):
        regime = make_sp500_regime(max_positions=10)
        cfg = {"risk": {"universe_limits": {"sp500": {"max_positions": 2}}}}
        constructor = PortfolioConstructor(
            regime_classification=regime,
            universe_limits=resolve_universe_limits(cfg),
        )
        signals = [make_signal(f"T{i}", universe="sp500") for i in range(5)]
        result = constructor.construct(signals, equity=10_000_000)
        assert len(result.signals) == 2

    def test_override_max_pct_equity_blocks_excess_exposure(self):
        """Tighten max_pct_equity and ensure the equity cap binds first."""
        regime = make_sp500_regime(max_positions=10)
        # Default is 0.60.  Lower to 0.05 → only 5% of equity may sit in sp500.
        cfg = {"risk": {"universe_limits": {"sp500": {"max_pct_equity": 0.05}}}}
        constructor = PortfolioConstructor(
            regime_classification=regime,
            universe_limits=resolve_universe_limits(cfg),
        )
        equity = 10_000
        # Each signal worth $1,000.  Cap = $500 → first signal already exceeds
        # cap → zero accepted.
        signals = [
            make_signal("AAA", universe="sp500", entry=100.0, size=10),
            make_signal("BBB", universe="sp500", entry=100.0, size=10),
        ]
        result = constructor.construct(signals, equity=equity)
        assert len(result.signals) == 0

    def test_override_max_pct_equity_increase_allowed(self):
        """Raise cap to 1.0 — both signals fit within new cap."""
        regime = make_sp500_regime(max_positions=10)
        cfg = {"risk": {"universe_limits": {"sp500": {"max_pct_equity": 1.0}}}}
        constructor = PortfolioConstructor(
            regime_classification=regime,
            universe_limits=resolve_universe_limits(cfg),
        )
        # 5 signals, $2,000 each, equity $10,000 → 100% would be fully used.
        signals = [
            make_signal(f"T{i}", universe="sp500", entry=200.0, size=10)
            for i in range(5)
        ]
        result = constructor.construct(signals, equity=10_000)
        assert len(result.signals) == 5

    def test_invalid_override_falls_back_to_default(self):
        """Malformed config must NOT widen the live universe cap."""
        regime = make_sp500_regime(max_positions=10)
        cfg = {
            "risk": {
                "universe_limits": {
                    "sp500": {"max_positions": "fifty", "max_pct_equity": "loose"},
                },
            },
        }
        constructor = PortfolioConstructor(
            regime_classification=regime,
            universe_limits=resolve_universe_limits(cfg),
        )
        signals = [make_signal(f"T{i}", universe="sp500") for i in range(8)]
        result = constructor.construct(signals, equity=10_000_000)
        # Behavior must match the hardcoded default cap (5).
        assert len(result.signals) == UNIVERSE_LIMITS["sp500"]["max_positions"]
