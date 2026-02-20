"""Signal Enrichment for Live Trading Pipeline.

Phase 7 Integration: Applies market breadth, relative strength,
and earnings blackout enrichment to live trading signals.

This module replicates the same enrichment logic used in the
backtest engine, ensuring consistency between backtested and
live signal generation.

Usage:
    from utils.signal_enrichment import enrich_signals
    enriched = enrich_signals(signals, data, config, trade_date)
"""

import logging
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Any
from datetime import datetime

from utils.market_breadth import MarketBreadth
from utils.relative_strength import RelativeStrength
from utils.earnings import is_near_earnings

logger = logging.getLogger("atlas.enrichment")


def enrich_signals(
    signals: list,
    data: Dict[str, pd.DataFrame],
    config: dict,
    trade_date: Optional[str] = None,
) -> list:
    """Enrich trading signals with Phase 7 features and confidence modifiers.

    Applies in order:
        1. Market breadth features injection + confidence modifiers (Phase 7C)
        2. Relative strength features injection + confidence modifiers (Phase 7B)
        3. Earnings blackout check for mean reversion signals (Phase 7A)

    Args:
        signals: List of signal objects from strategy.generate_signals().
        data: Dict mapping ticker -> OHLCV DataFrame.
        config: Active configuration dict.
        trade_date: ISO date string (YYYY-MM-DD). Defaults to today.

    Returns:
        The same signals list, mutated with enrichment features and
        adjusted confidence scores. Signals blocked by earnings blackout
        are removed from the list.
    """
    if not signals:
        return signals

    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    today = pd.Timestamp(trade_date)
    yesterday = today - pd.Timedelta(days=1)

    # Ensure signals have features dict
    for sig in signals:
        if not hasattr(sig, "features"):
            sig.features = {}

    # ── Phase 7C: Market Breadth ─────────────────────────────────
    try:
        mb = MarketBreadth(data)
        breadth = mb.compute(today)
        if breadth:
            logger.info(
                f"BREADTH ({trade_date}): "
                f"pct_above_50ma={breadth.get('pct_above_50ma', 0):.1f}%, "
                f"ad_ratio={breadth.get('ad_ratio', 0):.2f}"
            )
            for sig in signals:
                sig.features["breadth_pct_above_50ma"] = float(breadth.get("pct_above_50ma", 0))
                sig.features["breadth_pct_above_200ma"] = float(breadth.get("pct_above_200ma", 0))
                sig.features["breadth_ad_ratio"] = float(breadth.get("ad_ratio", 0))
                sig.features["breadth_thrust"] = float(breadth.get("breadth_thrust", 0))
                breadth_mom = breadth.get("breadth_momentum", 0)
                sig.features["breadth_momentum"] = float(breadth_mom) if not pd.isna(breadth_mom) else 0.0
                sig.features["breadth_net_new_highs_pct"] = float(breadth.get("net_new_highs_pct", 0))

            # Apply breadth confidence modifiers
            for sig in signals:
                strat_key = sig.strategy
                breadth_cfg = config.get("strategies", {}).get(strat_key, {}).get("breadth", {})
                if breadth_cfg.get("enabled", False):
                    metric = breadth_cfg.get("metric", "pct_above_50ma")
                    breadth_val = sig.features.get(f"breadth_{metric}", None)
                    if breadth_val is not None:
                        low_thresh = breadth_cfg.get("low_threshold", 48.0)
                        high_thresh = breadth_cfg.get("high_threshold", 58.0)
                        low_boost = breadth_cfg.get("low_boost", 0.0)
                        high_penalty = breadth_cfg.get("high_penalty", 0.0)
                        orig_conf = sig.confidence
                        adj = 0.0
                        if breadth_val < low_thresh:
                            adj = low_boost
                        elif breadth_val > high_thresh:
                            adj = -high_penalty
                        if adj != 0.0:
                            sig.confidence = max(0.0, min(1.0, sig.confidence + adj))
                            sig.features["breadth_confidence_adj"] = round(adj, 4)
                            sig.features["breadth_confidence_orig"] = round(orig_conf, 4)
                            logger.info(
                                f"BREADTH MODIFIER {sig.ticker} ({strat_key}): "
                                f"breadth={breadth_val:.1f}, adj={adj:+.3f}, "
                                f"conf {orig_conf:.3f} -> {sig.confidence:.3f}"
                            )
    except Exception as e:
        logger.error(f"Market breadth enrichment failed: {e}", exc_info=True)

    # ── Phase 7B: Relative Strength ──────────────────────────────
    try:
        rs = RelativeStrength(data)
        rs_ranks = rs.compute(today)
        if rs_ranks:
            logger.info(f"RS computed for {len(rs_ranks)} tickers on {trade_date}")
            for sig in signals:
                ticker = sig.ticker
                if ticker in rs_ranks:
                    rs_info = rs_ranks[ticker]
                    sig.features["rs_percentile"] = float(rs_info.get("rs_percentile", 50.0))
                    sig.features["rs_score"] = float(rs_info.get("rs_score", 0.0))
                    sig.features["rs_momentum"] = float(rs_info.get("rs_momentum", 0.0))
                    sig.features["roc_20"] = float(rs_info.get("roc_20", 0.0))
                    sig.features["roc_60"] = float(rs_info.get("roc_60", 0.0))
                    sig.features["roc_120"] = float(rs_info.get("roc_120", 0.0))

            # Apply RS confidence modifiers
            for sig in signals:
                strat_key = sig.strategy
                rs_cfg = config.get("strategies", {}).get(strat_key, {}).get("relative_strength", {})
                if rs_cfg.get("enabled", False):
                    rs_metric = rs_cfg.get("metric", "rs_percentile")
                    rs_val = sig.features.get(rs_metric, None)
                    if rs_val is not None:
                        rs_low_thresh = rs_cfg.get("low_threshold", 40.0)
                        rs_high_thresh = rs_cfg.get("high_threshold", 60.0)
                        rs_low_penalty = rs_cfg.get("low_penalty", 0.0)
                        rs_high_boost = rs_cfg.get("high_boost", 0.0)
                        rs_orig_conf = sig.confidence
                        rs_adj = 0.0
                        if rs_val < rs_low_thresh:
                            rs_adj = -rs_low_penalty
                        elif rs_val > rs_high_thresh:
                            rs_adj = rs_high_boost
                        if rs_adj != 0.0:
                            sig.confidence = max(0.0, min(1.0, sig.confidence + rs_adj))
                            sig.features["rs_confidence_adj"] = round(rs_adj, 4)
                            sig.features["rs_confidence_orig"] = round(rs_orig_conf, 4)
                            logger.info(
                                f"RS MODIFIER {sig.ticker} ({strat_key}): "
                                f"rs={rs_val:.1f}, adj={rs_adj:+.3f}, "
                                f"conf {rs_orig_conf:.3f} -> {sig.confidence:.3f}"
                            )
    except Exception as e:
        logger.error(f"Relative strength enrichment failed: {e}", exc_info=True)

    # ── Phase 7A: Earnings Blackout (live trading only) ──────────
    # For mean reversion signals, check if near earnings
    earnings_cfg = config.get("strategies", {}).get("mean_reversion", {}).get("earnings_blackout", {})
    blackout_enabled = earnings_cfg.get("enabled", True)  # Default ON for live
    blackout_days_before = earnings_cfg.get("days_before", 5)
    blackout_days_after = earnings_cfg.get("days_after", 1)

    if blackout_enabled:
        filtered = []
        blocked_count = 0
        for sig in signals:
            if sig.strategy == "mean_reversion":
                try:
                    if is_near_earnings(
                        sig.ticker,
                        reference_date=today,
                        blackout_days_before=blackout_days_before,
                        blackout_days_after=blackout_days_after,
                    ):
                        logger.warning(
                            f"EARNINGS BLACKOUT: Blocking {sig.ticker} "
                            f"mean reversion signal (near earnings)"
                        )
                        sig.features["earnings_blocked"] = True
                        blocked_count += 1
                        continue
                except Exception as e:
                    logger.debug(f"Earnings check failed for {sig.ticker}: {e}")
            filtered.append(sig)
        signals = filtered
        logger.info(
            f"Earnings blackout: {len(signals)} signals passed "
            f"(removed {blocked_count} blocked)"
        )
    else:
        logger.info("Earnings blackout disabled")

    return signals
