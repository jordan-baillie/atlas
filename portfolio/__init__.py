"""
portfolio — Cross-universe portfolio construction for Atlas.

Modules
-------
limits      Per-universe position and equity-exposure caps.
correlation Static correlation-group conflict detection.
constructor PortfolioConstructor — filters, sizes, and ranks signals
            before they reach the plan generator.

Typical usage
-------------
    from portfolio.constructor import PortfolioConstructor
    from regime.model import RegimeModel

    model  = RegimeModel()
    regime = model.classify_and_record()

    constructor = PortfolioConstructor(regime_classification=regime)
    result = constructor.construct(signals, equity=10_000)
    print(result.reasoning)
"""
from portfolio.constructor import ConstructedPortfolio, PortfolioConstructor
from portfolio.correlation import CORRELATION_GROUPS, check_correlation_conflicts
from portfolio.limits import UNIVERSE_LIMITS, get_limit, resolve_universe_limits

__all__ = [
    "PortfolioConstructor",
    "ConstructedPortfolio",
    "UNIVERSE_LIMITS",
    "CORRELATION_GROUPS",
    "check_correlation_conflicts",
    "get_limit",
    "resolve_universe_limits",
]
