"""Unified Auto-Promotion Pipeline for Atlas.

Called by sweep.py after each sweep cycle when a strategy has improved params.
Applies four validation gates before writing to active config:

    Gate 1: Cooldown (24h per strategy)
    Gate 2: Regression check (candidate vs active portfolio backtest)
    Gate 3: Sanity bounds (Sharpe > 0, CAGR > 0, ≥ 20 trades)
    Gate 4: OOS validation (time-split + perturbation robustness)

On pass: versions current config, writes new active, logs, notifies Telegram.
On fail: keeps candidate file, notifies why it was blocked.

Rollback: `rollback(market)` reads promotion_log.json and restores previous version.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import hashlib
import subprocess

logger = logging.getLogger(__name__)

ATLAS_ROOT = Path(__file__).resolve().parent.parent

# Cooldown: 24h per strategy (one promotion per day max)
COOLDOWN_PATH = Path("/tmp/promotion-cooldowns.json")
PROMOTION_LOG_PATH = ATLAS_ROOT / "config" / "promotion_log.json"
PENDING_PROMOTIONS_PATH = ATLAS_ROOT / "config" / "pending_promotions.json"
COOLDOWN_SECONDS = 86_400  # 24 hours

OOS_CACHE_DIR = ATLAS_ROOT / "config" / ".oos_cache"
OOS_CACHE_TTL_DAYS = 7



def _oos_cache_key(candidate_config: dict, market: str) -> str:
    """Deterministic cache key from config content + market."""
    # Hash the strategies section (the part that changes during sweeps)
    strat_str = json.dumps(candidate_config.get("strategies", {}), sort_keys=True)
    h = hashlib.sha256(f"{market}:{strat_str}".encode()).hexdigest()[:16]
    return f"oos_{market}_{h}"


def _get_cached_oos(cache_key: str) -> Optional[dict]:
    """Return cached OOS result if fresh (< 7 days), else None."""
    OOS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = OOS_CACHE_DIR / f"{cache_key}.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text())
        cached_at = datetime.fromisoformat(data["cached_at"])
        age_days = (datetime.now(timezone.utc) - cached_at).total_seconds() / 86400
        if age_days > OOS_CACHE_TTL_DAYS:
            logger.info("OOS cache expired (%.1f days old)", age_days)
            return None
        logger.info("OOS cache hit: %s (%.1f days old)", cache_key, age_days)
        return data
    except Exception as exc:
        logger.warning("OOS cache read failed: %s", exc)
        return None


def _save_oos_cache(cache_key: str, result: dict) -> None:
    """Persist OOS result to cache."""
    OOS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = OOS_CACHE_DIR / f"{cache_key}.json"
    result["cached_at"] = datetime.now(timezone.utc).isoformat()
    cache_file.write_text(json.dumps(result, indent=2, default=str))


def _run_oos_validation(candidate_config: dict, market: str) -> dict:
    """Run OOS validation via subprocess (calls scripts/validate_oos.py).

    Returns dict with keys: pass (bool), sharpe_oos, profit_factor_oos,
    cagr_degradation_pct, perturbation_pass_rate, reason (str).
    """
    import tempfile

    # Write candidate config to temp file for validate_oos.py to use
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, dir="/tmp"
    ) as f:
        json.dump(candidate_config, f, indent=2, default=str)
        tmp_config = f.name

    try:
        script = str(ATLAS_ROOT / "scripts" / "validate_oos.py")
        cmd = [
            "python3", script,
            "--config", tmp_config,
            "--market", market,
            "--output", "/tmp/oos_result.json",
        ]
        logger.info("Running OOS validation: %s", " ".join(cmd))
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=7200,
            cwd=str(ATLAS_ROOT),
        )

        if proc.returncode != 0:
            logger.warning("OOS validation script failed (rc=%d): %s", proc.returncode, proc.stderr[-500:] if proc.stderr else "no stderr")
            return {"pass": False, "reason": f"OOS script failed (rc={proc.returncode})"}

        # Parse output
        result_path = Path("/tmp/oos_result.json")
        if not result_path.exists():
            return {"pass": False, "reason": "OOS script produced no output file"}

        oos_data = json.loads(result_path.read_text())

        # Extract summary for verdicts (new-style v9.2+) or flat metrics (old-style)
        summary = oos_data.get("summary", {})

        # ── New-style validate_oos.py v9.2+: metrics nested under test sections ──
        t1 = oos_data.get("test1_time_period_split", {})
        t1_oos = t1.get("out_of_sample", {})
        t1_is = t1.get("in_sample", {})
        t2 = oos_data.get("test2_perturbation", {})
        n_trials = max(int(oos_data.get("n_perturbation_trials", 10)), 1)
        collapse_count = int(t2.get("collapse_count", 0))

        if t1_oos:
            # New-style: extract metrics from nested test1/test2 sections
            oos_sharpe = float(t1_oos.get("sharpe", 0) or 0)
            oos_pf = float(t1_oos.get("profit_factor", 0) or 0)
            is_cagr = float(t1_is.get("cagr_pct", 0) or 0)
            oos_cagr = float(t1_oos.get("cagr_pct", 0) or 0)
            # perturbation_rate: fraction of trials that did not collapse
            perturbation_rate = (n_trials - collapse_count) / n_trials
        else:
            # Old-style: flat keys in summary (backward compat)
            oos_sharpe = float(summary.get("oos_sharpe", summary.get("sharpe_oos", 0)))
            oos_pf = float(summary.get("oos_profit_factor", summary.get("profit_factor_oos", 0)))
            is_cagr = float(summary.get("is_cagr", summary.get("cagr_is", 0)))
            oos_cagr = float(summary.get("oos_cagr", summary.get("cagr_oos", 0)))
            perturbation_rate = float(summary.get("perturbation_pass_rate",
                                                  summary.get("robustness_pass_rate", 0)))

        cagr_degradation = ((is_cagr - oos_cagr) / abs(is_cagr) * 100) if is_cagr != 0 else 0

        # Apply gates
        failures = []
        if oos_sharpe <= 0:
            failures.append(f"OOS Sharpe {oos_sharpe:.4f} ≤ 0")
        if oos_pf <= 1.0:
            failures.append(f"OOS profit factor {oos_pf:.2f} ≤ 1.0")
        if cagr_degradation > 50:
            failures.append(f"CAGR degradation {cagr_degradation:.1f}% > 50%")
        if perturbation_rate < 0.70:
            failures.append(f"Perturbation pass rate {perturbation_rate:.0%} < 70%")

        # Authoritative cross-check: if validate_oos.py itself emits PASS,
        # trust it over any residual gate mismatch (defensive forward-compat).
        overall_verdict = summary.get("overall_verdict", "")
        if failures and overall_verdict.upper() == "PASS":
            logger.warning(
                "OOS extractor found failures %s but validate_oos.py "
                "overall_verdict=PASS — trusting script verdict",
                failures,
            )
            failures = []

        passed = len(failures) == 0
        reason = "OOS validation passed" if passed else "; ".join(failures)

        return {
            "pass": passed,
            "reason": reason,
            "sharpe_oos": oos_sharpe,
            "profit_factor_oos": oos_pf,
            "cagr_degradation_pct": round(cagr_degradation, 1),
            "perturbation_pass_rate": perturbation_rate,
            "raw": summary,
        }

    except subprocess.TimeoutExpired:
        return {"pass": False, "reason": "OOS validation timed out (2h limit)"}
    except Exception as exc:
        return {"pass": False, "reason": f"OOS validation error: {exc}"}
    finally:
        Path(tmp_config).unlink(missing_ok=True)

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

    # ── Gate 4: OOS validation ────────────────────────────────────────────────
    oos_cache_key = _oos_cache_key(candidate_config, market)
    oos_result = _get_cached_oos(oos_cache_key)

    if oos_result is None:
        logger.info("Running OOS validation (no cache hit)…")
        oos_result = _run_oos_validation(candidate_config, market)
        _save_oos_cache(oos_cache_key, oos_result)
    else:
        logger.info("Using cached OOS result: %s", oos_result.get("reason", ""))

    if not oos_result.get("pass", False):
        reason = f"OOS validation failed: {oos_result.get('reason', 'unknown')}"
        logger.info("📉 Promotion blocked: %s", reason)
        _notify({"promoted": False, "reason": reason, "strategy": strategy,
                 "market": market, "delta": delta,
                 "oos_result": oos_result})
        return {"promoted": False, "reason": reason, "version": None}

    logger.info(
        "OOS validation passed: Sharpe=%.4f PF=%.2f CAGR_deg=%.1f%% Perturb=%.0f%%",
        oos_result.get("sharpe_oos", 0), oos_result.get("profit_factor_oos", 0),
        oos_result.get("cagr_degradation_pct", 0),
        oos_result.get("perturbation_pass_rate", 0) * 100,
    )

    # Compute DSR for reporting
    dsr_info = {}
    try:
        from research.loop import _get_dsr_stats
        dsr_stats = _get_dsr_stats()
        if dsr_stats["num_experiments"] >= 5 and dsr_stats["variance_of_sharpes"] > 0:
            import numpy as np
            from scipy import stats as sp_stats
            n_exp = dsr_stats["num_experiments"]
            var_s = dsr_stats["variance_of_sharpes"]
            e_max_s = np.sqrt(var_s) * (
                (1 - np.euler_gamma) * sp_stats.norm.ppf(1 - 1 / n_exp)
                + np.euler_gamma * sp_stats.norm.ppf(1 - 1 / (n_exp * np.e))
            )
            dsr_info = {
                "dsr_expected_max_sharpe": round(float(e_max_s), 4),
                "dsr_num_experiments": n_exp,
                "dsr_significant": final_sharpe > e_max_s,
            }
    except Exception:
        pass

    # ── All gates passed — queue for Telegram approval ────────────────────────────
    metadata = {
        "strategy": strategy,
        "initial_sharpe": initial_sharpe,
        "final_sharpe": final_sharpe,
        "delta_sharpe": round(delta, 6),
        "improvements": improvements,
        "baseline_metrics": regression.get("baseline_metrics", {}),
        "candidate_metrics": candidate_metrics,
        "comparisons": regression.get("comparisons", {}),
        "oos_result": {k: v for k, v in oos_result.items() if k != "raw"},
        "dsr": dsr_info,
    }

    # Store pending promotion
    pending_entry = {
        "strategy": strategy,
        "market": market,
        "candidate_config": candidate_config,
        "metadata": metadata,
        "delta_sharpe": round(delta, 6),
        "final_sharpe": final_sharpe,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        pending_id = _add_pending(pending_entry)
    except Exception as exc:
        reason = f"Failed to store pending promotion: {exc}"
        logger.error("auto_promote: %s", reason)
        return {"promoted": False, "reason": reason, "version": None}

    # Send Telegram approval request with inline buttons
    _notify_approval_request(pending_id, strategy, market, delta, final_sharpe,
                              candidate_metrics, regression.get("comparisons", {}))

    # Record in brain (non-blocking)
    try:
        from research.brain.writer import record_promotion
        record_promotion(
            strategy=strategy,
            market=market,
            prev_version="pending",
            new_version=f"pending:{pending_id}",
            delta_sharpe=round(delta, 6),
            metrics_comparison={
                "active": regression.get("baseline_metrics", {}),
                "candidate": candidate_metrics,
            },
            auto=True,
        )
    except Exception as exc:
        logger.warning("Brain record_promotion failed (non-fatal): %s", exc)

    logger.info(
        "✅ auto_promote: %s passed all gates — pending approval (id=%s)", strategy, pending_id,
    )
    return {
        "promoted": False,
        "pending": True,
        "pending_id": pending_id,
        "reason": f"All gates passed — awaiting Telegram approval (Sharpe +{delta:.4f})",
        "version": None,
    }


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
    b_trades = int(baseline.get("total_trades") or baseline.get("num_trades") or 0)
    c_trades = int(candidate.get("total_trades") or candidate.get("num_trades") or 0)
    trade_drop_pct = ((b_trades - c_trades) / b_trades * 100) if b_trades > 0 else 0.0
    comparisons["total_trades"] = {
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
    trades = int(metrics.get("total_trades") or metrics.get("num_trades") or 0)

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


# ─── Pending promotions storage ───────────────────────────────────────────────────

def _load_pending() -> list:
    if PENDING_PROMOTIONS_PATH.exists():
        try:
            return json.loads(PENDING_PROMOTIONS_PATH.read_text())
        except Exception:
            return []
    return []


def _save_pending(entries: list) -> None:
    PENDING_PROMOTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PROMOTIONS_PATH.write_text(json.dumps(entries, indent=2, default=str))


def _add_pending(entry: dict) -> str:
    """Add a pending promotion. Returns the pending ID."""
    import hashlib
    pending_id = hashlib.sha256(
        f"{entry['strategy']}:{entry['market']}:{entry['timestamp']}".encode()
    ).hexdigest()[:12]
    entry["pending_id"] = pending_id
    entry["status"] = "pending"
    entries = _load_pending()
    entries.append(entry)
    _save_pending(entries)
    return pending_id


def complete_pending_promotion(pending_id: str) -> dict:
    """Called when user taps APPROVE. Executes the stored promotion."""
    entries = _load_pending()
    target = None
    for e in entries:
        if e.get("pending_id") == pending_id:
            target = e
            break
    if not target:
        return {"promoted": False, "reason": f"Pending promotion {pending_id} not found"}
    if target.get("status") != "pending":
        return {"promoted": False, "reason": f"Already {target.get('status')}"}

    # Execute the actual promotion
    try:
        version = _do_promote(target["candidate_config"], target["market"], target["metadata"])
    except Exception as exc:
        target["status"] = "error"
        target["error"] = str(exc)
        _save_pending(entries)
        return {"promoted": False, "reason": f"Promotion write failed: {exc}"}

    # Update cooldown
    _update_cooldown(target["strategy"])

    # Mark as approved
    target["status"] = "approved"
    target["approved_at"] = datetime.now(timezone.utc).isoformat()
    target["version"] = str(version)
    _save_pending(entries)

    # Record in brain
    try:
        from research.brain.writer import record_promotion
        record_promotion(
            strategy=target["strategy"],
            market=target["market"],
            prev_version="unknown",
            new_version=str(version),
            delta_sharpe=target.get("delta_sharpe", 0),
            metrics_comparison={
                "active": target.get("metadata", {}).get("baseline_metrics", {}),
                "candidate": target.get("metadata", {}).get("candidate_metrics", {}),
            },
            auto=True,
        )
    except Exception:
        pass

    return {
        "promoted": True,
        "version": str(version),
        "strategy": target["strategy"],
        "market": target["market"],
    }


def reject_pending_promotion(pending_id: str, reason: str = "User rejected") -> dict:
    """Called when user taps REJECT."""
    entries = _load_pending()
    for e in entries:
        if e.get("pending_id") == pending_id:
            e["status"] = "rejected"
            e["rejected_at"] = datetime.now(timezone.utc).isoformat()
            e["reject_reason"] = reason
            _save_pending(entries)
            return {"rejected": True, "strategy": e.get("strategy")}
    return {"rejected": False, "reason": "Not found"}


def expire_pending_promotions() -> list:
    """Expire any pending promotions older than 24h. Returns list of expired IDs."""
    entries = _load_pending()
    expired = []
    now = datetime.now(timezone.utc)
    for e in entries:
        if e.get("status") != "pending":
            continue
        ts = e.get("timestamp", "")
        try:
            created = datetime.fromisoformat(ts)
            if (now - created).total_seconds() > 86400:
                e["status"] = "expired"
                e["expired_at"] = now.isoformat()
                expired.append(e.get("pending_id"))
        except Exception:
            continue
    if expired:
        _save_pending(entries)
        # Notify via Telegram
        try:
            from utils.telegram import send_message
            for pid in expired:
                entry = next((e for e in entries if e.get("pending_id") == pid), None)
                if entry:
                    send_message(
                        f"⏰ <b>Promotion expired</b>: {entry.get('strategy')}\n"
                        f"Market: {entry.get('market', '').upper()}\n"
                        f"No response within 24h — auto-expired."
                    )
        except Exception:
            pass
    return expired


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
            # Escape HTML-special chars in reason (gate msgs may contain < > &)
            _html_reason = (
                reason.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            text = (
                f"⚠️ <b>Promotion blocked</b>: {strategy}\n"
                f"Market: {market.upper()}\n"
                f"Reason: {_html_reason}"
            )
            send_message(text)

    except Exception as exc:
        logger.warning("_notify failed (non-fatal): %s", exc)


# ─── Telegram approval request ───────────────────────────────────────────────────

def _notify_approval_request(pending_id: str, strategy: str, market: str,
                              delta: float, final_sharpe: float,
                              metrics: dict, comparisons: dict) -> None:
    """Send Telegram message with Approve/Reject inline buttons."""
    try:
        from utils.telegram import send_message

        # Build metric summary
        metric_lines = []
        for m in ("sharpe", "cagr_pct", "sortino", "profit_factor", "win_rate_pct"):
            cmp = comparisons.get(m)
            if isinstance(cmp, dict):
                sign = "+" if cmp.get("delta", 0) >= 0 else ""
                metric_lines.append(
                    f"  {m}: {cmp['baseline']:.3f} → {cmp['candidate']:.3f} "
                    f"({sign}{cmp['delta']:.3f})"
                )

        # Trade count
        tc = comparisons.get("total_trades", {})
        if isinstance(tc, dict):
            metric_lines.append(f"  trades: {tc.get('baseline', '?')} → {tc.get('candidate', '?')}")

        # Max drawdown
        dd = comparisons.get("max_drawdown_pct", {})
        if isinstance(dd, dict):
            metric_lines.append(f"  max_dd: {dd.get('baseline', 0):.1f}% → {dd.get('candidate', 0):.1f}%")

        metrics_text = "\n".join(metric_lines)

        text = (
            f"🔔 <b>Promotion Approval Required</b>\n\n"
            f"Strategy: <b>{strategy}</b>\n"
            f"Market: {market.upper()}\n"
            f"Sharpe Δ: <b>+{delta:.4f}</b> → {final_sharpe:.4f}\n\n"
            f"<b>Portfolio metrics:</b>\n{metrics_text}\n\n"
            f"<i>Auto-expires in 24h if no response.</i>"
        )

        reply_markup = {
            "inline_keyboard": [[
                {"text": "✅ APPROVE", "callback_data": f"sweep_promote:{pending_id}:approve:{market}"},
                {"text": "❌ REJECT", "callback_data": f"sweep_promote:{pending_id}:reject:{market}"},
            ]]
        }

        send_message(text, reply_markup=reply_markup)
        logger.info("Telegram approval request sent for %s (pending_id=%s)", strategy, pending_id)

    except Exception as exc:
        logger.warning("_notify_approval_request failed (non-fatal): %s", exc)


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
