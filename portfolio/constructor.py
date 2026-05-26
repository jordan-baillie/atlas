"""
portfolio/constructor.py — Cross-universe portfolio construction for Atlas.

The PortfolioConstructor sits between strategy signal generation and plan
output.  It receives raw signals from all active strategies/universes and
returns a :class:`ConstructedPortfolio` that respects:

* Per-universe position limits  (portfolio/limits.py)
* Cross-universe correlation caps  (portfolio/correlation.py)
* Regime active-universe filtering  (regime/states.py)
* Regime sizing multiplier
* Overall max-positions cap from the regime config
* Existing open positions counted toward limits
* AI-overlay adjustments (Phase 4 hook — pass-through for now)

Usage
-----
    from portfolio.constructor import PortfolioConstructor

    constructor = PortfolioConstructor(regime_classification=regime)
    result = constructor.construct(signals, equity=10_000,
                                   existing_positions=portfolio.positions)
    # result.signals → list of Signal objects ready for plan generation
    # result.rejected → list of (Signal, reason) tuples
    # result.reasoning → human-readable summary
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from portfolio.correlation import check_correlation_conflicts
from portfolio.limits import get_limit

logger = logging.getLogger(__name__)

# Default universe when no regime is provided (backward compatibility).
_DEFAULT_UNIVERSE = "sp500"
_DEFAULT_ACTIVE_UNIVERSES = ["sp500"]
_DEFAULT_SIZING_MULTIPLIER = 1.0
_DEFAULT_MAX_POSITIONS = 5
_DEFAULT_REGIME_STATE = "no_regime"


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------


@dataclass
class ConstructedPortfolio:
    """Result of a single portfolio-construction pass.

    Attributes
    ----------
    signals         Selected signals that passed all filters.  These are ready
                    to be forwarded to the plan generator.
    rejected        List of (signal, reason_str) tuples for signals that were
                    filtered out.  Useful for debugging and audit.
    universe_exposure
                    Summary of accepted positions per universe::

                        {
                            "sp500": {"positions": 3, "pct_equity": 0.45},
                            ...
                        }

    regime_state    String label of the active regime (or "no_regime").
    sizing_multiplier
                    Multiplier applied to position sizes.  1.0 = full size.
    total_positions Total accepted signal count.
    reasoning       Human-readable explanation of every construction decision.
    """

    signals: list = field(default_factory=list)
    rejected: list = field(default_factory=list)   # list[(signal, reason)]
    universe_exposure: dict = field(default_factory=dict)
    regime_state: str = _DEFAULT_REGIME_STATE
    sizing_multiplier: float = _DEFAULT_SIZING_MULTIPLIER
    total_positions: int = 0
    reasoning: str = ""


# ---------------------------------------------------------------------------
# PortfolioConstructor
# ---------------------------------------------------------------------------


class PortfolioConstructor:
    """Build a constrained portfolio from raw strategy signals.

    Parameters
    ----------
    regime_classification:
        :class:`regime.model.RegimeClassification` from the regime model.
        When *None*, defaults to SP500-only with full sizing (backward compat).
    overlay_adjustments:
        Dict from the AI overlay (Phase 4).  Keys are ticker symbols; values
        are dicts with optional ``sizing_scale`` (float) and ``veto`` (bool).
        Pass *None* (default) to skip overlay processing entirely.
    universe_limits:
        Optional fully-resolved per-universe limits dict (see
        :func:`portfolio.limits.resolve_universe_limits`).  When *None*
        (default) the constructor falls back to the hardcoded
        ``UNIVERSE_LIMITS`` table — preserving current production behavior.
        The plan generator passes ``resolve_universe_limits(active_config)``
        so that ``risk.universe_limits`` config overrides take effect for a
        given universe only (task #358).
    """

    def __init__(
        self,
        regime_classification=None,
        overlay_adjustments: Optional[dict] = None,
        universe_limits: Optional[dict] = None,
    ) -> None:
        self._regime = regime_classification
        self._overlay = overlay_adjustments or {}
        # When no overrides are supplied, the construct() loop falls back
        # to the hardcoded UNIVERSE_LIMITS via get_limit().  When supplied,
        # the dict is used as a fully resolved per-universe mapping.
        self._universe_limits = universe_limits

        if regime_classification is not None:
            self._active_universes: list[str] = list(regime_classification.active_universes)
            self._sizing_multiplier: float = float(regime_classification.sizing_multiplier)
            self._max_positions: int = int(regime_classification.max_positions)
            self._regime_state: str = regime_classification.state.value
        else:
            # Backward-compatible defaults — behave like a single-universe SP500 system.
            self._active_universes = list(_DEFAULT_ACTIVE_UNIVERSES)
            self._sizing_multiplier = _DEFAULT_SIZING_MULTIPLIER
            self._max_positions = _DEFAULT_MAX_POSITIONS
            self._regime_state = _DEFAULT_REGIME_STATE

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def construct(
        self,
        signals: list,
        equity: float,
        existing_positions: Optional[list] = None,
    ) -> ConstructedPortfolio:
        """Build a portfolio from raw signals, applying all constraints.

        Construction steps
        ------------------
        1. Filter to active universes only (from regime).
        2. Group by universe; apply per-universe position limits, counting
           existing open positions toward those limits.
        3. Apply cross-universe correlation check (max 2 per group).
        4. Apply overall ``max_positions`` cap (regime config).
        5. Apply regime ``sizing_multiplier`` to position sizes.
        6. Apply AI overlay adjustments (Phase 4 — pass-through for now).
        7. Build ``ConstructedPortfolio`` with exposure summary.

        Parameters
        ----------
        signals:
            Raw :class:`strategies.base.Signal` objects from all strategies.
        equity:
            Current portfolio equity in USD.  Used to compute pct_equity
            exposure metrics (not for sizing — that is the strategy's job).
        existing_positions:
            Currently open positions.  Each object must expose a ``.universe``
            attribute (str) and optionally a ``.ticker`` attribute.  When
            *None*, no existing positions are assumed.

        Returns
        -------
        ConstructedPortfolio
        """
        existing_positions = existing_positions or []
        rejected: list[tuple[Any, str]] = []
        reasoning_lines: list[str] = []

        reasoning_lines.append(
            f"Regime: {self._regime_state} | "
            f"Active universes: {self._active_universes} | "
            f"Sizing: {self._sizing_multiplier:.2f}x | "
            f"Max positions: {self._max_positions}"
        )

        # ── Step 1: filter to active universes ────────────────────────────
        universe_filtered: list[Any] = []
        for sig in signals:
            sig_universe = getattr(sig, "universe", _DEFAULT_UNIVERSE)
            if sig_universe not in self._active_universes:
                reason = (
                    f"Universe '{sig_universe}' not active in regime "
                    f"'{self._regime_state}' (active: {self._active_universes})"
                )
                rejected.append((sig, reason))
            else:
                universe_filtered.append(sig)

        reasoning_lines.append(
            f"After universe filter: {len(universe_filtered)} signals "
            f"({len(signals) - len(universe_filtered)} rejected)"
        )

        # ── Step 2: per-universe position limits ──────────────────────────
        # Count existing positions per universe.
        existing_by_universe: dict[str, int] = defaultdict(int)
        for pos in existing_positions:
            u = getattr(pos, "universe", _DEFAULT_UNIVERSE)
            existing_by_universe[u] += 1

        # Group candidate signals by universe; sort by confidence desc.
        by_universe: dict[str, list] = defaultdict(list)
        for sig in universe_filtered:
            u = getattr(sig, "universe", _DEFAULT_UNIVERSE)
            by_universe[u].append(sig)

        limit_filtered: list[Any] = []
        for universe, u_signals in by_universe.items():
            limit = get_limit(universe, overrides=self._universe_limits)
            max_pos = limit["max_positions"]
            max_pct = limit["max_pct_equity"]
            already_open = existing_by_universe.get(universe, 0)
            available_slots = max(0, max_pos - already_open)

            # Sort by confidence descending — best signals fill slots first.
            ranked = sorted(u_signals, key=lambda s: s.confidence, reverse=True)

            accepted_this_universe = 0
            cumulative_value = sum(
                getattr(pos, "position_value", 0.0)
                for pos in existing_positions
                if getattr(pos, "universe", _DEFAULT_UNIVERSE) == universe
            )

            for sig in ranked:
                if accepted_this_universe >= available_slots:
                    reason = (
                        f"Universe '{universe}' position limit reached "
                        f"({max_pos} max, {already_open} existing, "
                        f"{accepted_this_universe} accepted this pass)"
                    )
                    rejected.append((sig, reason))
                    continue

                # Check equity-exposure cap.
                sig_value = getattr(sig, "position_value", 0.0)
                proposed_pct = (cumulative_value + sig_value) / equity if equity > 0 else 0.0
                if proposed_pct > max_pct:
                    reason = (
                        f"Universe '{universe}' equity cap would be exceeded "
                        f"({proposed_pct:.1%} > {max_pct:.1%})"
                    )
                    rejected.append((sig, reason))
                    continue

                limit_filtered.append(sig)
                cumulative_value += sig_value
                accepted_this_universe += 1

            reasoning_lines.append(
                f"  {universe}: {accepted_this_universe} accepted "
                f"(limit={max_pos}, existing={already_open}, "
                f"slots={available_slots})"
            )

        reasoning_lines.append(
            f"After per-universe limits: {len(limit_filtered)} signals"
        )

        # ── Step 3: cross-universe correlation check ──────────────────────
        corr_filtered = check_correlation_conflicts(limit_filtered)
        corr_rejected_ids = {id(s) for s in limit_filtered} - {id(s) for s in corr_filtered}
        for sig in limit_filtered:
            if id(sig) in corr_rejected_ids:
                rejected.append((
                    sig,
                    f"Correlation conflict: ticker '{sig.ticker}' exceeds "
                    "max 2 positions per correlation group"
                ))

        reasoning_lines.append(
            f"After correlation filter: {len(corr_filtered)} signals "
            f"({len(corr_rejected_ids)} rejected)"
        )

        # ── Step 4: overall max_positions cap ─────────────────────────────
        # Sort the remaining signals by confidence; take the top N.
        total_existing = len(existing_positions)
        overall_slots = max(0, self._max_positions - total_existing)
        ranked_final = sorted(corr_filtered, key=lambda s: s.confidence, reverse=True)

        selected: list[Any] = []
        for sig in ranked_final:
            if len(selected) >= overall_slots:
                rejected.append((
                    sig,
                    f"Overall max-positions cap reached "
                    f"({self._max_positions} max, {total_existing} existing, "
                    f"{len(selected)} accepted this pass)"
                ))
            else:
                selected.append(sig)

        reasoning_lines.append(
            f"After overall cap ({self._max_positions} max, "
            f"{total_existing} existing): {len(selected)} signals selected"
        )

        # ── Step 5: apply regime sizing multiplier ────────────────────────
        if self._sizing_multiplier != 1.0:
            for sig in selected:
                self._apply_sizing_multiplier(sig, self._sizing_multiplier)
            reasoning_lines.append(
                f"Sizing multiplier {self._sizing_multiplier:.2f}x applied to "
                f"{len(selected)} signals"
            )

        # ── Step 6: AI overlay (Phase 4 — pass-through) ───────────────────
        if self._overlay:
            selected = self._apply_overlay(selected, rejected, reasoning_lines)

        # ── Step 7: build exposure summary ────────────────────────────────
        universe_exposure = self._build_exposure(selected, equity)

        result = ConstructedPortfolio(
            signals=selected,
            rejected=rejected,
            universe_exposure=universe_exposure,
            regime_state=self._regime_state,
            sizing_multiplier=self._sizing_multiplier,
            total_positions=len(selected),
            reasoning="\n".join(reasoning_lines),
        )
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_sizing_multiplier(sig: Any, multiplier: float) -> None:
        """Scale a signal's position_size and derived values in-place."""
        orig_size = getattr(sig, "position_size", 0)
        new_size = max(1, int(round(orig_size * multiplier)))
        try:
            object.__setattr__(sig, "position_size", new_size)
        except (AttributeError, TypeError):
            # If the signal is a frozen dataclass or doesn't support setattr,
            # log and move on — don't crash construction.
            logger.debug(
                "Could not apply sizing multiplier to signal for %s "
                "(read-only object?)", getattr(sig, "ticker", "?")
            )
            return

        # Recompute position_value and risk_amount if they exist.
        entry = getattr(sig, "entry_price", None)
        stop  = getattr(sig, "stop_price", None)
        if entry is not None:
            try:
                object.__setattr__(sig, "position_value",
                                   round(entry * new_size, 2))
            except (AttributeError, TypeError):
                pass
        if entry is not None and stop is not None:
            try:
                object.__setattr__(sig, "risk_amount",
                                   round(abs(entry - stop) * new_size, 2))
            except (AttributeError, TypeError):
                pass

    def _apply_overlay(
        self,
        selected: list,
        rejected: list,
        reasoning_lines: list,
    ) -> list:
        """Phase 4 AI overlay hook — pass-through implementation.

        In Phase 4 the overlay dict will carry per-ticker ``sizing_scale``
        (float) and ``veto`` (bool) adjustments.  For now we just log that
        an overlay was provided but take no action.
        """
        reasoning_lines.append(
            f"AI overlay provided ({len(self._overlay)} entries) — "
            "Phase 4 not yet active; pass-through applied"
        )
        return selected

    @staticmethod
    def _build_exposure(selected: list, equity: float) -> dict:
        """Compute per-universe exposure from the accepted signal list."""
        by_universe: dict[str, dict] = {}
        for sig in selected:
            u = getattr(sig, "universe", _DEFAULT_UNIVERSE)
            if u not in by_universe:
                by_universe[u] = {"positions": 0, "pct_equity": 0.0,
                                  "_total_value": 0.0}
            by_universe[u]["positions"] += 1
            by_universe[u]["_total_value"] += getattr(sig, "position_value", 0.0)

        exposure = {}
        for u, data in by_universe.items():
            pct = data["_total_value"] / equity if equity > 0 else 0.0
            exposure[u] = {
                "positions": data["positions"],
                "pct_equity": round(pct, 4),
            }
        return exposure
