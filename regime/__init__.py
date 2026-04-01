"""
regime — Layer 1 Quantitative Regime Model package.

Public API
----------
    from regime import RegimeState, REGIME_CONFIGS

    # Enum member access
    state = RegimeState.BULL_RISK_ON
    assert state.value == "bull_risk_on"

    # Config lookup
    cfg = REGIME_CONFIGS[RegimeState.BEAR_CAPITULATION]
    print(cfg["sizing_multiplier"])   # 0.3

    # String-valued lookup (for SQLite row dicts)
    from regime.states import REGIME_CONFIGS_BY_VALUE
    cfg = REGIME_CONFIGS_BY_VALUE["recovery_early"]
"""
from regime.states import (
    REGIME_CONFIGS,
    REGIME_CONFIGS_BY_VALUE,
    REQUIRED_CONFIG_KEYS,
    RegimeConfig,
    RegimeState,
)

__all__ = [
    "RegimeState",
    "REGIME_CONFIGS",
    "REGIME_CONFIGS_BY_VALUE",
    "REQUIRED_CONFIG_KEYS",
    "RegimeConfig",
]
