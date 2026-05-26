"""
portfolio/limits.py — Per-universe position and equity-exposure caps.

UNIVERSE_LIMITS defines hard ceilings for each of the six Atlas universes.
The PortfolioConstructor enforces these limits when selecting signals.

Keys
----
max_positions   Maximum simultaneous open positions within the universe.
max_pct_equity  Maximum fraction of total portfolio equity deployed in the
                universe (e.g. 0.60 = 60 %).

Config-driven overrides (task #358)
-----------------------------------
Active config can override these defaults per-universe via the optional
``risk.universe_limits`` block.  Example: tighten SP500 down to 3 names /
40% and widen sector ETFs slightly while leaving every other universe at
its hardcoded default::

    {
      "risk": {
        "universe_limits": {
          "sp500":       {"max_positions": 3, "max_pct_equity": 0.40},
          "sector_etfs": {"max_positions": 4, "max_pct_equity": 0.35}
        }
      }
    }

**Where these overrides apply.** Config-driven limits are consulted only
in the regime-aware plan path — i.e. when the active config sets
``regime_enabled: true``.  The legacy SP500-only path
(``regime_enabled: false`` / missing) is unaffected: it never instantiates
:class:`portfolio.constructor.PortfolioConstructor` with the resolved
limits, so live SP500 caps stay at their hardcoded defaults until the
regime-aware pipeline is enabled.

Override rules — designed so a *missing* or *malformed* block can NEVER
silently raise live deployment caps beyond their hardcoded defaults:

* Missing/empty block → defaults from ``UNIVERSE_LIMITS`` are used.
* Each override key is validated:
  - ``max_positions`` must be ``int`` in ``[1, 50]``.
  - ``max_pct_equity`` must be a number in ``(0.0, 1.0]``.
  - Invalid values are *rejected with a warning*; the hardcoded default for
    that universe (or ``_DEFAULT_LIMIT`` for unknown universes) is kept.
* Only the keys present are overridden; the other key keeps its default.
* Unknown keys inside an override (anything other than ``max_positions`` or
  ``max_pct_equity``) are *ignored with a warning* — so a typo like
  ``max_postions: 8`` does not silently fail to apply.
* Overrides for *unknown* universes are accepted but logged — this lets a
  reviewer pre-stage a future universe before it exists in ``UNIVERSE_LIMITS``.

The plan generator wires this up by calling :func:`resolve_universe_limits`
on the active config and passing the result into
:class:`portfolio.constructor.PortfolioConstructor`.  To tune a live cap,
add/modify the relevant ``risk.universe_limits.<universe>`` block in the
candidate config and promote it through the standard risk-gated review
flow (see ``atlas_risk_check_config_promotion``).
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Mapping, Optional, TypedDict

logger = logging.getLogger(__name__)


class UniverseLimit(TypedDict):
    max_positions: int
    max_pct_equity: float


UNIVERSE_LIMITS: dict[str, UniverseLimit] = {
    "sp500":          {"max_positions": 5, "max_pct_equity": 0.60},
    "sector_etfs":    {"max_positions": 3, "max_pct_equity": 0.30},
    "treasury_etfs":  {"max_positions": 2, "max_pct_equity": 0.40},
    "commodity_etfs": {"max_positions": 3, "max_pct_equity": 0.30},
    "gold_etfs":      {"max_positions": 2, "max_pct_equity": 0.20},
    "defensive_etfs": {"max_positions": 2, "max_pct_equity": 0.30},
}

# Fallback used when a signal's universe is not in UNIVERSE_LIMITS.
_DEFAULT_LIMIT: UniverseLimit = {"max_positions": 3, "max_pct_equity": 0.30}

# Validation bounds for config-driven overrides.  These are intentionally
# conservative — they exist so a malformed config cannot escalate live risk
# beyond reason.  Tighter limits per-universe still apply via the per-call
# review/risk-gate flow.
_MAX_POSITIONS_LO = 1
_MAX_POSITIONS_HI = 50
_MAX_PCT_EQUITY_LO_EXCLUSIVE = 0.0
_MAX_PCT_EQUITY_HI_INCLUSIVE = 1.0

# Keys recognised inside a per-universe override block.  Anything else
# (typos, deprecated fields, etc.) is ignored with a warning so a silent
# no-op cannot hide behind a misspelt key like ``max_postions``.
_VALID_OVERRIDE_KEYS: frozenset[str] = frozenset({"max_positions", "max_pct_equity"})


def get_limit(
    universe: str,
    overrides: Optional[Mapping[str, UniverseLimit]] = None,
) -> UniverseLimit:
    """Return the limit config for *universe*.

    When *overrides* (typically produced by :func:`resolve_universe_limits`)
    is supplied, it is consulted first; otherwise the hardcoded defaults
    apply and unknown universes fall back to ``_DEFAULT_LIMIT``.
    """
    if overrides and universe in overrides:
        return overrides[universe]
    return UNIVERSE_LIMITS.get(universe, _DEFAULT_LIMIT)


def _coerce_max_positions(raw: Any) -> Optional[int]:
    """Return validated ``max_positions`` int, or None if invalid."""
    # Reject bool (subclass of int in Python — would silently become 0/1).
    if isinstance(raw, bool):
        return None
    if not isinstance(raw, int):
        return None
    if not (_MAX_POSITIONS_LO <= raw <= _MAX_POSITIONS_HI):
        return None
    return raw


def _coerce_max_pct_equity(raw: Any) -> Optional[float]:
    """Return validated ``max_pct_equity`` float, or None if invalid."""
    if isinstance(raw, bool):
        return None
    if not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    # Use exclusive lower bound: 0% would block every entry; that should be
    # expressed by disabling the universe upstream, not via the cap.
    if not (_MAX_PCT_EQUITY_LO_EXCLUSIVE < value <= _MAX_PCT_EQUITY_HI_INCLUSIVE):
        return None
    return value


def resolve_universe_limits(
    config: Optional[Mapping[str, Any]],
) -> dict[str, UniverseLimit]:
    """Return a fully resolved per-universe limits dict.

    Starts from the hardcoded ``UNIVERSE_LIMITS`` defaults and applies
    validated overrides from ``config["risk"]["universe_limits"]``.

    Parameters
    ----------
    config:
        Active config dict (e.g. loaded from ``config/active/sp500.json``).
        May be ``None`` or missing the ``risk.universe_limits`` block.

    Returns
    -------
    dict[str, UniverseLimit]
        Deep-copied limits dict.  Always contains every key present in
        ``UNIVERSE_LIMITS``; may contain additional keys for any
        valid override that targets a universe not in the defaults.

    Behavior contract
    -----------------
    * Missing config or ``risk.universe_limits`` block → returns the
      defaults unchanged (current production behavior).
    * Each invalid override entry is *ignored with a warning*; the
      corresponding default is preserved.  No silent risk escalation.
    * Both ``max_positions`` and ``max_pct_equity`` are merged
      independently so a config can tune just one of the two.
    """
    resolved: dict[str, UniverseLimit] = copy.deepcopy(UNIVERSE_LIMITS)

    if not config:
        return resolved

    risk_block = config.get("risk") if isinstance(config, Mapping) else None
    if not isinstance(risk_block, Mapping):
        return resolved

    overrides_block = risk_block.get("universe_limits")
    if overrides_block in (None, {}):
        return resolved
    if not isinstance(overrides_block, Mapping):
        logger.warning(
            "risk.universe_limits must be a mapping; got %s — ignoring",
            type(overrides_block).__name__,
        )
        return resolved

    for universe, override in overrides_block.items():
        if not isinstance(universe, str) or not universe:
            logger.warning(
                "risk.universe_limits: ignoring non-string universe key %r",
                universe,
            )
            continue
        if not isinstance(override, Mapping):
            logger.warning(
                "risk.universe_limits.%s must be a mapping; got %s — ignoring",
                universe, type(override).__name__,
            )
            continue

        # Warn (don't fail) on unknown keys inside the override block.  This
        # turns silent typos (e.g. ``max_postions``) into visible log noise
        # without changing live behavior for valid configs.
        unknown_keys = sorted(
            k for k in override.keys()
            if isinstance(k, str) and k not in _VALID_OVERRIDE_KEYS
        )
        if unknown_keys:
            logger.warning(
                "risk.universe_limits.%s contains unknown key(s) %s — ignoring "
                "(valid keys: %s). Likely a typo; the intended override was "
                "NOT applied.",
                universe,
                unknown_keys,
                sorted(_VALID_OVERRIDE_KEYS),
            )

        # Start from current default for this universe, or the generic
        # fallback when the universe isn't in UNIVERSE_LIMITS.
        base: UniverseLimit = dict(  # type: ignore[assignment]
            resolved.get(universe, _DEFAULT_LIMIT)
        )

        if "max_positions" in override:
            coerced = _coerce_max_positions(override["max_positions"])
            if coerced is None:
                logger.warning(
                    "risk.universe_limits.%s.max_positions invalid (%r); "
                    "keeping default %d",
                    universe, override["max_positions"], base["max_positions"],
                )
            else:
                base["max_positions"] = coerced

        if "max_pct_equity" in override:
            coerced_pct = _coerce_max_pct_equity(override["max_pct_equity"])
            if coerced_pct is None:
                logger.warning(
                    "risk.universe_limits.%s.max_pct_equity invalid (%r); "
                    "keeping default %.4f",
                    universe, override["max_pct_equity"], base["max_pct_equity"],
                )
            else:
                base["max_pct_equity"] = coerced_pct

        if universe not in UNIVERSE_LIMITS:
            logger.info(
                "risk.universe_limits.%s applies to a universe not present "
                "in UNIVERSE_LIMITS defaults — accepted as forward-staged "
                "override (max_positions=%d, max_pct_equity=%.4f)",
                universe, base["max_positions"], base["max_pct_equity"],
            )

        resolved[universe] = base

    return resolved
