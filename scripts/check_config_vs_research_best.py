#!/usr/bin/env python3
"""Config vs research_best drift monitor.

Weekly drift monitor.  Alert via Telegram when a research_best row has been
stable ≥14 days with Sharpe improvement ≥0.10 vs the live
config/active/{market}.json — i.e., an overdue promotion candidate.

Also alerts when a row has been stable ≥30 days regardless of Sharpe delta
(aging signal — research may be stale).

Expected cron entry (Worker B adds to pi-cron.sh):
    0 9 * * 1 /usr/bin/flock -n /tmp/drift_monitor.lock bash -c \\
        'cd /root/atlas && timeout 5m python3 scripts/check_config_vs_research_best.py --notify' \\
        >> /root/atlas/logs/drift_monitor.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

DB_PATH = ATLAS_ROOT / "data" / "atlas.db"
CONFIG_ACTIVE_DIR = ATLAS_ROOT / "config" / "active"

DAYS_STABLE_ALERT = 14
SHARPE_DELTA_ALERT = 0.10
DAYS_AGING_ALERT = 30

logger = logging.getLogger(__name__)


# ─── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [drift] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ─── Data loading ─────────────────────────────────────────────────────────────

def _load_active_configs(config_dir: Path) -> dict[str, dict]:
    """Load all config/active/*.json. Returns {market_id: config_dict}."""
    configs: dict[str, dict] = {}
    if not config_dir.exists():
        logger.warning("Config active directory not found: %s", config_dir)
        return configs
    for f in sorted(config_dir.glob("*.json")):
        market_id = f.stem
        try:
            configs[market_id] = json.loads(f.read_text())
        except Exception as exc:
            logger.warning("Could not load %s: %s", f, exc)
    return configs


def _get_config_sharpe_baseline(config: dict, strategy: str) -> float | None:
    """Extract Sharpe baseline for a strategy from a config dict.

    Checks config['baselines'][strategy]['sharpe'] first, then
    config['strategies'][strategy]['sharpe'] (some configs embed it).
    Returns None if not found.
    """
    baselines = config.get("baselines") or {}
    if strategy in baselines:
        bs = baselines[strategy]
        if isinstance(bs, dict) and "sharpe" in bs:
            return float(bs["sharpe"])
        if isinstance(bs, (int, float)):
            return float(bs)

    strat_cfg = (config.get("strategies") or {}).get(strategy)
    if isinstance(strat_cfg, dict) and "sharpe" in strat_cfg:
        return float(strat_cfg["sharpe"])

    return None


def _load_research_best_all(db_path: Path) -> list[dict]:
    """Load all rows from research_best."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT strategy, universe, sharpe, trades, max_dd_pct, updated_at "
            "FROM research_best"
        )
        return [dict(r) for r in cursor.fetchall()]


def _parse_updated_at(updated_at_str: str | None) -> datetime | None:
    """Parse updated_at string (various formats) to a UTC-aware datetime."""
    if not updated_at_str:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            dt = datetime.strptime(updated_at_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    logger.warning("Could not parse updated_at: %r", updated_at_str)
    return None


# ─── Analysis ─────────────────────────────────────────────────────────────────

def _build_analysis(
    configs: dict[str, dict],
    research_rows: list[dict],
    now: datetime | None = None,
) -> dict:
    """Compare research_best vs live configs and classify rows.

    Returns dict with keys:
        overdue  — strategies ≥14d stable with ≥0.10 Sharpe improvement
        aging    — strategies ≥30d stable (any delta)
        all      — full enriched row list
    """
    now = now or datetime.now(timezone.utc)

    # Build lookup: (strategy, universe) → config_sharpe_baseline
    config_baselines: dict[tuple[str, str], float | None] = {}
    for market_id, config in configs.items():
        strats = list((config.get("strategies") or {}).keys())
        for strategy in strats:
            config_baselines[(strategy, market_id)] = _get_config_sharpe_baseline(
                config, strategy
            )

    overdue: list[dict] = []
    aging: list[dict] = []
    all_rows: list[dict] = []

    for row in research_rows:
        strategy = row["strategy"]
        universe = row["universe"]
        sharpe_research = float(row.get("sharpe") or 0.0)
        updated_at = _parse_updated_at(row.get("updated_at"))

        if updated_at is None:
            continue

        days_stable = (now - updated_at).total_seconds() / 86400.0
        config_sharpe = config_baselines.get((strategy, universe))

        if config_sharpe is not None:
            sharpe_delta = sharpe_research - config_sharpe
        else:
            sharpe_delta = 0.0  # no baseline — only days_stable check applies

        entry = {
            "strategy": strategy,
            "universe": universe,
            "days_stable": round(days_stable, 1),
            "sharpe_research": round(sharpe_research, 4),
            "sharpe_config": round(config_sharpe, 4) if config_sharpe is not None else None,
            "sharpe_delta": round(sharpe_delta, 4),
            "updated_at": row.get("updated_at", ""),
            "has_config_baseline": config_sharpe is not None,
        }
        all_rows.append(entry)

        is_overdue = (
            days_stable >= DAYS_STABLE_ALERT
            and sharpe_delta >= SHARPE_DELTA_ALERT
            and config_sharpe is not None
        )
        is_aging = days_stable >= DAYS_AGING_ALERT

        if is_overdue:
            overdue.append(entry)
        if is_aging:
            aging.append(entry)

    return {"overdue": overdue, "aging": aging, "all": all_rows}


# ─── Output formatting ────────────────────────────────────────────────────────

def _format_plain(analysis: dict, now: datetime) -> str:
    overdue = analysis["overdue"]
    aging = analysis["aging"]
    all_rows = analysis["all"]

    lines: list[str] = [
        f"Config vs research_best drift monitor — {now.strftime('%Y-%m-%d')}",
        "",
        f"OVERDUE PROMOTIONS (≥{DAYS_STABLE_ALERT} days stable, "
        f"≥{SHARPE_DELTA_ALERT} Sharpe improvement):",
    ]

    if overdue:
        for item in overdue:
            dt_str = item["updated_at"][:10] if len(item["updated_at"]) >= 10 else item["updated_at"]
            lines.append(f"  {item['strategy']}/{item['universe']}:")
            lines.append(f"    config baseline: Sharpe {item['sharpe_config']:.4f}")
            lines.append(
                f"    research_best:   Sharpe {item['sharpe_research']:.4f} "
                f"(updated {dt_str}, {item['days_stable']:.0f} days ago)"
            )
            lines.append(f"    delta: +{item['sharpe_delta']:.4f} — ALERT")
    else:
        # Show informational breakdown for rows that have baselines
        rows_with_baseline = [r for r in all_rows if r["has_config_baseline"]]
        if rows_with_baseline:
            for item in rows_with_baseline:
                dt_str = item["updated_at"][:10] if len(item["updated_at"]) >= 10 else item["updated_at"]
                delta_str = (
                    f"+{item['sharpe_delta']:.4f}"
                    if item["sharpe_delta"] >= 0
                    else f"{item['sharpe_delta']:.4f}"
                )
                below_thresh = (
                    f"below {SHARPE_DELTA_ALERT} threshold, no alert"
                    if item["sharpe_delta"] < SHARPE_DELTA_ALERT
                    else "ALERT"
                )
                lines.append(
                    f"  {item['strategy']}/{item['universe']}:\n"
                    f"    config baseline: Sharpe {item['sharpe_config']:.4f}\n"
                    f"    research_best:   Sharpe {item['sharpe_research']:.4f} "
                    f"(updated {dt_str}, {item['days_stable']:.0f} days ago)\n"
                    f"    delta: {delta_str} — {below_thresh}"
                )
        else:
            lines.append("  (no strategies with config baselines found)")

    lines.extend(["", f"AGING BEST-PARAMS (≥{DAYS_AGING_ALERT} days stable):"])
    if aging:
        for item in aging:
            dt_str = item["updated_at"][:10] if len(item["updated_at"]) >= 10 else item["updated_at"]
            lines.append(
                f"  {item['strategy']}/{item['universe']}: "
                f"stable {item['days_stable']:.0f} days (updated {dt_str})"
            )
    else:
        lines.append("  (none)")

    lines.extend([
        "",
        f"TOTAL: {len(overdue)} overdue, {len(aging)} aging.",
    ])
    return "\n".join(lines)


def _format_json(analysis: dict, now: datetime) -> str:
    output = {
        "as_of": now.isoformat(),
        "thresholds": {
            "days_stable_alert": DAYS_STABLE_ALERT,
            "sharpe_delta_alert": SHARPE_DELTA_ALERT,
            "days_aging_alert": DAYS_AGING_ALERT,
        },
        "overdue_count": len(analysis["overdue"]),
        "aging_count": len(analysis["aging"]),
        "overdue": analysis["overdue"],
        "aging": analysis["aging"],
    }
    return json.dumps(output, indent=2)


# ─── Telegram ─────────────────────────────────────────────────────────────────

def _send_telegram_alert(analysis: dict) -> bool:
    """Send Telegram alert if any overdue or aging entries exist."""
    overdue = analysis["overdue"]
    aging = analysis["aging"]

    if not overdue and not aging:
        return True  # nothing to send

    lines: list[str] = ["⚠️ <b>Atlas — Config Drift Monitor</b>", ""]

    if overdue:
        lines.append(
            f"{len(overdue)} strategies have promotion-candidate improvements waiting:"
        )
        for item in overdue:
            lines.append(
                f"- {item['strategy']}/{item['universe']}: "
                f"+{item['sharpe_delta']:.4f} Sharpe ({item['days_stable']:.0f}d stable)"
            )
        lines.append("")

    if aging:
        lines.append(
            f"{len(aging)} strategies have aging best-params (≥{DAYS_AGING_ALERT}d):"
        )
        for item in aging[:10]:  # cap to avoid Telegram message overflow
            lines.append(
                f"- {item['strategy']}/{item['universe']}: "
                f"{item['days_stable']:.0f}d stable"
            )
        if len(aging) > 10:
            lines.append(f"  ... and {len(aging) - 10} more")

    lines.extend([
        "",
        "Review: run python3 scripts/check_config_vs_research_best.py for full report",
    ])

    message = "\n".join(lines)
    try:
        from utils.telegram import send_message  # noqa: PLC0415
        return send_message(message)
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
        return False


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Weekly config vs research_best drift monitor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 scripts/check_config_vs_research_best.py\n"
            "  python3 scripts/check_config_vs_research_best.py --notify\n"
            "  python3 scripts/check_config_vs_research_best.py --json\n"
        ),
    )
    parser.add_argument("--notify", action="store_true",
                        help="Send Telegram if any alerts")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Machine-readable JSON output")
    parser.add_argument(
        "--db",
        default=str(DB_PATH),
        help=f"SQLite DB path (default: {DB_PATH})",
    )
    parser.add_argument(
        "--config-dir",
        default=str(CONFIG_ACTIVE_DIR),
        help="Config active directory",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _setup_logging()

    db_path = Path(args.db)
    config_dir = Path(args.config_dir)
    now = datetime.now(timezone.utc)

    if not db_path.exists():
        logger.error("Database not found: %s", db_path)
        return 1

    configs = _load_active_configs(config_dir)
    if not configs:
        logger.warning("No active configs loaded from %s", config_dir)

    research_rows = _load_research_best_all(db_path)
    analysis = _build_analysis(configs, research_rows, now=now)

    if args.json_output:
        print(_format_json(analysis, now))
    else:
        print(_format_plain(analysis, now))

    if args.notify:
        sent = _send_telegram_alert(analysis)
        if not sent:
            logger.warning("Telegram alert failed")

    # Exit 1 when there are actionable overdue promotions
    return 1 if analysis["overdue"] else 0


if __name__ == "__main__":
    sys.exit(main())
