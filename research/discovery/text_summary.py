"""
research.discovery.text_summary — Structured text-summary builder for the LLM research loop.

Implements the #314 upgrade: adds volume features, structured section headers,
cross-asset context, and telemetry logging so the LLM receives a richer feature
set without vision inference cost.

Usage
-----
    from research.discovery.text_summary import build_enriched_summary

    summary_str = build_enriched_summary(
        ticker_data={"close": [...], "volume": [...]},
        base_fields={"rsi": 58.3, "trend": "bullish", ...},
        market_id="sp500",
    )

Feature flag (#314 step 4)
--------------------------
Set ``ATLAS_TEXT_SUMMARY_V2=1`` to enable the enriched feature set.
Without the flag, ``build_enriched_summary`` falls back to a plain-text blob
identical to the pre-#314 behaviour (backward-compatible).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)

# ── Lazy DB imports (module-level for test patchability) ──────────────────────
try:
    from db.atlas_db import get_current_regime_state, get_db  # noqa: F401
except ImportError:
    # Allow import in minimal test environments
    def get_current_regime_state():  # type: ignore[misc]
        return None
    def get_db():  # type: ignore[misc]
        raise RuntimeError("atlas_db not available")

# Feature flag — set ATLAS_TEXT_SUMMARY_V2=1 to enable enriched output.
TEXT_SUMMARY_V2_ENABLED: bool = os.environ.get("ATLAS_TEXT_SUMMARY_V2", "0") == "1"

# Telemetry log path (appended per call).
_ATLAS_ROOT = Path(__file__).resolve().parents[3]  # .../atlas
_TELEMETRY_LOG = _ATLAS_ROOT / "logs" / "text_summary_telemetry.log"

# OBV divergence threshold (matches research.chart_intel._DIVERGENCE_SLOPE_THRESHOLD).
_DIVERGENCE_SLOPE_THRESHOLD: float = -0.005


# ── Step 1: Volume-feature enrichment ──────────────────────────────────────────

def add_volume_features(
    summary_dict: dict[str, Any],
    ticker_data: dict[str, Sequence[float]],
) -> dict[str, Any]:
    """Enrich *summary_dict* with OBV slope, volume trend, and divergence flags.

    Parameters
    ----------
    summary_dict : Dict to mutate in-place (and return).
    ticker_data  : Must contain ``"close"`` and ``"volume"`` keys (array-like).

    Added keys
    ----------
    obv_slope_20d   : float — normalised OBV regression slope over 20 bars
    volume_trend    : str — "rising", "falling", or "flat"
    pv_divergence   : bool — price rising + volume declining (bearish signal)
    volume_vs_median: float — current volume / 20-day median (>1.0 = above median)
    candle_pattern  : str — last-bar pattern name ("engulfing_bull", "doji", etc.)
    """
    out = dict(summary_dict)  # non-mutating copy

    closes = np.asarray(ticker_data.get("close", []), dtype=float)
    volumes = np.asarray(ticker_data.get("volume", []), dtype=float)

    # ── OBV slope ─────────────────────────────────────────────────────────────
    obv_slope = 0.0
    if len(closes) >= 22 and len(volumes) == len(closes):
        try:
            from research.chart_intel import _compute_obv_slope
            obv_slope = _compute_obv_slope(closes, volumes, lookback=20)
        except Exception as exc:
            logger.debug("text_summary: OBV slope error — %s", exc)
    out["obv_slope_20d"] = obv_slope

    # ── Volume trend ─────────────────────────────────────────────────────────
    volume_trend = "flat"
    if len(volumes) >= 21:
        recent_vol = volumes[-20:]
        x = np.arange(len(recent_vol), dtype=float)
        if recent_vol.mean() > 0:
            slope_norm = _linreg_slope_norm(x, recent_vol)
            if slope_norm > 0.003:
                volume_trend = "rising"
            elif slope_norm < -0.003:
                volume_trend = "falling"
    out["volume_trend"] = volume_trend

    # ── Price-volume divergence ───────────────────────────────────────────────
    pv_divergence = False
    if len(closes) >= 21 and len(volumes) == len(closes):
        try:
            from research.chart_intel import _detect_price_volume_divergence
            pv_divergence, _mag = _detect_price_volume_divergence(closes, volumes, window=20)
        except Exception as exc:
            logger.debug("text_summary: PV divergence error — %s", exc)
    out["pv_divergence"] = pv_divergence

    # ── Volume vs 20-day median ───────────────────────────────────────────────
    vol_vs_median = 1.0
    if len(volumes) >= 21:
        median_vol = float(np.median(volumes[-20:]))
        current_vol = float(volumes[-1])
        vol_vs_median = (current_vol / median_vol) if median_vol > 0 else 1.0
    out["volume_vs_median"] = round(vol_vs_median, 3)

    # ── Candle-pattern detector (last bar) ────────────────────────────────────
    opens = np.asarray(ticker_data.get("open", []), dtype=float)
    highs = np.asarray(ticker_data.get("high", []), dtype=float)
    lows = np.asarray(ticker_data.get("low", []), dtype=float)
    out["candle_pattern"] = _detect_candle_pattern(opens, highs, lows, closes)

    return out


def _linreg_slope_norm(x: np.ndarray, y: np.ndarray) -> float:
    """Normalised linear regression slope (slope / mean(y))."""
    x_mean = x.mean()
    y_mean = y.mean()
    num = float(((x - x_mean) * (y - y_mean)).sum())
    den = float(((x - x_mean) ** 2).sum())
    if den == 0 or y_mean == 0:
        return 0.0
    return (num / den) / y_mean


def _detect_candle_pattern(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
) -> str:
    """Classify last bar as a named candle pattern, or 'none'.

    Patterns detected
    -----------------
    doji             : body < 10% of range
    hammer           : long lower wick, small body at top
    shooting_star    : long upper wick, small body at bottom
    engulfing_bull   : current bar engulfs prior bar on the upside
    engulfing_bear   : current bar engulfs prior bar on the downside
    """
    if len(closes) < 2 or len(opens) < 2:
        return "none"

    # ── Current bar ──────────────────────────────────────────────────────────
    try:
        o, h, l, c = float(opens[-1]), float(highs[-1]), float(lows[-1]), float(closes[-1])
        po, pc = float(opens[-2]), float(closes[-2])
    except (IndexError, ValueError):
        return "none"

    bar_range = h - l
    if bar_range <= 0:
        return "none"

    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    body_ratio = body / bar_range

    # Doji: body very small relative to range.
    if body_ratio < 0.1:
        return "doji"

    # Hammer: lower wick > 2× body, body in upper 40% of range.
    if lower_wick >= 2 * body and (min(o, c) - l) >= 0.6 * bar_range:
        return "hammer"

    # Shooting star: upper wick > 2× body, body in lower 40% of range.
    if upper_wick >= 2 * body and (h - max(o, c)) >= 0.6 * bar_range:
        return "shooting_star"

    # Bullish engulfing: current green bar body engulfs prior red bar body.
    if c > o and pc < po:  # current bullish, prior bearish
        if o <= pc and c >= po:
            return "engulfing_bull"

    # Bearish engulfing: current red bar body engulfs prior green bar body.
    if c < o and pc > po:  # current bearish, prior bullish
        if o >= pc and c <= po:
            return "engulfing_bear"

    return "none"


# ── Step 2: Structured section builder ────────────────────────────────────────

def structure_summary(summary_dict: dict[str, Any]) -> str:
    """Render *summary_dict* as a sectioned markdown string for LLM consumption.

    Sections
    --------
    ## Price Action  — trend, SMAs, momentum, support/resistance
    ## Volume        — volume ratio, OBV slope, divergence, candle pattern
    ## Volatility    — RSI, ATR / vol proxy (if available)
    ## Indicators    — additional indicators (regime, breadth)
    ## Risk Overlay  — suppression signal, OBV divergence warning
    """
    lines: list[str] = []

    # ── ## Price Action ───────────────────────────────────────────────────────
    lines.append("## Price Action")
    trend = summary_dict.get("trend", "unknown")
    lines.append(f"Trend: {trend}")
    for sma_label in ("sma20", "sma50", "sma200"):
        val = summary_dict.get(sma_label)
        if val is not None:
            flag = "above" if summary_dict.get(f"above_{sma_label}sma") else "below"
            lines.append(f"{sma_label.upper()}: {val:.2f} ({flag})")
    mom = summary_dict.get("momentum_20d")
    if mom is not None:
        lines.append(f"Momentum 20d: {mom:+.1%}")
    support = summary_dict.get("support")
    resistance = summary_dict.get("resistance")
    if support is not None:
        lines.append(f"Support: {support:.2f}")
    if resistance is not None:
        lines.append(f"Resistance: {resistance:.2f}")
    lines.append("")

    # ── ## Volume ─────────────────────────────────────────────────────────────
    lines.append("## Volume")
    vol_ratio = summary_dict.get("volume_ratio")
    if vol_ratio is not None:
        lines.append(f"Volume ratio (vs 20d avg): {vol_ratio:.2f}x")
    vol_vs_median = summary_dict.get("volume_vs_median")
    if vol_vs_median is not None:
        lines.append(f"Volume vs 20d median: {vol_vs_median:.2f}x")
    obv = summary_dict.get("obv_slope_20d")
    if obv is not None:
        direction = "accumulation" if obv > 0 else "distribution" if obv < 0 else "flat"
        lines.append(f"OBV slope (20d): {obv:+.4f} [{direction}]")
    vol_trend = summary_dict.get("volume_trend")
    if vol_trend is not None:
        lines.append(f"Volume trend: {vol_trend}")
    candle = summary_dict.get("candle_pattern", "none")
    if candle != "none":
        lines.append(f"Candle pattern: {candle}")
    lines.append("")

    # ── ## Volatility ─────────────────────────────────────────────────────────
    lines.append("## Volatility")
    rsi = summary_dict.get("rsi")
    rsi_status = summary_dict.get("rsi_status", "neutral")
    if rsi is not None:
        lines.append(f"RSI (14): {rsi:.1f} [{rsi_status}]")
    atr = summary_dict.get("atr")
    if atr is not None:
        lines.append(f"ATR: {atr:.2f}")
    vix = summary_dict.get("vix_level")
    if vix is not None:
        lines.append(f"VIX: {vix:.1f}")
    lines.append("")

    # ── ## Indicators ─────────────────────────────────────────────────────────
    lines.append("## Indicators")
    regime = summary_dict.get("regime")
    if regime is not None:
        lines.append(f"Regime: {regime}")
    sector_rs = summary_dict.get("sector_relative_strength")
    if sector_rs is not None:
        lines.append(f"Sector RS vs SPY (4w): {sector_rs:+.1%}")
    spy_breadth = summary_dict.get("spy_breadth")
    if spy_breadth is not None:
        lines.append(f"SPY breadth: {spy_breadth}")
    lines.append("")

    # ── ## Risk Overlay ───────────────────────────────────────────────────────
    lines.append("## Risk Overlay")
    pv_div = summary_dict.get("pv_divergence")
    if pv_div:
        lines.append("⚠️ Price-volume divergence detected (price up, volume declining)")
    dist = summary_dict.get("distribution_signal")
    if dist:
        lines.append("⚠️ Distribution signal: at resistance on low volume")
    sizing = summary_dict.get("sizing_override")
    if sizing is not None and float(sizing) < 0.5:
        reason = summary_dict.get("sizing_reason", "overlay signal")
        lines.append(f"⚠️ Overlay suppressed: {reason} (multiplier={sizing:.2f})")
    obv_neg = summary_dict.get("obv_slope_20d")
    if obv_neg is not None and obv_neg < -0.01:
        lines.append("⚠️ OBV divergence: price may be rising on distribution")
    if not any(
        summary_dict.get(k)
        for k in ("pv_divergence", "distribution_signal")
    ) and (sizing is None or float(sizing) >= 0.5):
        lines.append("No active risk flags")
    lines.append("")

    return "\n".join(lines)


# ── Step 3: Cross-asset context ───────────────────────────────────────────────

def add_cross_asset_context(
    summary_dict: dict[str, Any],
    market_id: str = "sp500",
) -> dict[str, Any]:
    """Enrich *summary_dict* with regime state, VIX level, and sector relative strength.

    All lookups are non-fatal (fail-soft on DB error or missing data).

    Added keys
    ----------
    regime          : str — e.g. "bull_risk_on"
    vix_level       : float | None
    spy_breadth     : str | None — e.g. "85% above 50SMA"
    sector_relative_strength : float | None — rolling 4-week RS vs SPY
    """
    out = dict(summary_dict)

    # ── Regime ────────────────────────────────────────────────────────────────
    try:
        regime = get_current_regime_state() or "unknown"
        out["regime"] = regime
    except Exception as exc:
        logger.debug("text_summary: regime lookup failed — %s", exc)
        out.setdefault("regime", "unknown")

    # ── VIX level ─────────────────────────────────────────────────────────────
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM macro_indicators WHERE indicator='vix' ORDER BY date DESC LIMIT 1"
            ).fetchone()
            if row:
                out["vix_level"] = float(row["value"])
    except Exception as exc:
        logger.debug("text_summary: VIX lookup failed — %s", exc)

    # ── Sector relative strength ──────────────────────────────────────────────
    try:
        with get_db() as db:
            # 20-day (4-week) momentum for sector ETF relevant to market_id
            etf_map = {
                "sp500": "SPY",
                "sector_etfs": "XLK",
                "commodity_etfs": "GLD",
            }
            sector_etf = etf_map.get(market_id, "SPY")
            rows = db.execute(
                """SELECT close FROM ohlcv
                   WHERE ticker=? ORDER BY date DESC LIMIT 21""",
                (sector_etf,),
            ).fetchall()
            if len(rows) >= 21:
                prices = [r["close"] for r in rows]
                rs = (prices[0] - prices[20]) / prices[20] if prices[20] else 0.0
                out["sector_relative_strength"] = round(rs, 4)
    except Exception as exc:
        logger.debug("text_summary: sector RS lookup failed — %s", exc)

    return out


# ── Step 4: Telemetry ─────────────────────────────────────────────────────────

def log_summary_telemetry(
    summary_dict: dict[str, Any],
    ticker: str = "unknown",
) -> None:
    """Append a single-line JSON entry to the telemetry log for prompt-budget tracking.

    Logs
    ----
    timestamp, ticker, features_included (list), summary_length (chars),
    has_regime, has_vix, has_pv_divergence, obv_slope
    """
    try:
        import json as _json

        features_included = [k for k in summary_dict if summary_dict[k] is not None]
        summary_str = structure_summary(summary_dict)

        entry = {
            "ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "ticker": ticker,
            "features": features_included,
            "n_features": len(features_included),
            "summary_chars": len(summary_str),
            "has_regime": "regime" in summary_dict,
            "has_vix": "vix_level" in summary_dict,
            "has_pv_divergence": bool(summary_dict.get("pv_divergence")),
            "obv_slope": summary_dict.get("obv_slope_20d"),
            "candle_pattern": summary_dict.get("candle_pattern", "none"),
        }

        _TELEMETRY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _TELEMETRY_LOG.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(entry) + "\n")

        logger.debug(
            "text_summary telemetry: ticker=%s features=%d chars=%d",
            ticker,
            len(features_included),
            len(summary_str),
        )
    except Exception as exc:
        # Telemetry must never break the main flow.
        logger.warning("text_summary: telemetry write failed — %s", exc)


# ── Orchestrator ───────────────────────────────────────────────────────────────

def build_enriched_summary(
    ticker_data: dict[str, Sequence[float]],
    base_fields: Optional[dict[str, Any]] = None,
    market_id: str = "sp500",
    ticker: str = "unknown",
) -> str:
    """Build an enriched, structured text summary for LLM consumption.

    Feature-flagged by ``ATLAS_TEXT_SUMMARY_V2=1``.  When the flag is off,
    returns a simple flat string from *base_fields* for backward compatibility.

    Parameters
    ----------
    ticker_data : Dict with keys ``"close"``, ``"volume"``, optionally
                  ``"open"``, ``"high"``, ``"low"`` (all array-like, ascending).
    base_fields : Pre-computed indicator fields (trend, rsi, sma*, etc.).
    market_id   : Market context for cross-asset lookup.
    ticker      : For telemetry labelling.
    """
    base = dict(base_fields or {})

    if not TEXT_SUMMARY_V2_ENABLED:
        # ── Legacy flat-blob format (backward-compatible) ──────────────────
        parts = []
        for k, v in base.items():
            if v is not None:
                parts.append(f"{k}={v}")
        return ", ".join(parts) if parts else "(no data)"

    # ── V2 enrichment pipeline ─────────────────────────────────────────────
    enriched = add_volume_features(base, ticker_data)
    enriched = add_cross_asset_context(enriched, market_id=market_id)
    log_summary_telemetry(enriched, ticker=ticker)
    return structure_summary(enriched)
