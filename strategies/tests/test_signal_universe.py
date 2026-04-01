"""Tests for Signal.universe field (backward compatibility and explicit use)."""

import pytest
from strategies.base import Signal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_signal(**overrides) -> Signal:
    """Return a valid Signal with minimal required fields, allowing overrides."""
    defaults = dict(
        ticker="AAPL",
        strategy="test_strategy",
        direction="long",
        entry_price=100.0,
        stop_price=90.0,
        take_profit=120.0,
        position_size=10,
        position_value=1000.0,
        risk_amount=100.0,
        confidence=0.8,
        rationale="Test signal",
    )
    defaults.update(overrides)
    return Signal(**defaults)


# ---------------------------------------------------------------------------
# universe field — default behaviour
# ---------------------------------------------------------------------------

class TestSignalUniverseDefault:
    def test_universe_field_exists(self):
        """Signal must have a 'universe' field."""
        import inspect
        params = list(inspect.signature(Signal).parameters.keys())
        assert "universe" in params, "universe field missing from Signal"

    def test_default_is_sp500(self):
        """Creating a signal without specifying universe defaults to 'sp500'."""
        sig = _minimal_signal()
        assert sig.universe == "sp500"

    def test_explicit_universe(self):
        """Signal can be created with an explicit universe value."""
        sig = _minimal_signal(universe="sector_etfs")
        assert sig.universe == "sector_etfs"

    def test_universe_values(self):
        """All expected universe identifiers are accepted."""
        expected_universes = [
            "sp500",
            "sector_etfs",
            "treasury_etfs",
            "commodity_etfs",
            "gold_etfs",
            "defensive_etfs",
        ]
        for u in expected_universes:
            sig = _minimal_signal(universe=u)
            assert sig.universe == u


# ---------------------------------------------------------------------------
# to_dict serialization
# ---------------------------------------------------------------------------

class TestSignalSerialisation:
    def test_to_dict_includes_universe_default(self):
        """to_dict() must include 'universe' key when default is used."""
        sig = _minimal_signal()
        d = sig.to_dict()
        assert "universe" in d
        assert d["universe"] == "sp500"

    def test_to_dict_includes_universe_explicit(self):
        """to_dict() must preserve explicit universe value."""
        sig = _minimal_signal(universe="sector_etfs")
        d = sig.to_dict()
        assert d["universe"] == "sector_etfs"


# ---------------------------------------------------------------------------
# Backward compatibility — all existing strategy imports still work
# ---------------------------------------------------------------------------

class TestStrategyImports:
    """All strategy modules must still import cleanly after the Signal change."""

    def _import(self, module_path: str):
        import importlib
        return importlib.import_module(module_path)

    def test_import_momentum_breakout(self):
        self._import("strategies.momentum_breakout")

    def test_import_mean_reversion(self):
        self._import("strategies.mean_reversion")

    def test_import_trend_following(self):
        self._import("strategies.trend_following")

    def test_import_opening_gap(self):
        self._import("strategies.opening_gap")

    def test_import_sector_rotation(self):
        self._import("strategies.sector_rotation")

    def test_import_short_term_mr(self):
        self._import("strategies.short_term_mr")

    def test_import_connors_rsi2(self):
        self._import("strategies.connors_rsi2")

    def test_import_bb_squeeze(self):
        self._import("strategies.bb_squeeze")

    def test_import_dividend_capture(self):
        self._import("strategies.dividend_capture")

    def test_import_mtf_momentum(self):
        self._import("strategies.mtf_momentum")

    def test_import_entry_optimizer(self):
        self._import("strategies.entry_optimizer")
