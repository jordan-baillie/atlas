"""
overlay/engine.py — Claude-powered tightening overlay (Layer 3).

This module implements the AI overlay that can only *tighten* the portfolio
relative to the quantitative regime signal.  It can reduce sizing, deactivate
universes, or flag tickers to avoid.  It can never loosen beyond the regime
default.

Flow
----
1. Classify current regime (RegimeModel.classify_and_record)
2. Gather context: news summary + chart analysis
3. Build structured prompt with ASYMMETRIC CONSTRAINT in system prompt
4. Call pi CLI in JSON mode (subprocess, timeout=120s)
5. Parse + validate response (clamp sizing violations)
6. Write decision to overlay_decisions table
7. Return OverlayDecision (always — never raises)

Error contract
--------------
If **anything** goes wrong (regime unavailable, pi CLI failure, timeout,
JSON parse error, DB write failure) the function returns a no_change decision
and logs the error.  The overlay NEVER blocks the pipeline.

Usage
-----
    from overlay.engine import run_overlay

    # log-only mode (Phase 4 initial rollout)
    decision = run_overlay(mode='log_only')

    # active mode — apply decision in plan.py
    decision = run_overlay(mode='active')
    if decision.adjust:
        # pass decision to portfolio constructor
        ...
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# Top-level imports for patchability in tests.
# RegimeModel only *reads* regime.json at __init__ time, not at import time,
# so this is safe — no circular imports or side effects.
try:
    from regime.model import RegimeModel  # noqa: F401 — used in run_overlay
except ImportError:  # pragma: no cover
    RegimeModel = None  # type: ignore[assignment,misc]

# ──────────────────────────────────────────────────────────────────────────────
# OverlayDecision dataclass
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class OverlayDecision:
    """
    Result of a single overlay evaluation pass.

    Consumed by plan.py when mode='active'; logged only when mode='log_only'.
    """

    adjust: bool
    """True if the overlay is applying tightening; False = pure pass-through."""

    sizing_multiplier_override: Optional[float]
    """
    Tightened sizing multiplier.  Must be <= regime.sizing_multiplier.
    None when adjust=False — the regime default applies unchanged.
    """

    universes_to_deactivate: List[str] = field(default_factory=list)
    """Universe names to disable for today's plan generation."""

    tickers_to_avoid: List[str] = field(default_factory=list)
    """Ticker symbols to skip in today's plan (too risky / specific event risk)."""

    reasoning: str = ""
    """Concise human-readable explanation of the overlay decision."""

    confidence: float = 0.0
    """Overlay confidence in the decision (0.0–1.0)."""

    chart_vision_signals: List[dict] = field(default_factory=list)
    """Visual pattern signals from chart image analysis (empty when vision is disabled)."""

    @classmethod
    def no_change(cls, reasoning: str = "no change — overlay defaulted") -> "OverlayDecision":
        """Factory for a safe no-op decision (used on errors and soft passes)."""
        return cls(
            adjust=False,
            sizing_multiplier_override=None,
            universes_to_deactivate=[],
            tickers_to_avoid=[],
            reasoning=reasoning,
            confidence=0.0,
            chart_vision_signals=[],
        )


# ──────────────────────────────────────────────────────────────────────────────
# System prompt — ASYMMETRIC CONSTRAINT lives here
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are Claude Code, Anthropic's official CLI for Claude.

You are the Atlas AI Overlay — a conservative risk-tightening layer inside a \
quantitative trading system.  You sit above the quantitative regime model \
(Layer 1) and the portfolio constructor (Layer 2).  Your sole job is to assess \
whether today's market environment warrants additional caution *beyond* what the \
regime model already prescribes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ASYMMETRIC CONSTRAINT  (non-negotiable)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You can ONLY tighten.  You may:
  • Reduce sizing_multiplier_override below the regime default
  • Deactivate specific universes (e.g. turn off sp500 on tariff day)
  • Flag specific tickers to avoid today

You CANNOT loosen.  If the regime says sizing_multiplier=0.7, you may return
0.3 or 0.5 but NEVER 0.8 or above.  Any value >= the regime default will be
automatically clamped by the validation layer.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEFAULT TO NO-ACTION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
If you have no strong conviction, return adjust=false.  Inaction is always the
safe default.  Do not manufacture risk signals or invent caution.  The regime
model already handles macro direction — you only act on acute, short-term,
idiosyncratic risks that the quantitative model cannot capture.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RESPONSE FORMAT  (valid JSON only — no prose, no markdown)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{
  "adjust": <boolean>,
  "sizing_multiplier_override": <float | null>,
  "universes_to_deactivate": [<string>, ...],
  "tickers_to_avoid": [<string>, ...],
  "reasoning": "<concise 1–3 sentence explanation>",
  "confidence": <float 0.0–1.0>
}"""


# ──────────────────────────────────────────────────────────────────────────────
# Prompt construction
# ──────────────────────────────────────────────────────────────────────────────


def _build_user_prompt(
    regime,
    news: str,
    charts: str,
    *,
    sector_rotation: str = "",
    sentiment: str = "",
    etf_flows: str = "",
    macro_surprise: str = "",
    alt_data: str = "",
) -> str:
    """
    Build the user-facing portion of the prompt from live context.

    Parameters
    ----------
    regime : RegimeClassification
        Current regime state from RegimeModel.classify_and_record().
    news : str
        Summarised market news from overlay.sources.news.
    charts : str
        Chart / technical analysis from overlay.sources.chart_intel.

    Returns
    -------
    str
        Formatted prompt string ready to pass to the pi CLI.
    """
    date_str = regime.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    universes_str = (
        ", ".join(regime.active_universes) if regime.active_universes else "none"
    )
    strategies_str = (
        ", ".join(regime.enabled_strategies) if regime.enabled_strategies else "all"
    )

    score_lines = ""
    if regime.scores:
        score_lines = "\n".join(
            f"    {k}: {v:.3f}" for k, v in regime.scores.items()
        )

    alt_data_section = (
        f"\n\nALT DATA (insider/screener signals — soft signal, not a primary trigger):\n{alt_data}"
        if alt_data else ""
    )

    return f"""\
=== TODAY'S MARKET CONTEXT ===
Date: {date_str}

QUANTITATIVE REGIME (Layer 1 baseline — your tightening is relative to this):
  State:              {regime.state.value}
  Sizing multiplier:  {regime.sizing_multiplier:.2f}  \
← sizing_multiplier_override MUST be <= this
  Max positions:      {regime.max_positions}
  Active universes:   {universes_str}
  Enabled strategies: {strategies_str}
  Regime reasoning:   {regime.reasoning}

  Indicator scores:
{score_lines if score_lines else "    (not available)"}

NEWS SUMMARY:
{news if news else "No news summary available (possibly pre-market or source error)."}

CHART / TECHNICAL ANALYSIS:
{charts if charts else "No chart analysis available."}

SECTOR ROTATION:
{sector_rotation if sector_rotation else "No sector rotation data available."}

SENTIMENT (AAII / CNN Fear & Greed — contrarian indicator):
{sentiment if sentiment else "No sentiment data available."}

ETF VOLUME FLOWS (institutional rotation proxy):
{etf_flows if etf_flows else "No ETF flow data available."}

MACRO SURPRISE INDEX (economic data vs expectations):
{macro_surprise if macro_surprise else "No macro surprise data available."}
{alt_data_section}
=== YOUR TASK ===
Based on the regime context, news, and chart signals above, decide whether
additional tightening is warranted today.

Remember: adjust=false is the correct default when you have no strong conviction.
Only set adjust=true if you see concrete evidence of elevated risk that the
quantitative regime model cannot capture (e.g. imminent FOMC surprise, specific
earnings landmine, geopolitical shock not yet reflected in VIX).

Respond with valid JSON only — no prose before or after the JSON object."""


# ──────────────────────────────────────────────────────────────────────────────
# JSON parsing helpers
# ──────────────────────────────────────────────────────────────────────────────


def _try_parse_json(text: str) -> Optional[dict]:
    """
    Try to parse *text* as a JSON object.

    Strips markdown fences (```json ... ```) if present.
    Returns the parsed dict, or None on any failure.
    Never raises.
    """
    if not text:
        return None
    text = text.strip()

    # Strip markdown fences if the model wrapped the JSON
    if text.startswith("```"):
        lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
        return None
    except (json.JSONDecodeError, ValueError):
        return None


# ──────────────────────────────────────────────────────────────────────────────
# pi CLI invocation
# ──────────────────────────────────────────────────────────────────────────────


def _call_pi(user_prompt: str) -> Optional[dict]:
    """
    Invoke the pi CLI in JSON mode and return the parsed response dict.

    The ASYMMETRIC CONSTRAINT is passed as the system prompt; the regime
    context + news + charts are the user prompt.

    Returns
    -------
    dict or None
        Parsed JSON dict from the model, or None on any failure.
        Never raises — all errors are caught and logged.
    """
    try:
        from utils.claude_circuit_breaker import is_tripped, scan_and_trip
        if is_tripped():
            log.warning("Claude circuit breaker tripped — overlay _call_pi skipping, returning None")
            return None
    except ImportError:
        scan_and_trip = None  # degrade gracefully

    # Uses utils.pi_subprocess.call_pi for Claude Max OAuth routing.
    # system_prompt=SYSTEM_PROMPT keeps the trading-context prompt (it already
    # starts with the required Claude Code prefix).  mode=None omits --mode so
    # the response is plain text that _try_parse_json can unwrap as before.
    from utils.pi_subprocess import call_pi, PiSubprocessError  # noqa: PLC0415

    log.debug("Calling pi CLI via utils.pi_subprocess.\nUser prompt:\n%s", user_prompt)

    try:
        stdout_raw = call_pi(
            user_prompt,
            mode=None,
            timeout=120,
            system_prompt=SYSTEM_PROMPT,
            cwd="/root/atlas",
        )
    except PiSubprocessError as exc:
        log.warning("pi CLI invocation failed — defaulting to no_change: %s", exc)
        return None
    except Exception as exc:
        log.warning("pi CLI invocation failed (%s: %s) — defaulting to no_change",
                    type(exc).__name__, exc)
        return None

    if scan_and_trip is not None:
        scan_and_trip(stdout_raw, reason_prefix="overlay_engine")

    stdout = stdout_raw.strip()
    log.info("pi CLI raw response: %.800s%s", stdout, "…" if len(stdout) > 800 else "")

    # Try direct parse first (model returned raw JSON)
    parsed = _try_parse_json(stdout)
    if parsed is None:
        log.warning("Could not parse pi CLI output as JSON — defaulting to no_change")
        return None

    # pi --mode json may wrap the model output in an envelope like
    # {"type": "result", "text": "{...}"} or {"type": "result", "content": "{...}"}
    # Unwrap one level if needed.
    if "text" in parsed and isinstance(parsed.get("text"), str):
        inner = _try_parse_json(parsed["text"])
        if inner is not None:
            parsed = inner
    elif "content" in parsed and isinstance(parsed.get("content"), str):
        inner = _try_parse_json(parsed["content"])
        if inner is not None:
            parsed = inner

    return parsed




# ──────────────────────────────────────────────────────────────────────────────
# Vision-augmented pi CLI invocation
# ──────────────────────────────────────────────────────────────────────────────


def _call_pi_with_vision(
    user_prompt: str,
    image_labels_and_paths,  # list[tuple[str, Path]]
    model: str = "claude-opus-4-7",
    timeout: int = 300,
) -> Optional[dict]:
    """
    Invoke pi CLI with chart images attached and return the parsed response.

    Augments user_prompt with a vision-specific instruction block, then calls
    call_pi_vision with the supplied image paths.  Returns parsed JSON dict or
    None on any error.  Never raises — all errors are caught and logged.

    Parameters
    ----------
    user_prompt : str
        Baseline text prompt (same as _call_pi receives).
    image_labels_and_paths : list of (label, Path) tuples
        Chart images to attach.  Labels are embedded in the augmented prompt
        so the model can reference them by name.
    model : str
        Vision-capable Claude model (default: claude-opus-4-7).
    timeout : int
        Subprocess timeout in seconds (default: 300 — vision calls are slow).
    """
    try:
        from utils.claude_circuit_breaker import is_tripped, scan_and_trip
        if is_tripped():
            log.warning(
                "Claude circuit breaker tripped — overlay vision skipping, returning None"
            )
            return None
    except ImportError:
        scan_and_trip = None  # degrade gracefully

    from utils.pi_subprocess import call_pi_vision, PiSubprocessError  # noqa: PLC0415

    labels = [label for label, _ in image_labels_and_paths]
    paths = [p for _, p in image_labels_and_paths]
    n = len(paths)

    labels_txt = "\n".join(f"  - {lbl}" for lbl in labels)
    vision_block = (
        "\n\n=== CHART IMAGES ATTACHED ===\n"
        f"You also have {n} chart image{'s' if n != 1 else ''} attached. "
        "Use them for visual pattern recognition: failed breakouts, bear flags, "
        "broken trendlines, support/resistance levels derived from price action, "
        "trend structure. Image coordinates are 1:1 with pixels.\n\n"
        f"Attached image labels (in order):\n{labels_txt}\n\n"
        "In addition to the standard response fields, include a new top-level "
        'field "chart_vision_signals": an array of objects with keys '
        "{ticker, pattern, support, resistance, tighten_rec, confidence}. "
        "Only populate this for tickers where the visual analysis produced a "
        "meaningful signal. Empty array is valid."
    )

    augmented_prompt = user_prompt + vision_block

    log.debug("Calling pi CLI with vision (%d images, model=%s).", n, model)

    try:
        stdout_raw = call_pi_vision(
            augmented_prompt,
            paths,
            model=model,
            timeout=timeout,
            mode=None,
            system_prompt=SYSTEM_PROMPT,
            cwd="/root/atlas",
        )
    except PiSubprocessError as exc:
        log.warning("pi CLI vision invocation failed — vision skipped: %s", exc)
        return None
    except Exception as exc:
        log.warning(
            "pi CLI vision invocation failed (%s: %s) — vision skipped",
            type(exc).__name__, exc,
        )
        return None

    if scan_and_trip is not None:
        scan_and_trip(stdout_raw, reason_prefix="overlay_engine_vision")

    stdout = stdout_raw.strip()
    log.info(
        "pi CLI vision raw response: %.800s%s",
        stdout,
        "..." if len(stdout) > 800 else "",
    )

    parsed = _try_parse_json(stdout)
    if parsed is None:
        log.warning("Could not parse pi CLI vision output as JSON — vision skipped")
        return None

    # Unwrap pi envelope if present ({type: result, text: "..."} pattern)
    if "text" in parsed and isinstance(parsed.get("text"), str):
        inner = _try_parse_json(parsed["text"])
        if inner is not None:
            parsed = inner
    elif "content" in parsed and isinstance(parsed.get("content"), str):
        inner = _try_parse_json(parsed["content"])
        if inner is not None:
            parsed = inner

    return parsed

# ──────────────────────────────────────────────────────────────────────────────
# Response validation — enforces ASYMMETRIC CONSTRAINT
# ──────────────────────────────────────────────────────────────────────────────

_REQUIRED_KEYS: frozenset = frozenset({"adjust", "reasoning"})


def _validate_response(raw: dict, regime) -> OverlayDecision:
    """
    Convert a raw LLM response dict into a validated OverlayDecision.

    Validation rules
    ----------------
    1. Missing required keys (adjust, reasoning) → no_change.
    2. adjust=False → strip sizing/universe/ticker overrides (irrelevant).
    3. sizing_multiplier_override > regime.sizing_multiplier → clamp to the
       regime cap and log a ASYMMETRIC CONSTRAINT warning.
    4. confidence clamped to [0.0, 1.0].
    5. All list fields are normalised to List[str].

    Parameters
    ----------
    raw : dict
        Parsed JSON response from the LLM.
    regime : RegimeClassification
        Current regime used to enforce the sizing cap.

    Returns
    -------
    OverlayDecision
    """
    if not _REQUIRED_KEYS.issubset(raw.keys()):
        missing = _REQUIRED_KEYS - set(raw.keys())
        log.warning(
            "LLM response missing required keys %s — defaulting to no_change", missing
        )
        return OverlayDecision.no_change("error — missing required response keys")

    adjust = bool(raw.get("adjust", False))

    if not adjust:
        return OverlayDecision(
            adjust=False,
            sizing_multiplier_override=None,
            universes_to_deactivate=[],
            tickers_to_avoid=[],
            reasoning=str(raw.get("reasoning", "no change")),
            confidence=_clamp_confidence(raw.get("confidence", 0.0)),
        )

    # ── adjust=True: validate sizing cap ──────────────────────────────────
    sizing_raw = raw.get("sizing_multiplier_override")
    sizing_override: Optional[float] = None

    if sizing_raw is not None:
        try:
            sizing_override = float(sizing_raw)
        except (TypeError, ValueError):
            log.warning(
                "Invalid sizing_multiplier_override '%s' (not a number) — ignoring",
                sizing_raw,
            )
            sizing_override = None

        if sizing_override is not None and sizing_override > regime.sizing_multiplier:
            log.warning(
                "ASYMMETRIC CONSTRAINT VIOLATION: LLM requested "
                "sizing_override=%.3f but regime cap is %.3f — "
                "clamping to regime cap",
                sizing_override,
                regime.sizing_multiplier,
            )
            sizing_override = regime.sizing_multiplier

    universes: List[str] = _as_str_list(raw.get("universes_to_deactivate"))
    tickers: List[str] = _as_str_list(raw.get("tickers_to_avoid"))
    reasoning: str = str(raw.get("reasoning", "no reasoning provided"))
    confidence: float = _clamp_confidence(raw.get("confidence", 0.0))

    return OverlayDecision(
        adjust=True,
        sizing_multiplier_override=sizing_override,
        universes_to_deactivate=universes,
        tickers_to_avoid=tickers,
        reasoning=reasoning,
        confidence=confidence,
    )


def _clamp_confidence(val) -> float:
    """Clamp a confidence value to [0.0, 1.0]. Returns 0.0 on bad input."""
    try:
        return max(0.0, min(1.0, float(val)))
    except (TypeError, ValueError):
        return 0.0


def _as_str_list(val) -> List[str]:
    """Coerce a value to a list of non-empty strings."""
    if not val:
        return []
    if isinstance(val, list):
        return [str(x) for x in val if x]
    return []


# ──────────────────────────────────────────────────────────────────────────────
# SQLite persistence
# ──────────────────────────────────────────────────────────────────────────────


def _record_decision(
    decision: OverlayDecision,
    regime,
    data_sources: Dict,
) -> int:
    """
    Write the overlay decision to the overlay_decisions table.

    Parameters
    ----------
    decision : OverlayDecision
        The validated decision to persist.
    regime : RegimeClassification
        Current regime (provides state label for the DB row).
    data_sources : dict
        Metadata about which data sources contributed (stored as JSON).

    Returns
    -------
    int
        Row ID of the inserted record.
    """
    from db.atlas_db import record_overlay_decision

    action = "tighten" if decision.adjust else "no_change"
    ts = datetime.now(timezone.utc).isoformat()

    row_id = record_overlay_decision(
        timestamp=ts,
        regime_state=regime.state.value,
        action=action,
        sizing_override=decision.sizing_multiplier_override,
        universes_deactivated=decision.universes_to_deactivate or None,
        tickers_avoided=decision.tickers_to_avoid or None,
        reasoning=decision.reasoning,
        confidence=decision.confidence if decision.confidence else None,
        data_sources=data_sources or None,
    )
    log.info(
        "Overlay decision recorded: id=%s action=%s regime=%s confidence=%.2f",
        row_id,
        action,
        regime.state.value,
        decision.confidence,
    )
    return row_id


# ──────────────────────────────────────────────────────────────────────────────
# Source loaders (graceful degradation if Builder 2's sources not yet available)
# ──────────────────────────────────────────────────────────────────────────────


def _load_news() -> str:
    """
    Load today's news summary from overlay.sources.news.

    Returns empty string if the module is unavailable (Builder 2 not yet
    merged) or if the source raises any exception.
    """
    try:
        from overlay.sources.news import get_news_summary  # type: ignore[import]

        result = get_news_summary()
        return result or ""
    except ImportError:
        log.debug("overlay.sources.news not available — skipping news (Builder 2 pending)")
        return ""
    except Exception as exc:
        log.warning("News source error: %s — continuing without news", exc)
        return ""


def _load_charts() -> str:
    """
    Load today's chart / technical analysis from overlay.sources.chart_intel.

    Returns empty string if the module is unavailable or raises.
    """
    try:
        from overlay.sources.chart_intel import get_chart_analysis  # type: ignore[import]

        result = get_chart_analysis()
        return result or ""
    except ImportError:
        log.debug(
            "overlay.sources.chart_intel not available — "
            "skipping chart analysis (Builder 2 pending)"
        )
        return ""
    except Exception as exc:
        log.warning("Chart source error: %s — continuing without chart analysis", exc)
        return ""


def _load_sector_rotation() -> str:
    """Load sector rotation signal from signals.sector_rotation."""
    try:
        from signals.sector_rotation import get_sector_rotation_signal
        result = get_sector_rotation_signal()
        if not result:
            return ""
        lines = [f"Rotation: {result.get('rotation_state', 'unknown')}"]
        if result.get('defensive_alert'):
            lines.append(f"⚠ DEFENSIVE ALERT: {result['defensive_alert']}")
        rankings = result.get('rankings', [])
        for r in rankings[:5]:
            lines.append(f"  {r.get('ticker','?'):4s} ({r.get('name',''):20s}): ROC={r.get('roc_63d',0):+.1f}%  rank={r.get('rank',0)}")
        return "\n".join(lines)
    except Exception as exc:
        log.warning("Sector rotation source error: %s", exc)
        return ""


def _load_aaii_sentiment() -> str:
    """Load AAII/CNN Fear & Greed sentiment signal."""
    try:
        from data.aaii import get_sentiment_signal
        sig = get_sentiment_signal()
        if not sig or sig.get("confidence", 0) == 0 and sig.get("bullish_pct") is None:
            return ""
        lines = [
            f"Sentiment: {sig['signal']} (confidence={sig['confidence']:.2f})",
            f"  Bull: {sig['bullish_pct']}%  Bear: {sig['bearish_pct']}%  Neutral: {sig['neutral_pct']}%",
            f"  Spread (bull-bear): {sig['spread']}",
        ]
        if sig.get("fear_greed_score") is not None:
            lines.append(f"  CNN Fear & Greed Score: {sig['fear_greed_score']:.1f}/100")
        if sig.get("details"):
            lines.append(f"  {sig['details']}")
        return "\n".join(lines)
    except Exception as exc:
        log.warning("AAII sentiment source error: %s", exc)
        return ""


def _load_etf_flows() -> str:
    """Load ETF flow rotation signal from volume z-scores."""
    try:
        from signals.etf_flows import get_etf_flow_signal
        sig = get_etf_flow_signal()
        if not sig:
            return ""
        lines = [
            f"ETF Volume Rotation: {sig['rotation_signal']} (confidence={sig['confidence']:.2f})",
            f"  Cyclical avg z-score: {sig['cyclical_avg_zscore']:+.2f}",
            f"  Defensive avg z-score: {sig['defensive_avg_zscore']:+.2f}",
        ]
        surges = [z for z in sig.get("zscores", []) if z["signal"] == "surge"]
        droughts = [z for z in sig.get("zscores", []) if z["signal"] == "drought"]
        if surges:
            lines.append(f"  Surges: {', '.join(z['ticker'] for z in surges)}")
        if droughts:
            lines.append(f"  Droughts: {', '.join(z['ticker'] for z in droughts)}")
        return "\n".join(lines)
    except Exception as exc:
        log.warning("ETF flow source error: %s", exc)
        return ""


def _load_macro_surprise() -> str:
    """Load macro surprise index from signals.macro_surprise."""
    try:
        from signals.macro_surprise import get_macro_surprise_signal
        sig = get_macro_surprise_signal()
        if not sig:
            return ""
        lines = [
            f"Macro Surprise: {sig['signal']} (composite z={sig['composite_surprise']:+.3f}, confidence={sig['confidence']:.2f})",
            f"  Regime implication: {sig.get('regime_implication', 'neutral')}",
        ]
        for name, s in sig.get("surprises", {}).items():
            lines.append(f"  {s['name']:25s}: z={s['z_score']:+.2f} ({s['direction']})")
        return "\n".join(lines)
    except Exception as exc:
        log.warning("Macro surprise source error: %s", exc)
        return ""




def _load_alt_data() -> str:
    """Load alt-data (insider/screener) signals if enabled in config.

    Default OFF — returns empty string unless `alt_data.enabled` is true
    AND `alt_data.tickers` (or held positions) are available.
    """
    try:
        from utils.config import load_config
        cfg = load_config()
    except (ImportError, FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        log.warning("Could not load config in _load_alt_data: %s — skipping alt data", exc)
        return ""
    alt_cfg = cfg.get("alt_data", {})
    if not alt_cfg.get("enabled", False):
        return ""
    try:
        from overlay.sources.alt_data import get_alt_data_summary  # type: ignore[import]
        tickers = alt_cfg.get("tickers", []) or []
        # Auto-resolve from current positions if no explicit list given
        if not tickers:
            try:
                from brokers.live_portfolio import LivePortfolio
                pf = LivePortfolio(cfg, market_id=cfg.get("market", "sp500"))
                tickers = [p.ticker for p in pf.positions if hasattr(p, "ticker")]
            except (ImportError, AttributeError, OSError, RuntimeError) as exc:
                log.debug("Could not load live positions for alt-data ticker lookup: %s", exc)
                tickers = []
        if not tickers:
            return ""
        return get_alt_data_summary(tickers=tickers) or ""
    except ImportError:
        log.debug("overlay.sources.alt_data not available — skipping alt_data")
        return ""
    except Exception as exc:
        log.warning("Alt-data source error: %s — continuing without alt-data", exc)
        return ""

# ──────────────────────────────────────────────────────────────────────────────
# Vision A/B helpers  (Wave 4 P1)
# ──────────────────────────────────────────────────────────────────────────────


def _compute_vision_divergence(text_decision: "OverlayDecision", vision_raw: dict) -> list:
    """Return per-ticker divergence flags between text and vision decisions.

    For each signal in vision_raw["chart_vision_signals"]:
      - "vision_bear_text_bull"  : vision says tighten, text does not flag ticker
      - "vision_bull_text_bear"  : text flags ticker to avoid, vision says OK
    Returns a list of dicts: {ticker, flag, text_signal, vision_signal}.
    """
    results = []
    for sig in vision_raw.get("chart_vision_signals", []):
        ticker = sig.get("ticker", "")
        if not ticker:
            continue
        tighten_rec = bool(sig.get("tighten_rec", False))
        in_text_avoid = ticker in (text_decision.tickers_to_avoid or [])
        text_tightening = in_text_avoid or text_decision.adjust

        if tighten_rec and not in_text_avoid and not text_decision.adjust:
            results.append({
                "ticker": ticker,
                "flag": "vision_bear_text_bull",
                "text_signal": "bull",
                "vision_signal": "bear",
            })
        elif not tighten_rec and in_text_avoid:
            results.append({
                "ticker": ticker,
                "flag": "vision_bull_text_bear",
                "text_signal": "bear",
                "vision_signal": "bull",
            })
    return results


def _write_vision_ab_log(
    text_decision: "OverlayDecision",
    vision_raw: "Optional[dict]",
    universe: str,
    tickers_analysed: list,
) -> None:
    """Append one JSONL line to the daily A/B log file.

    Non-fatal — any exception is logged as a warning; never raises.
    Log path: /root/atlas/logs/overlay_vision_ab/<YYYY-MM-DD>.jsonl
    """
    try:
        import json as _json
        import os as _os
        from datetime import datetime as _dt, timezone as _tz
        from pathlib import Path as _Path

        log_dir = _Path("/root/atlas/logs/overlay_vision_ab")
        log_dir.mkdir(parents=True, exist_ok=True)

        today = _dt.now(_tz.utc).strftime("%Y-%m-%d")
        log_path = log_dir / f"{today}.jsonl"

        divergence_flags = _compute_vision_divergence(text_decision, vision_raw or {})

        entry = {
            "timestamp": _dt.now(_tz.utc).isoformat(),
            "universe": universe,
            "tickers_analysed": tickers_analysed,
            "text_decision": {
                "adjust": text_decision.adjust,
                "sizing_multiplier_override": text_decision.sizing_multiplier_override,
                "universes_to_deactivate": list(text_decision.universes_to_deactivate or []),
                "tickers_to_avoid": list(text_decision.tickers_to_avoid or []),
                "reasoning": text_decision.reasoning,
                "confidence": text_decision.confidence,
            },
            "vision_decision": vision_raw,
            "divergence_flags": divergence_flags,
        }

        line = _json.dumps(entry, default=str)
        with open(log_path, "a") as fh:
            fh.write(line + "\n")

        log.debug("overlay_vision A/B log written → %s", log_path)
    except Exception as exc:
        log.warning("_write_vision_ab_log failed (non-fatal): %s", exc)



# ──────────────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────────────


def run_overlay(mode: str = "log_only") -> OverlayDecision:
    """
    Run one overlay evaluation cycle.

    This is the primary entry point called by the daily plan workflow.

    Parameters
    ----------
    mode : {"log_only", "active"}
        ``log_only``  — Evaluate and persist to DB, but the decision is
                        informational only.  The pipeline continues with the
                        unmodified regime defaults.  Use during the 2-week
                        log-only validation window (Phase 4 rollout).
        ``active``    — Decision is returned for plan.py to apply to the
                        portfolio constructor (sizing, universe, ticker filters).

    Returns
    -------
    OverlayDecision
        Always returns a valid decision.  On any error (regime failure,
        pi CLI timeout, JSON parse error, DB write failure) returns a safe
        no_change decision.  The overlay **never** blocks the pipeline.

    Notes
    -----
    The sizing_multiplier_override in the returned decision is *always* <=
    regime.sizing_multiplier (enforced by _validate_response).  plan.py may
    use it directly without re-validating.
    """
    log.info("=== Overlay engine starting (mode=%s) ===", mode)

    # ── Step 1: Get current regime ───────────────────────────────────────────
    try:
        model = RegimeModel()
        regime = model.classify_and_record()
        log.info(
            "Regime classified: state=%s sizing=%.2f date=%s",
            regime.state.value,
            regime.sizing_multiplier,
            regime.date,
        )
    except Exception as exc:
        log.error(
            "Regime classification failed: %s — defaulting to no_change", exc
        )
        return OverlayDecision.no_change(f"error — regime unavailable: {exc}")

    # ── Step 2: Gather data sources ──────────────────────────────────────────
    news = _load_news()
    charts = _load_charts()
    sector_rotation = _load_sector_rotation()
    sentiment = _load_aaii_sentiment()
    etf_flows = _load_etf_flows()
    macro_surprise = _load_macro_surprise()
    alt_data = _load_alt_data()

    data_sources: Dict = {
        "news_available": bool(news),
        "charts_available": bool(charts),
        "sector_rotation_available": bool(sector_rotation),
        "sentiment_available": bool(sentiment),
        "etf_flows_available": bool(etf_flows),
        "macro_surprise_available": bool(macro_surprise),
        "alt_data_available": bool(alt_data),
        "regime_date": regime.date,
        "regime_state": regime.state.value,
    }

    # ── Step 3: Build structured prompt ─────────────────────────────────────
    user_prompt = _build_user_prompt(
        regime, news, charts,
        sector_rotation=sector_rotation,
        sentiment=sentiment,
        etf_flows=etf_flows,
        macro_surprise=macro_surprise,
        alt_data=alt_data,
    )

    # ── Step 4: Call pi CLI ──────────────────────────────────────────────────
    raw_response = _call_pi(user_prompt)

    if raw_response is None:
        decision = OverlayDecision.no_change("error — defaulting to no change")
        log.warning("pi CLI returned no usable response — no_change applied")
    else:
        # ── Step 5: Validate response (enforces ASYMMETRIC CONSTRAINT) ───────
        decision = _validate_response(raw_response, regime)


    # ── Vision overlay (feature-flagged, default OFF) ────────────────────────
    # Lazy-imported so zero overhead when flag is off.  Non-fatal — any error
    # here is logged and skipped; the text-only decision already stands.
    try:
        from utils.config import load_config
        cfg_vision = load_config().get("overlay_vision", {"enabled": False})
    except (ImportError, FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        log.debug("Could not load overlay_vision config: %s — vision disabled", exc)
        cfg_vision = {"enabled": False}

    if cfg_vision.get("enabled", False):
        # log_only=True (default for safety): vision is called + A/B-logged
        # but NOT merged into decision.  Set False to activate full merge.
        log_only = bool(cfg_vision.get("log_only", True))
        try:
            from overlay.sources.chart_renders import render_reference_set  # lazy
            # Best-effort: read universe name for A/B log
            try:
                _vision_universe = load_config().get("market", "unknown")
            except (AttributeError, OSError, KeyError) as exc:
                log.debug("Could not read market name for vision A/B log: %s", exc)
                _vision_universe = "unknown"
            # Best-effort: read held positions for personalised charts
            try:
                from brokers.live_portfolio import live_portfolio
                positions = (
                    [p.ticker for p in live_portfolio.get_open_positions()]
                    if live_portfolio else []
                )
            except (ImportError, AttributeError, OSError, RuntimeError) as exc:
                log.debug("Could not load open positions for vision charts: %s", exc)
                positions = []
            max_imgs = int(cfg_vision.get("max_images", 10))
            images = render_reference_set(positions, max_images=max_imgs)
            vision_raw = None
            if images:
                labels_and_paths = list(images.items())
                vision_raw = _call_pi_with_vision(
                    user_prompt,
                    labels_and_paths,
                    model=cfg_vision.get("model", "claude-opus-4-7"),
                    timeout=int(cfg_vision.get("timeout_seconds", 300)),
                )
            # Derive tickers_analysed from image labels (strip suffixes, dedupe)
            _tickers_analysed = list(dict.fromkeys(
                k.replace("_daily_1y", "").replace("_hourly_1w", "")
                for k in images.keys()
            )) if images else []
            # Always write A/B log, regardless of log_only flag
            _write_vision_ab_log(decision, vision_raw, _vision_universe, _tickers_analysed)
            if vision_raw and isinstance(vision_raw.get("chart_vision_signals"), list):
                if log_only:
                    log.info(
                        "overlay_vision: log_only=true — vision response recorded "
                        "to A/B log, NOT merged into decision"
                    )
                else:
                    decision.chart_vision_signals = vision_raw["chart_vision_signals"]
                    log.info(
                        "A/B COMPARE — text-only adjust=%s; vision-augmented signals=%d",
                        decision.adjust,
                        len(decision.chart_vision_signals),
                    )
        except Exception as exc:
            log.warning("Overlay vision path failed (non-fatal): %s", exc)

        log.info(
        "Overlay decision: adjust=%s sizing_override=%s "
        "universes_off=%s tickers_avoid=%s confidence=%.2f",
        decision.adjust,
        decision.sizing_multiplier_override,
        decision.universes_to_deactivate,
        decision.tickers_to_avoid,
        decision.confidence,
    )

    # ── Step 6: Persist to SQLite ────────────────────────────────────────────
    try:
        _record_decision(decision, regime, data_sources)
    except Exception as exc:
        log.error(
            "Failed to record overlay decision to DB: %s — continuing", exc
        )

    # ── Step 7 & 8: Return decision ──────────────────────────────────────────
    if mode == "log_only":
        log.info(
            "Mode=log_only: decision logged but NOT applied to plan "
            "(2-week validation window)"
        )
    else:
        log.info("Mode=%s: decision available for plan.py consumption", mode)

    return decision
