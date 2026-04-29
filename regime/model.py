"""
regime/model.py — RegimeModel classifier for the Atlas quantitative regime model.

Reads macro indicators, runs indicator scoring functions, and maps the resulting
scores to one of six RegimeState values using a priority-ordered rule set.

Usage
-----
    from regime.model import RegimeModel

    model = RegimeModel()                       # loads config/active/regime.json

    # Classify from a raw indicator dict
    result = model.classify(indicators_dict)
    print(result.state.value, result.scores["composite"])

    # Classify a specific date from the DB
    result = model.classify_date("2024-03-15")

    # Classify the most recent available data and persist to regime_history
    result = model.classify_and_record()
"""
from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from regime.indicators import compute_all_scores
from regime.states import REGIME_CONFIGS, RegimeState

logger = logging.getLogger(__name__)

# Default config path, relative to the atlas project root.
_DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "active" / "regime.json"


# ──────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class RegimeClassification:
    """Full output of a single-day regime classification."""

    state: RegimeState
    scores: dict                    # {"trend": float, "risk": float, ..., "composite": float}
    active_universes: list          # list[str]
    sizing_multiplier: float
    max_positions: int
    enabled_strategies: list        # list[str]
    reasoning: str
    model_version: str
    date: str = ""
    pending_state: Optional[str] = None  # raw unconfirmed regime during N-day gate


# ──────────────────────────────────────────────────────────────────────────────
# RegimeModel
# ──────────────────────────────────────────────────────────────────────────────


class RegimeModel:
    """
    Quantitative regime classifier.

    Applies a priority-ordered rule set to normalised indicator scores
    (each in [-1, +1]) to assign one of six RegimeState labels.

    Parameters
    ----------
    config_path : str or Path, optional
        Path to ``regime.json``.  Defaults to ``config/active/regime.json``
        relative to the atlas project root.
    """

    def __init__(self, config_path=None):
        path = Path(config_path) if config_path is not None else _DEFAULT_CONFIG
        if not path.exists():
            raise FileNotFoundError(f"Regime config not found: {path}")
        with open(path) as fh:
            self._config = json.load(fh)
        self._model_version: str = str(self._config.get("model_version", "v1"))

    # ── Public API ────────────────────────────────────────────────────────────

    def classify(self, indicators: dict) -> RegimeClassification:
        """
        Classify a single day's macro indicators into a regime state.

        Parameters
        ----------
        indicators : dict
            Raw macro indicator values for one date.  See
            ``regime/indicators.py`` for expected keys.

        Returns
        -------
        RegimeClassification
        """
        scores = compute_all_scores(indicators, self._config)
        state = self._apply_rules(scores, recent_was_bear=None)
        return self._build_result(state, scores, date="")

    def classify_date(self, date: str) -> RegimeClassification:
        """
        Classify a specific date by reading from the ``macro_indicators`` table.

        Parameters
        ----------
        date : str
            ISO date string, e.g. ``"2024-03-15"``.

        Returns
        -------
        RegimeClassification

        Raises
        ------
        ValueError
            If no macro indicator row exists for *date*.
        """
        from db.atlas_db import get_macro_indicators

        rows = get_macro_indicators(start_date=date, end_date=date)
        if not rows:
            raise ValueError(f"No macro indicators found for date: {date}")
        indicators = rows[0]

        # Check recent regime history for recovery detection.
        recent_was_bear = self._check_recent_bear(lookback_days=25, anchor_date=date)

        scores = compute_all_scores(indicators, self._config)
        state = self._apply_rules(scores, recent_was_bear=recent_was_bear)
        return self._build_result(state, scores, date=date)

    def classify_current(self) -> RegimeClassification:
        """
        Classify the most recent available date in ``macro_indicators``.

        Returns
        -------
        RegimeClassification

        Raises
        ------
        ValueError
            If the ``macro_indicators`` table is empty.
        """
        from db.atlas_db import get_macro_indicators

        rows = get_macro_indicators()
        if not rows:
            raise ValueError("No macro indicators in database — run ingest first.")

        # get_macro_indicators returns ASC; last row is most recent.
        latest = rows[-1]
        date = latest.get("date", "")

        recent_was_bear = self._check_recent_bear(lookback_days=25)
        scores = compute_all_scores(latest, self._config)
        state = self._apply_rules(scores, recent_was_bear=recent_was_bear)
        return self._build_result(state, scores, date=date)

    def classify_and_record(self, date: str = None) -> RegimeClassification:
        """
        Classify and write the result to the ``regime_history`` table.

        When ``regime_confirmation_days`` in the config is >= 2, the regime
        state is only flipped after N consecutive same-state raw classifications.
        While pending, ``regime_history.pending_state`` carries the raw state
        and ``regime_history.regime_state`` retains the previously confirmed
        state.  ``RegimeClassification.pending_state`` mirrors this on the
        return value.

        Parameters
        ----------
        date : str, optional
            ISO date string.  If *None*, uses the most recent available data.

        Returns
        -------
        RegimeClassification
        """
        from db.atlas_db import record_regime

        if date is None:
            result = self.classify_current()
            effective_date = result.date
        else:
            result = self.classify_date(date)
            effective_date = date

        # ── Confirmation gate ──────────────────────────────────────────────
        confirmation_days = int(self._config.get("regime_confirmation_days", 1))
        if confirmation_days >= 2:
            confirmed_state, pending_state_str = self._apply_confirmation_gate(
                raw=result,
                effective_date=effective_date,
                confirmation_days=confirmation_days,
            )
        else:
            # Default path — instant flip, no confirmation (default OFF).
            confirmed_state = result.state
            pending_state_str = None

        record_regime(
            date=effective_date,
            state=confirmed_state.value,
            trend_score=result.scores["trend"],
            risk_score=result.scores["risk"],
            active_universes=result.active_universes,
            sizing_multiplier=result.sizing_multiplier,
            reasoning=result.reasoning,
            enabled_strategies=result.enabled_strategies,
            model_version=result.model_version,
            pending_state=pending_state_str,
        )

        # Reflect the (possibly gated) outcome back to the caller.
        result.state = confirmed_state
        result.pending_state = pending_state_str
        return result

    # ── Classification rules ──────────────────────────────────────────────────

    def _apply_rules(
        self,
        scores: dict,
        recent_was_bear: Optional[bool],
    ) -> RegimeState:
        """
        Apply priority-ordered classification rules to *scores*.

        Parameters
        ----------
        scores : dict
            Output of ``compute_all_scores()``.
        recent_was_bear : bool or None
            True if regime_history shows a bear period in the last 20 days.
            None means history is unavailable (single-date classify path);
            the rule falls back to a mixed-signal heuristic.

        Returns
        -------
        RegimeState
        """
        composite = scores["composite"]
        trend     = scores["trend"]
        risk      = scores["risk"]
        credit    = scores["credit"]

        # Rule 1 — bear_capitulation: extreme stress in VIX + credit.
        # Threshold relaxed from < -0.6 to <= -0.5: trend_score is bounded by
        # the above/below-200DMA weight, so -0.6 was unreachable in practice.
        if composite <= -0.5 and (risk < -0.7 or credit < -0.7):
            return RegimeState.BEAR_CAPITULATION

        # Rule 2 — bear_risk_off: trend broken, risk elevated.
        # Two paths to bear_risk_off:
        #   (a) composite clearly negative + trend negative (original rule)
        #   (b) trend strongly negative + any risk present (catches 2022-style
        #       bear where VIX term structure stays in contango, muting the
        #       composite but SPY is well below 200 DMA for months)
        if (composite <= -0.25 and trend <= -0.25) or (
            trend <= -0.35 and risk <= -0.05
        ):
            return RegimeState.BEAR_RISK_OFF

        # Rule 3 — recovery_early: trend turning positive after a bear period.
        if trend > 0.0:
            if recent_was_bear is None:
                # No history available — use mixed-signal proxy:
                # positive trend but at least one risk indicator still negative.
                if risk < 0.0 or credit < 0.0:
                    return RegimeState.RECOVERY_EARLY
            elif recent_was_bear:
                return RegimeState.RECOVERY_EARLY

        # Rule 4 — bull_risk_off: trend up but risk/credit hedging.
        if trend > 0.2 and (risk < -0.2 or credit < -0.2):
            return RegimeState.BULL_RISK_OFF

        # Rule 5 — transition_uncertain: signal conflict, no clear direction.
        if abs(composite) < 0.15:
            return RegimeState.TRANSITION_UNCERTAIN

        # Rule 6 — bull_risk_on: confirmed bull — trend up, composite positive.
        if composite > 0.2 and trend > 0.2:
            return RegimeState.BULL_RISK_ON

        # Fallback (should rarely trigger).
        return RegimeState.TRANSITION_UNCERTAIN

    # ── Result builder ────────────────────────────────────────────────────────

    def _build_result(
        self,
        state: RegimeState,
        scores: dict,
        date: str,
    ) -> RegimeClassification:
        """Assemble a RegimeClassification from state + scores."""
        cfg = REGIME_CONFIGS[state]
        active_universes: list = list(cfg["active_universes"])
        sizing_multiplier: float = float(cfg["sizing_multiplier"])
        max_positions: int = int(cfg["max_positions"])
        strategy_types: list = list(cfg["strategy_types"])

        reasoning = self._build_reasoning(state, scores)

        return RegimeClassification(
            state=state,
            scores=scores,
            active_universes=active_universes,
            sizing_multiplier=sizing_multiplier,
            max_positions=max_positions,
            enabled_strategies=strategy_types,
            reasoning=reasoning,
            model_version=self._model_version,
            date=date,
        )

    @staticmethod
    def _build_reasoning(state: RegimeState, scores: dict) -> str:
        """
        Build a human-readable reasoning string for the classification.

        Example output::
            bull_risk_on: SPY above 200 DMA (trend +0.85), VIX low (risk +0.72),
            credit tight (credit +0.80), yield curve normal (yield_curve +0.60).
            Composite: +0.71

        Returns
        -------
        str
        """
        trend     = scores["trend"]
        risk      = scores["risk"]
        credit    = scores["credit"]
        yield_c   = scores["yield_curve"]
        composite = scores["composite"]

        # Trend description.
        if trend > 0.3:
            trend_desc = f"SPY above 200 DMA (trend {trend:+.2f})"
        elif trend < -0.3:
            trend_desc = f"SPY below 200 DMA (trend {trend:+.2f})"
        else:
            trend_desc = f"SPY near 200 DMA (trend {trend:+.2f})"

        # Risk description.
        if risk > 0.3:
            risk_desc = f"VIX low/calm (risk {risk:+.2f})"
        elif risk < -0.3:
            risk_desc = f"VIX elevated/spiking (risk {risk:+.2f})"
        else:
            risk_desc = f"VIX moderate (risk {risk:+.2f})"

        # Credit description.
        if credit > 0.3:
            credit_desc = f"credit tight (credit {credit:+.2f})"
        elif credit < -0.3:
            credit_desc = f"credit blowing out (credit {credit:+.2f})"
        else:
            credit_desc = f"credit moderate (credit {credit:+.2f})"

        # Yield curve description.
        if yield_c > 0.2:
            yc_desc = f"yield curve normal ({yield_c:+.2f})"
        elif yield_c < -0.2:
            yc_desc = f"yield curve inverted ({yield_c:+.2f})"
        else:
            yc_desc = f"yield curve flat ({yield_c:+.2f})"

        return (
            f"{state.value}: {trend_desc}, {risk_desc}, {credit_desc}, "
            f"{yc_desc}. Composite: {composite:+.2f}"
        )

    # ── Confirmation gate ─────────────────────────────────────────────────────

    def _apply_confirmation_gate(
        self,
        raw: "RegimeClassification",
        effective_date: str,
        confirmation_days: int,
    ) -> tuple:
        """
        Apply the N-day consecutive confirmation gate.

        A regime change is only applied after *confirmation_days* consecutive
        daily raw classifications agree on the new state.  While the streak is
        still building, the DB row records:
          - ``regime_state``  = currently confirmed (stable) state
          - ``pending_state`` = the incoming raw state (not yet confirmed)

        Parameters
        ----------
        raw : RegimeClassification
            Today's instant (unconfirmed) classification.
        effective_date : str
            ISO date for today's record (used as upper bound for history query).
        confirmation_days : int
            Number of consecutive same-state raw classifications required (>= 2).

        Returns
        -------
        (confirmed_state: RegimeState, pending_state_str: str | None)
            confirmed_state  — the state to persist as ``regime_state``.
            pending_state_str — the raw state string to store in ``pending_state``
                                (None when no change is pending).
        """
        from db.atlas_db import get_db

        # ── Step 1: current confirmed regime from most recent DB row ──────
        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT regime_state FROM regime_history "
                    "WHERE date < ? ORDER BY date DESC LIMIT 1",
                    (effective_date,),
                ).fetchone()
        except Exception:
            row = None

        if row is None:
            # No prior history — accept raw immediately (first-ever record).
            return raw.state, None

        try:
            confirmed_state = RegimeState(row["regime_state"])
        except ValueError:
            return raw.state, None

        # ── Step 2: no change — raw already equals confirmed ──────────────
        if raw.state == confirmed_state:
            return raw.state, None

        # ── Step 3: load (N-1) most recent historical raw states ──────────
        # A historical day's "raw" classification = pending_state if set,
        # else regime_state (meaning raw matched confirmed that day).
        needed = confirmation_days - 1
        try:
            with get_db() as conn:
                hist_rows = conn.execute(
                    "SELECT regime_state, pending_state FROM regime_history "
                    "WHERE date < ? ORDER BY date DESC LIMIT ?",
                    (effective_date, needed),
                ).fetchall()
        except Exception:
            hist_rows = []

        historical_raw: list[str] = [
            (r["pending_state"] if r["pending_state"] else r["regime_state"])
            for r in hist_rows
        ]

        # ── Step 4: confirm or hold ───────────────────────────────────────
        all_agree = (
            len(historical_raw) >= needed
            and all(s == raw.state.value for s in historical_raw)
        )

        if all_agree:
            logger.info(
                "REGIME_CONFIRMED: %s (after %d consecutive days)",
                raw.state.value,
                confirmation_days,
            )
            return raw.state, None
        else:
            logger.info(
                "REGIME_PENDING: raw=%s confirmed=%s "
                "(need %d consecutive, have %d/%d)",
                raw.state.value,
                confirmed_state.value,
                confirmation_days,
                len(historical_raw) + 1,
                confirmation_days,
            )
            return confirmed_state, raw.state.value

    # ── History helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _check_recent_bear(
        lookback_days: int = 25,
        anchor_date: Optional[str] = None,
    ) -> Optional[bool]:
        """
        Return True if any entry in regime_history within the *lookback_days*
        window before *anchor_date* (or today) was a bear state, False if
        history is accessible but no bear period was found, or None if the DB
        is unavailable (triggering the no-history heuristic fallback).

        Used by the recovery_early rule to detect "crossing back above 200 DMA
        after a bear period".

        Parameters
        ----------
        lookback_days : int
            How many calendar days to look back.
        anchor_date : str or None
            ISO date to look back from.  None → use today (for classify_current).
        """
        try:
            from db.atlas_db import get_db

            # Build the start_date for the lookback window.
            if anchor_date:
                # Use SQLite date arithmetic relative to the anchor date.
                with get_db() as conn:
                    row = conn.execute(
                        "SELECT date(?, ?) AS d",
                        (anchor_date, f"-{lookback_days} days"),
                    ).fetchone()
                    start_date = row["d"]
            else:
                # Relative to today.
                with get_db() as conn:
                    row = conn.execute(
                        "SELECT date('now', ?) AS d",
                        (f"-{lookback_days} days",),
                    ).fetchone()
                    start_date = row["d"]

            # Upper bound: use anchor_date (or today) to avoid reading future
            # regime entries during sequential backfill.
            end_date = anchor_date or datetime.date.today().isoformat()

            with get_db() as conn:
                rows = conn.execute(
                    "SELECT regime_state FROM regime_history WHERE date >= ? AND date < ? ORDER BY date DESC",
                    (start_date, end_date),
                ).fetchall()

            bear_states = {
                RegimeState.BEAR_CAPITULATION.value,
                RegimeState.BEAR_RISK_OFF.value,
            }
            # Empty history: DB accessible, no bear period — return False.
            return any(row["regime_state"] in bear_states for row in rows)
        except Exception:
            # DB unavailable — fall back to no-history heuristic.
            return None
