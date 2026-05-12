"""Research integrity helpers — detect portfolio-contaminated research_best files.

A research_best file is considered *contaminated* when the headline backtest
metrics (Sharpe, CAGR, etc.) are dominated by strategies other than the named
strategy.  This happens when a multi-strategy portfolio backtest is used as the
basis for a solo-strategy evaluation.

Contamination threshold: solo_fraction < 0.50 (less than half the trades are
from the target strategy).

Usage::

    from research.integrity import check_solo, assert_solo_or_raise

    is_solo, frac, note = check_solo("connors_rsi2")
    # is_solo=False, frac=0.11, note="Headline metrics are portfolio-contaminated..."

    assert_solo_or_raise("connors_rsi2")  # raises ValueError
    assert_solo_or_raise("momentum_breakout")  # passes silently
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

ATLAS_ROOT = Path(__file__).resolve().parent.parent
BEST_DIR = ATLAS_ROOT / "research" / "best"


def _safe_load(path: Path) -> dict:
    """Load JSON, tolerating Python-invalid NaN / Infinity literals."""
    text = path.read_text()
    text = text.replace(": NaN", ": null").replace(":NaN", ":null")
    text = text.replace(": Infinity", ": null").replace(":Infinity", ":null")
    text = text.replace(": -Infinity", ": null").replace(":-Infinity", ":null")
    return json.loads(text)


def check_solo(
    strategy: str, universe: str = "sp500"
) -> Tuple[Optional[bool], Optional[float], Optional[str]]:
    """Return (is_solo, solo_fraction, contamination_note) for a research_best file.

    Looks up ``research/best/{strategy}_{universe}.json`` for non-sp500 universes,
    or ``research/best/{strategy}.json`` for sp500.

    Returns:
        - ``(True, fraction, None)``  — strategy dominates (>= 50% of trades)
        - ``(False, fraction, note)`` — portfolio-contaminated (< 50% solo trades)
        - ``(None, None, note)``      — no file found or no trades recorded

    The ``is_solo`` field is read directly from the JSON file (written by
    ``scripts/audit_promotion_integrity.py``).  If the field is missing, the
    function recomputes it from the raw breakdown data so it is safe to call
    before the enrichment pass runs.
    """
    # Resolve file path
    best_path: Optional[Path] = None
    if universe and universe != "sp500":
        candidate = BEST_DIR / f"{strategy}_{universe}.json"
        if candidate.exists():
            best_path = candidate
    if best_path is None:
        candidate = BEST_DIR / f"{strategy}.json"
        if candidate.exists():
            best_path = candidate
    if best_path is None:
        # Last-resort: {strategy}_{universe}.json even for sp500
        candidate = BEST_DIR / f"{strategy}_{universe}.json"
        if candidate.exists():
            best_path = candidate

    if best_path is None:
        return (None, None, "no_file")

    try:
        data = _safe_load(best_path)
    except Exception as exc:
        logger.warning("check_solo: failed to load %s: %s", best_path, exc)
        return (None, None, f"load_error: {exc}")

    # Prefer pre-computed fields (written by audit_promotion_integrity.py)
    if "is_solo" in data:
        is_solo: Optional[bool] = data["is_solo"]
        solo_frac: Optional[float] = data.get("solo_fraction")
        note: Optional[str] = data.get("contamination_note")
        return (is_solo, solo_frac, note)

    # Fallback: compute on the fly
    metrics = data.get("metrics", {})
    total_trades: int = metrics.get("total_trades") or 0
    bd: Optional[dict] = metrics.get("strategy_breakdown")

    if not bd or total_trades == 0:
        return (None, None, "No trades recorded — backtest not yet run or no signals fired.")

    solo_trades: int = (bd.get(strategy) or {}).get("trades", 0)
    frac: float = solo_trades / total_trades

    if frac >= 0.50:
        return (True, round(frac, 4), None)

    others = {k: (v.get("trades") or 0) for k, v in bd.items() if k != strategy}
    if others:
        dom_name, dom_trades = max(others.items(), key=lambda x: x[1])
        dom_pct = round((dom_trades / total_trades) * 100, 1)
    else:
        dom_name, dom_pct = "unknown", 0.0

    note = (
        f"Headline metrics are portfolio-contaminated. "
        f"Dominant strategy: {dom_name} ({dom_pct}%). "
        f"True solo performance unknown — see task #327."
    )
    return (False, round(frac, 4), note)


def assert_solo_or_raise(strategy: str, universe: str = "sp500") -> None:
    """Raise ValueError if the research_best file is contaminated (is_solo=False).

    This is a hard gate for promotion sweeps.  Files with is_solo=None (zero
    trades / not yet run) are silently allowed through — they will fail the
    delta-Sharpe gate in the promoter anyway.

    Args:
        strategy: Strategy name (e.g. "connors_rsi2").
        universe: Universe name (e.g. "sp500", "commodity_etfs").

    Raises:
        ValueError: If the file exists and is_solo is explicitly False.
    """
    is_solo, solo_frac, note = check_solo(strategy, universe)
    if is_solo is False:
        raise ValueError(
            f"Research best for {strategy}/{universe} is portfolio-contaminated "
            f"(solo_fraction={solo_frac:.2%}). "
            f"Run a true solo backtest before promoting. {note or ''}"
        )
