"""
regime/states.py — RegimeState enum and REGIME_CONFIGS mapping.

Six macro regime states covering trend × risk-appetite space.  Each state
maps to a canonical configuration that controls which universes are active,
which strategy types are permitted, how aggressively positions are sized, and
the maximum number of open positions.

The `RegimeModel` (regime/model.py) classifies each trading day into one of
these states using quantitative indicators; `REGIME_CONFIGS` then translates
that classification into actionable portfolio parameters.

Usage
-----
    from regime.states import RegimeState, REGIME_CONFIGS

    state = RegimeState.BULL_RISK_ON
    cfg   = REGIME_CONFIGS[state]
    print(cfg["sizing_multiplier"])   # 1.0
"""
from __future__ import annotations

from enum import Enum
from typing import Any


# ──────────────────────────────────────────────────────────────────────────────
# Regime state enum
# ──────────────────────────────────────────────────────────────────────────────


class RegimeState(str, Enum):
    """
    Six mutually-exclusive macro regime states.

    States are strings so they can be stored directly in SQLite without
    serialisation and compared with ``==`` to plain string values.

    Activation conditions (brief):
        BULL_RISK_ON        SPY > 200-DMA, VIX < 20, credit spreads tight.
                            Full deployment across growth universes.

        BULL_RISK_OFF       SPY > 200-DMA but VIX elevated *or* credit
                            spreading.  Selective strategies, rotate toward
                            defensives.

        TRANSITION_UNCERTAIN Flat or conflicting signals — no clear trend.
                            Small positions, mean-reversion focus.

        BEAR_RISK_OFF       SPY < 200-DMA, VIX > 25, credit widening.
                            Capital preservation; safe-haven trend-follow.

        BEAR_CAPITULATION   VIX > 35, yield curve inverted, credit blowout.
                            Minimal deployment — mostly cash, small gold/tsy.

        RECOVERY_EARLY      SPY crossing back above 200-DMA, VIX declining
                            from elevated levels.  Increasing deployment;
                            momentum + trend for the turn.
    """

    BULL_RISK_ON         = "bull_risk_on"
    BULL_RISK_OFF        = "bull_risk_off"
    TRANSITION_UNCERTAIN = "transition_uncertain"
    BEAR_RISK_OFF        = "bear_risk_off"
    BEAR_CAPITULATION    = "bear_capitulation"
    RECOVERY_EARLY       = "recovery_early"


# ──────────────────────────────────────────────────────────────────────────────
# Regime → portfolio configuration mapping
# ──────────────────────────────────────────────────────────────────────────────

#: Type alias for a single regime configuration block.
RegimeConfig = dict[str, Any]

REGIME_CONFIGS: dict[RegimeState, RegimeConfig] = {
    RegimeState.BULL_RISK_ON: {
        # All growth universes active; full strategy suite; full sizing.
        "active_universes": ["sp500", "sector_etfs", "commodity_etfs"],
        "strategy_types": ["all"],
        "sizing_multiplier": 1.0,
        "max_positions": 5,
    },
    RegimeState.BULL_RISK_OFF: {
        # Trend intact but risk appetite softening.  Rotate toward defensive.
        # Avoid momentum-chasing; favour mean-reversion and trend-following.
        "active_universes": ["sp500", "sector_etfs", "treasury_etfs"],
        "strategy_types": ["mean_reversion", "trend_following"],
        "sizing_multiplier": 0.7,
        "max_positions": 4,
    },
    RegimeState.TRANSITION_UNCERTAIN: {
        # Signals conflicting — de-risk materially.
        # Focus on lower-beta defensive universes and short-duration mean-rev.
        "active_universes": ["sector_etfs", "treasury_etfs", "gold_etfs"],
        "strategy_types": ["mean_reversion", "short_term_mr"],
        "sizing_multiplier": 0.5,
        "max_positions": 3,
    },
    RegimeState.BEAR_RISK_OFF: {
        # Trend broken, volatility elevated.  Capital preservation mode.
        # Only trend-follow safe-haven assets (treasuries, gold, defensives).
        "active_universes": ["treasury_etfs", "gold_etfs", "defensive_etfs"],
        "strategy_types": ["trend_following"],
        "sizing_multiplier": 0.5,
        "max_positions": 3,
    },
    RegimeState.BEAR_CAPITULATION: {
        # Panic regime — VIX > 35, credit blowout.
        # Minimal deployment; preserve capital; small safe-haven positions only.
        "active_universes": ["treasury_etfs", "gold_etfs"],
        "strategy_types": ["trend_following"],
        "sizing_multiplier": 0.3,
        "max_positions": 2,
    },
    RegimeState.RECOVERY_EARLY: {
        # Early signs of recovery: SPY reclaiming 200-DMA, VIX declining.
        # Selectively add momentum and trend exposure; remain cautious.
        "active_universes": ["sp500", "sector_etfs", "commodity_etfs"],
        "strategy_types": ["momentum_breakout", "trend_following"],
        "sizing_multiplier": 0.7,
        "max_positions": 4,
    },
}

# Convenience: string-keyed view for code that uses raw state values
# (e.g., SQLite row lookups).
REGIME_CONFIGS_BY_VALUE: dict[str, RegimeConfig] = {
    state.value: cfg for state, cfg in REGIME_CONFIGS.items()
}

# Required keys every REGIME_CONFIGS entry must have.
REQUIRED_CONFIG_KEYS: frozenset[str] = frozenset(
    {"active_universes", "strategy_types", "sizing_multiplier", "max_positions"}
)
