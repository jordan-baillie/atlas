"""Unified Auto-Promotion Pipeline for Atlas.

Called by sweep.py after each sweep cycle when a strategy has improved params.
Applies three validation gates before writing to active config:

    Gate 1: Cooldown (24h per strategy)
    Gate 2: Regression check (candidate vs active portfolio backtest)
    Gate 3: Sanity bounds (Sharpe > 0, CAGR > 0, ≥ 20 trades)

On pass: versions current config, writes new active, logs, notifies Telegram.
On fail: keeps candidate file, notifies why it was blocked.

Rollback: `rollback(market)` reads promotion_log.json and restores previous version.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ATLAS_ROOT = Path(__file__).resolve().parent.parent

# Cooldown: 24h per strategy (one promotion per day max)
COOLDOWN_PATH = Path("/tmp/promotion-cooldowns.json")
PROMOTION_LOG_PATH = ATLAS_ROOT / "config" / "promotion_log.json"
COOLDOWN_SECONDS = 86_400  # 24 hours


# ─── Public entry point ───────────────────────────────────────────────────────

def auto_promote(
    strategy: str,
    improved_params: dict,
    initial_sharpe: float,
    final_sharpe: float,
    improvements: list,
    market: str = "sp500",
) -> dict:
    """Unified auto-promotion gate.

    Args:
        strategy:        Strategy name (e.g. "mean_reversion").
        improved_params: Dict of param name → improved value.
        initial_sharpe:  Sharpe at start of sweep cycle.
        final_sharpe:    Sharpe after improvement.
        improvements:    List of improvement description strings.
        market:          Market id (default "sp500").

    Returns:
        dict with keys:
            promoted  — bool, True if config was updated.
            reason    — human-readable outcome string.
            version   — new version string if promoted, else None.
    """
    delta = final_sharpe - initial_sharpe
    logger.info(
        "auto_promote called: strategy=%s delta=+%.4f market=%s",
        strategy, delta, market,
    )

    # ── Gate 1: Cooldown ─────────────────────────────────────────────────────
    if not _check_cooldown(strategy):
        reason = f"24h cooldown active for {strategy}"
        logger.info("🕐 Promotion blocked: %s", reason)
        _notify({"promoted": False, "reason": reason, "strategy": strategy,
                 "market": market, "delta": delta})
        return {"promoted": False, "reason": reason, "version": None}

    # ── Build candidate config ────────────────────────────────────────────────
    try:
        from utils.config import get_active_config
        candidate_config = get_active_config(market)
    except Exception as exc:
        reason = f"Failed to load active config: {exc}"
        logger.error("auto_promote: %s", reason)
        return {"promoted": False, "reason": reason, "version": None}

    # Inject improved params into the strategy section
    strat_section = (
        candidate_config.setdefault("strategies", {}).setdefault(strategy, {})
    )
    strat_section.update(improved_params)
    strat_section["enabled"] = True

    now = datetime.now(timezone.utc)
    candidate_config["_sweep_metadata"] = {
        "promoted_at": now.isoformat(),
        "strategy": strategy,
        "initial_sharpe": initial_sharpe,
        "final_sharpe": final_sharpe,
        "delta_sharpe": round(delta, 6),
        "sweep_improvements": improvements,
    }

    # ── Gate 2: Regression check ──────────────────────────────────────────────
    try:
        regression = _regression_check(candidate_config, market)
    except Exception as exc:
        reason = f"Regression check errored: {exc}"
        logger.warning("auto_promote: %s — blocking promotion to be safe", reason)
        _notify({"promoted": False, "reason": reason, "strategy": strategy,
                 "market": market, "delta": delta})
        return {"promoted": False, "reason": reason, "version": None}

    if not regression.get("pass", False):
        # Summarise which metrics failed
        bad = [
            f"{m} {v['pct_change']:+.1f}%"
            for m, v in regression.get("comparisons", {}).items()
            if isinstance(v, dict) and v.get("pct_change", 0) < -10
        ]
        dd_info = regression.get("comparisons", {}).get("max_drawdown_pct", {})
        if isinstance(dd_info, dict) and dd_info.get("delta", 0) > 3.0:
            bad.append(f"drawdown +{dd_info['delta']:.1f}pp")

        reason = f"Regression check failed: {', '.join(bad) if bad else 'unknown metric'}"
        logger.info("📊 Promotion blocked: %s", reason)
        _notify({"promoted": False, "reason": reason, "strategy": strategy,
                 "market": market, "delta": delta,
                 "comparisons": regression.get("comparisons", {})})
        return {"promoted": False, "reason": reason, "version": None}

    candidate_metrics = regression.get("candidate_metrics", {})

    # ── Gate 3: Sanity bounds ─────────────────────────────────────────────────
    sanity = _sanity_check(candidate_metrics)
    if not sanity.get("pass", False):
        reason = f"Sanity check failed: {sanity.get('reason', 'unknown')}"
        logger.info("🚫 Promotion blocked: %s", reason)
        _notify({"promoted": False, "reason": reason, "strategy": strategy,
                 "market": market, "delta": delta})
        return {"promoted": False, "reason": reason, "version": None}

    # ── All gates passed — promote ────────────────────────────────────────────
    metadata = {
        "strategy": strategy,
        "initial_sharpe": initial_sharpe,
        "final_sharpe": final_sharpe,
        "delta_sharpe": round(delta, 6),
        "improvements": improvements,
        "baseline_metrics": regression.get("baseline_metrics", {}),
        "candidate_metrics": candidate_metrics,
        "comparisons": regression.get("comparisons", {}),
    }

    try:
        version = _do_promote(candidate_config, market, metadata)
    except Exception as exc:
        reason = f"Promotion write failed: {exc}"
        logger.error("auto_promote: %s", reason, exc_info=True)
        _notify({"promoted": False, "reason": reason, "strategy": strategy,
                 "market": market, "delta": delta})
        return {"promoted": False, "reason": reason, "version": None}

    # Update cooldown timestamp
    _update_cooldown(strategy)

    result = {
        "promoted": True,
        "reason": f"All gates passed — Sharpe +{delta:.4f}",
        "version": version,
        "strategy": strategy,
        "market": market,
        "delta": delta,
        "final_sharpe": final_sharpe,
        "comparisons": regression.get("comparisons", {}),
    }
    _notify(result)

    # Record in brain (non-blocking)
    try:
        from research.brain.writer import record_promotion
        prev_entry = _last_promotion_entry(market)
        prev_version = prev_entry.get("new_version", "unknown") if prev_entry else "unknown"
        record_promotion(
            strategy=strategy,
            market=market,
            prev_version=prev_version,
            new_version=version,
            delta_sharpe=round(delta, 6),
            metrics_comparison={
                "active": regression.get("baseline_metrics", {}),
                "candidate": regression.get("candidate_metrics", {}),
            },
            auto=True,
        )
    except Exception as exc:
        logger.warning("Brain record_promotion failed (non-fatal): %s", exc)

    logger.info(
        "✅ auto_promote: %s promoted to %s (Sharpe +%.4f)", strategy, version, delta,
    )
    return result


# ─── Gate 1: Cooldown ────────────────────────────────────────────────────────

def _check_cooldown(strategy: str) -> bool:
    """Return True if promotion is allowed (cooldown expired or never set)."""
    cooldowns = _load_cooldowns()
    last_iso = cooldowns.get(strategy)
    if not last_iso:
        return True
    try:
        last_dt = datetime.fromisoformat(last_iso)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        return elapsed >= COOLDOWN_SECONDS
    except Exception:
        return True  # Malformed timestamp — allow


def _load_cooldowns() -> dict:
    if COOLDOWN_PATH.exists():
        try:
            return json.loads(COOLDOWN_PATH.read_text())
        except Exception:
            pass
    return {}


def _update_cooldown(strategy: str) -> None:
    cooldowns = _load_cooldowns()
    cooldowns[strategy] = datetime.now(timezone.utc).isoformat()
    try:
        COOLDOWN_PATH.write_text(json.dumps(cooldowns, indent=2, default=str))
    except Exception as exc:
        logger.warning("Could not persist cooldown for %s: %s", strategy, exc)


# ─── Gate 2: Regression check ────────────────────────────────────────────────

def _regression_check(candidate_config: dict, market: str) -> dict:
    """Full portfolio backtest comparison: candidate vs active config.

    Returns:
        {
          pass: bool,
          baseline_metrics: dict,
          candidate_metrics: dict,
          comparisons: {metric: {baseline, candidate, delta, pct_change}},
        }
    """
    from scripts.strategy_evaluator import load_market_data, run_backtest
    from utils.config import get_active_config

    current_config = get_active_config(market)
    data = load_market_data(market)

    logger.info("Regression check: running baseline backtest…")
    baseline = run_backtest(current_config, data)
    logger.info("Regression check: running candidate backtest…")
    candidate = run_backtest(candidate_config, data)

    regression_ok = True
    comparisons: dict = {}

    # Percentage-based metrics — no degradation > 10%
    for metric in ("sharpe", "cagr_pct", "sortino", "profit_factor", "win_rate_pct"):
        b = float(baseline.get(metric) or 0)
        c = float(candidate.get(metric) or 0)
        delta = c - b
        pct_change = ((c - b) / abs(b) * 100) if b != 0 else 0.0
        comparisons[metric] = {
            "baseline": round(b, 4),
            "candidate": round(c, 4),
            "delta": round(delta, 4),
            "pct_change": round(pct_change, 2),
        }
        if pct_change < -10:
            regression_ok = False
            logger.warning("Regression: %s degraded %.1f%%", metric, pct_change)

    # Drawdown — lower is better; increase > 3pp is a fail
    b_dd = float(baseline.get("max_drawdown_pct") or 0)
    c_dd = float(candidate.get("max_drawdown_pct") or 0)
    dd_increase = c_dd - b_dd
    comparisons["max_drawdown_pct"] = {
        "baseline": round(b_dd, 4),
        "candidate": round(c_dd, 4),
        "delta": round(dd_increase, 4),
    }
    if dd_increase > 3.0:
        regression_ok = False
        logger.warning("Regression: drawdown increased by %.2fpp", dd_increase)

    # Trade count — drop > 20% blocks promotion
    b_trades = int(baseline.get("num_trades") or 0)
    c_trades = int(candidate.get("num_trades") or 0)
    trade_drop_pct = ((b_trades - c_trades) / b_trades * 100) if b_trades > 0 else 0.0
    comparisons["num_trades"] = {
        "baseline": b_trades,
        "candidate": c_trades,
        "delta": c_trades - b_trades,
        "pct_change": round(-trade_drop_pct, 2),
    }
    if trade_drop_pct > 20:
        regression_ok = False
        logger.warning("Regression: trade count dropped %.1f%%", trade_drop_pct)

    return {
        "pass": regression_ok,
        "baseline_metrics": baseline,
        "candidate_metrics": candidate,
        "comparisons": comparisons,
    }


# ─── Gate 3: Sanity bounds ────────────────────────────────────────────────────

def _sanity_check(metrics: dict) -> dict:
    """Absolute floors on portfolio-level metrics.

    Checks:
        • Sharpe > 0
        • CAGR > 0%
        • num_trades >= 20 (statistical significance)

    Returns:
        {pass: bool, reason: str}
    """
    sharpe = float(metrics.get("sharpe") or 0)
    cagr = float(metrics.get("cagr_pct") or 0)
    trades = int(metrics.get("num_trades") or 0)

    if sharpe <= 0:
        return {"pass": False, "reason": f"Sharpe {sharpe:.4f} ≤ 0"}
    if cagr <= 0:
        return {"pass": False, "reason": f"CAGR {cagr:.2f}% ≤ 0%"}
    if trades < 20:
        return {"pass": False, "reason": f"Only {trades} trades (min 20 required)"}

    return {"pass": True, "reason": "OK"}


# ─── Promotion write ──────────────────────────────────────────────────────────

def _do_promote(candidate_config: dict, market: str, metadata: dict) -> str:
    """Version current active config, write new active, append to promotion log.

    Returns:
        New version string (e.g. "v2.3").
    """
    from utils.config import save_config_version, get_active_config, VERSIONS_DIR

    # Capture the current version path BEFORE overwriting
    prev_active = get_active_config(market)
    prev_version = prev_active.get("version", "unknown")
    prev_config_path = str(VERSIONS_DIR / f"{market}_{prev_version}.json")

    # Write versioned copy + update active config
    version_path = save_config_version(candidate_config, market_id=market)
    new_version = candidate_config.get("version", str(version_path.name))

    # Append to promotion log
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "strategy": metadata.get("strategy"),
        "market": market,
        "prev_version": prev_version,
        "new_version": new_version,
        "prev_config_path": prev_config_path,
        "delta_sharpe": metadata.get("delta_sharpe"),
        "improvements": metadata.get("improvements", []),
        "baseline_metrics": metadata.get("baseline_metrics", {}),
        "candidate_metrics": metadata.get("candidate_metrics", {}),
        "auto": True,
    }
    _append_promotion_log(log_entry)

    logger.info("Config promoted: %s → %s for market %s", prev_version, new_version, market)
    return str(new_version)


def _append_promotion_log(entry: dict) -> None:
    PROMOTION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entries: list = []
    if PROMOTION_LOG_PATH.exists():
        try:
            entries = json.loads(PROMOTION_LOG_PATH.read_text())
        except Exception:
            entries = []
    entries.append(entry)
    PROMOTION_LOG_PATH.write_text(json.dumps(entries, indent=2, default=str))


def _last_promotion_entry(market: str) -> Optional[dict]:
    """Return the most recent promotion log entry for a market, or None."""
    if not PROMOTION_LOG_PATH.exists():
        return None
    try:
        entries = json.loads(PROMOTION_LOG_PATH.read_text())
        mkt_entries = [e for e in entries if e.get("market") == market]
        return mkt_entries[-1] if mkt_entries else None
    except Exception:
        return None


# ─── Telegram notification ────────────────────────────────────────────────────

def _notify(result: dict) -> None:
    """Send Telegram message. Rollback button attached on success."""
    try:
        from utils.telegram import send_message

        promoted = result.get("promoted", False)
        strategy = result.get("strategy", "unknown")
        market = result.get("market", "sp500")
        reason = result.get("reason", "")
        delta = result.get("delta", 0.0)
        version = result.get("version")

        if promoted:
            final_sharpe = result.get("final_sharpe", 0.0)
            comparisons = result.get("comparisons", {})

            # Build concise metric summary
            metric_lines = []
            for m in ("sharpe", "cagr_pct", "sortino", "profit_factor"):
                cmp = comparisons.get(m)
                if isinstance(cmp, dict):
                    sign = "+" if cmp["delta"] >= 0 else ""
                    metric_lines.append(
                        f"  {m}: {cmp['baseline']:.3f} → {cmp['candidate']:.3f} "
                        f"({sign}{cmp['delta']:.3f})"
                    )

            metrics_text = "\n".join(metric_lines)
            text = (
                f"✅ <b>Auto-promoted</b>: {strategy}\n"
                f"Market: {market.upper()} | Version: <code>{version}</code>\n"
                f"Sharpe: +{delta:.4f} → {final_sharpe:.4f}\n\n"
                f"<b>Portfolio metrics:</b>\n{metrics_text}"
            )
            # Rollback button: promote:{version}:rollback:{market}
            reply_markup = {
                "inline_keyboard": [[
                    {
                        "text": "↩️ Rollback",
                        "callback_data": f"promote:{version}:rollback:{market}",
                    }
                ]]
            }
            send_message(text, reply_markup=reply_markup)
        else:
            text = (
                f"⚠️ <b>Promotion blocked</b>: {strategy}\n"
                f"Market: {market.upper()}\n"
                f"Reason: {reason}"
            )
            send_message(text)

    except Exception as exc:
        logger.warning("_notify failed (non-fatal): %s", exc)


# ─── Rollback ─────────────────────────────────────────────────────────────────

def rollback(market: str) -> dict:
    """Restore previous config version from promotion_log.json.

    Reads the most recent log entry for `market`, copies the prev_config_path
    back to the active config, and appends a rollback entry to the log.

    Returns:
        {success: bool, message: str, version_restored: str|None}
    """
    last = _last_promotion_entry(market)
    if not last:
        return {
            "success": False,
            "message": f"No promotion log entries found for market '{market}'.",
            "version_restored": None,
        }

    prev_path = Path(last.get("prev_config_path", ""))
    prev_version = last.get("prev_version", "unknown")
    restored_from = last.get("new_version", "unknown")

    if not prev_path.exists():
        return {
            "success": False,
            "message": f"Previous config not found at {prev_path}",
            "version_restored": None,
        }

    try:
        from utils.config import ACTIVE_DIR
        import shutil

        active_path = ACTIVE_DIR / f"{market}.json"
        active_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(prev_path), str(active_path))
        logger.info("Rollback: restored %s → active (%s)", prev_version, market)
    except Exception as exc:
        return {
            "success": False,
            "message": f"Failed to copy config: {exc}",
            "version_restored": None,
        }

    # Log the rollback
    rollback_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "strategy": last.get("strategy"),
        "market": market,
        "action": "rollback",
        "restored_version": prev_version,
        "rolled_back_from": restored_from,
        "auto": False,
    }
    _append_promotion_log(rollback_entry)

    return {
        "success": True,
        "message": f"Rolled back {market} to {prev_version} (was {restored_from}).",
        "version_restored": prev_version,
    }
