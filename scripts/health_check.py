#!/usr/bin/env python3
"""Performance Health Check for Atlas Trading System.

Runs a quick backtest on last 6 months of data, compares to stored baseline,
and flags degradation. Exits 0 (healthy) or 1 (degraded).

Usage: python3 scripts/health_check.py

NOTE — INFORMATIONAL ONLY: this script is not consumed by alerters or watchdogs.
The BASELINE strategy mix below (v9.3 robust blend) predates the live v3.2.1 config
which runs momentum_breakout + connors_rsi2 only.  build_strategies() instantiates
only the 4 legacy strategies (mean_reversion / trend_following / bb_squeeze /
opening_gap), all of which are disabled in config/active/sp500.json — so the
sp500 backtest always produces 0 trades, CAGR=0%, and trips the DEGRADED flag.

The only active consumers of this file are:
  - _check_overlay_backlog()  — imported by healthz_hourly.sh (unrelated to DEGRADED)
  - check_equity_config_sum() — imported by check_equity_config_sum.py (unrelated to DEGRADED)

auto_reoptimize.py reads the health_check JSON and would act on DEGRADED, but that
script has no cron entry, systemd timer, or caller — it is not scheduled.

Do NOT update the BASELINE constant in response to DEGRADED output; treat any
DEGRADED for sp500 as a stale-comparison artifact.  Source of truth for the live
strategy mix is config/active/<market>.json.

Last reviewed: 2026-05-04 (Validation audit closeout — Diag B).
"""
import sys, json, time, argparse
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import pandas as pd
from backtest.engine import BacktestEngine
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.bb_squeeze import BBSqueeze
from strategies.opening_gap import OpeningGap

DATA_DIR = PROJECT_ROOT / 'data' / 'cache'
CONFIG_DIR = PROJECT_ROOT / 'config'
LOGS_DIR = PROJECT_ROOT / 'logs'

# Universes that are known-passive (no live trading); health check is skipped for these.
# Also checked via trading.live_enabled field in the config JSON.
PASSIVE_UNIVERSES: frozenset[str] = frozenset({'asx', 'crypto'})


def _is_inactive(cfg: dict) -> bool:
    """Return True if the universe config marks it as inactive/passive.

    Priority order:
      1. trading.live_enabled is explicitly False  → inactive
      2. trading.live_enabled field absent AND market in PASSIVE_UNIVERSES → inactive
      3. Everything else → active (run the full health check)
    """
    trading = cfg.get('trading', {})
    live_enabled = trading.get('live_enabled')  # None means key absent

    if live_enabled is False:
        return True

    if live_enabled is None:
        market = cfg.get('market', '').lower()
        return market in PASSIVE_UNIVERSES

    return False


# Baseline metrics from v9.3 robust blend (full-period)
# Update these when a new config is promoted to active
BASELINE = {
    'cagr': 11.15,
    'sharpe': 0.6806,
    'profit_factor': 1.4059,
    'max_drawdown': 7.07,
}

# Degradation thresholds
THRESHOLDS = {
    'cagr_drop_pct': 50,       # Flag if CAGR drops >50% from baseline
    'sharpe_floor': 0.0,       # Flag if Sharpe goes negative
    'pf_floor': 1.0,           # Flag if Profit Factor drops below 1.0
}

def load_data_recent(months=18, min_rows=60, universe: str | None = None):
    """Load only the last ~N months of data for quick health check.

    If ``universe`` is given, scopes to data/cache/<universe>/*.parquet.
    Otherwise recurses across all subdirs (data/cache/**/*.parquet) for
    backwards compatibility with the old flat layout.
    """
    dd = {}
    cutoff = pd.Timestamp.now() - pd.DateOffset(months=months)

    if universe:
        scan_dir = DATA_DIR / universe
        if not scan_dir.exists():
            # Missing universe dir is non-fatal — return empty dict, caller decides
            return dd
        files = sorted(scan_dir.glob('*.parquet'))
    else:
        files = sorted(DATA_DIR.rglob('*.parquet'))

    for pf in files:
        if pf.stem == 'IOZ_AX':
            continue
        ticker = pf.stem.replace('_AX', '.AX')
        try:
            df = pd.read_parquet(pf)
        except Exception:
            continue
        df.columns = [c.lower() for c in df.columns]
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')
        df.index = pd.to_datetime(df.index)
        df = df[df.index >= cutoff]
        if len(df) >= min_rows:
            dd[ticker] = df
    return dd

def build_strategies(cfg):
    s = []
    if cfg['strategies'].get('mean_reversion', {}).get('enabled', True):
        s.append(MeanReversion(cfg))
    if cfg['strategies'].get('trend_following', {}).get('enabled', True):
        s.append(TrendFollowing(cfg))
    if cfg['strategies'].get('bb_squeeze', {}).get('enabled', True):
        s.append(BBSqueeze(cfg))
    if cfg['strategies'].get('opening_gap', {}).get('enabled', True):
        s.append(OpeningGap(cfg))
    return s

def norm_metric(val):
    """Normalize fractional metric to percentage if needed."""
    if val is not None and abs(val) < 2:
        return val * 100
    return val


def resolve_path(path_value, default_path):
    p = Path(path_value) if path_value else Path(default_path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Run Atlas health check against a specified config and write a report JSON."
    )
    parser.add_argument(
        '--config-path',
        type=str,
        default=None,
        help='Config JSON path (default: config/active/asx.json)',
    )
    parser.add_argument(
        '--report-path',
        type=str,
        default=None,
        help='Output report JSON path (default: logs/health_check_YYYY-MM-DD.json)',
    )
    parser.add_argument(
        '--months',
        type=int,
        default=18,
        help='Recent data window in months (default: 18)',
    )
    return parser.parse_args(argv)

def main(argv: list | None = None) -> int:
    args = parse_args(argv)
    t0 = time.time()
    today = datetime.now().strftime('%Y-%m-%d')
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    default_report_path = LOGS_DIR / f'health_check_{today}.json'
    report_path = resolve_path(args.report_path, default_report_path)
    cfg_path = resolve_path(args.config_path, CONFIG_DIR / 'active' / 'asx.json')

    print(f"=== Atlas Health Check ({today}) ===")
    print(f"Baseline: CAGR={BASELINE['cagr']:.2f}% Sh={BASELINE['sharpe']:.4f} PF={BASELINE['profit_factor']:.4f}")

    # Load config
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        universe = cfg_path.stem
        print(f"Universe {universe} decommissioned (config absent at {cfg_path}), skipping")
        report = {
            'date': today,
            'status': 'SKIPPED',
            'message': f'Universe {universe} decommissioned (config absent at {cfg_path})',
            'config_path': str(cfg_path),
            'report_path': str(report_path),
            'runtime_s': round(time.time() - t0, 1),
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, 'w') as f:
            json.dump(report, f, indent=2)
        sys.exit(0)
    print(f"Config: {cfg.get('version', 'unknown')}")
    print(f"Config path: {cfg_path}")

    # Skip inactive/passive universes — exit 0, write SKIPPED report
    if _is_inactive(cfg):
        universe = cfg.get('market', cfg_path.stem)
        report = {
            'date': today,
            'status': 'SKIPPED',
            'message': f'Universe {universe} is inactive — health check not applicable',
            'config_path': str(cfg_path),
            'report_path': str(report_path),
            'runtime_s': round(time.time() - t0, 1),
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, 'w') as f:
            json.dump(report, f, indent=2)
        print(f"SKIPPED: Universe {universe} is inactive")
        sys.exit(0)

    # Load recent data
    print(f"Loading last {args.months} months of data...")
    data = load_data_recent(months=args.months, min_rows=60, universe=cfg.get('market'))
    print(f"  {len(data)} tickers loaded")

    if len(data) < 10:
        report = {
            'date': today,
            'status': 'ERROR',
            'message': f'Insufficient tickers: {len(data)} < 10',
            'config_path': str(cfg_path),
            'report_path': str(report_path),
            'runtime_s': round(time.time() - t0, 1),
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, 'w') as f:
            json.dump(report, f, indent=2)
        print(f"ERROR: {report['message']}")
        sys.exit(1)
        return 1

    # Run backtest
    print("Running backtest on recent data...")
    engine = BacktestEngine(cfg)
    strategies = build_strategies(cfg)
    result = engine.run_walkforward(data, strategies)
    m = result.metrics
    elapsed = time.time() - t0

    # Normalize metrics
    cagr = norm_metric(m.get('cagr', 0))
    sharpe = m.get('sharpe', 0)
    pf = m.get('profit_factor', 0)
    maxdd = norm_metric(m.get('max_drawdown', 0))
    trades = m.get('total_trades', 0)

    print(f"  CAGR={cagr:.2f}% Sharpe={sharpe:.4f} PF={pf:.4f} MaxDD={maxdd:.2f}% Trades={trades}")

    # Check degradation
    flags = []
    if BASELINE['cagr'] > 0:
        cagr_drop = ((BASELINE['cagr'] - cagr) / BASELINE['cagr']) * 100
        if cagr_drop > THRESHOLDS['cagr_drop_pct']:
            flags.append(f"CAGR degraded {cagr_drop:.1f}% from baseline ({cagr:.2f}% vs {BASELINE['cagr']:.2f}%)")

    if sharpe < THRESHOLDS['sharpe_floor']:
        flags.append(f"Sharpe negative: {sharpe:.4f}")

    if pf < THRESHOLDS['pf_floor']:
        flags.append(f"Profit Factor below 1.0: {pf:.4f}")

    status = 'DEGRADED' if flags else 'HEALTHY'

    report = {
        'date': today,
        'config_version': cfg.get('version', 'unknown'),
        'config_path': str(cfg_path),
        'report_path': str(report_path),
        'status': status,
        'metrics': {
            'cagr_pct': round(cagr, 4),
            'sharpe': round(sharpe, 4),
            'profit_factor': round(pf, 4),
            'max_drawdown_pct': round(maxdd, 4),
            'total_trades': trades,
        },
        'baseline': BASELINE,
        'thresholds': THRESHOLDS,
        'flags': flags,
        'tickers_tested': len(data),
        'data_window_months': args.months,
        'runtime_s': round(elapsed, 1),
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, 'w') as f:
        json.dump(report, f, indent=2)

    print(f"\nStatus: {status}")
    if flags:
        for flag in flags:
            print(f"  ⚠ {flag}")
    print(f"Report: {report_path}")
    print(f"Runtime: {elapsed:.1f}s")

    sys.exit(0 if status == 'HEALTHY' else 1)
    return 0 if status == 'HEALTHY' else 1



# ── Equity-sum config guard ────────────────────────────────────────────────
#
# KEEP-LOUD ALERT: Σ(active_configs.starting_equity) ≤ broker.equity × 1.05
# A violation means config drift — per-market equity claims exceed real capital.
# This guard must fail LOUD (Telegram alert) so operators can recalibrate.

def check_equity_config_sum(
    config_dir: Path | None = None,
    db_path: Path | None = None,
    tolerance: float = 0.05,
    dry_run: bool = False,
) -> tuple[bool, dict]:
    """Assert Σ(live market starting_equity) ≤ broker.equity × (1 + tolerance).

    Reads ``market_equity_history`` for the latest broker equity snapshot
    (avoids a live broker connection).  Active markets are those with
    ``trading.live_enabled = true``.

    Parameters
    ----------
    config_dir:  Defaults to ``<project_root>/config/active``.
    db_path:     Defaults to ``<project_root>/data/atlas.db``.
    tolerance:   Fractional overage allowed (default 0.05 = 5%).
    dry_run:     Print alert text instead of sending Telegram.

    Returns
    -------
    (ok, info_dict)
        ok=True if constraint satisfied, False if violated.
    """
    import glob
    import sqlite3

    config_dir = config_dir or (PROJECT_ROOT / 'config' / 'active')
    db_path = db_path or (PROJECT_ROOT / 'data' / 'atlas.db')

    # ── Step 1: sum starting_equity for all live-enabled configs ─────────────
    active_equities: dict[str, float] = {}
    for cfg_path in sorted(Path(config_dir).glob('*.json')):
        try:
            with open(cfg_path) as fh:
                cfg = json.load(fh)
        except Exception:
            continue

        trading = cfg.get('trading', {})
        live_enabled = trading.get('live_enabled')
        if live_enabled is not True:
            continue  # skip inactive/passive configs

        market = cfg.get('market_id') or cfg_path.stem
        se = cfg.get('risk', {}).get('starting_equity', 0)
        if se and se > 0:
            active_equities[market] = float(se)

    equity_sum = sum(active_equities.values())

    # ── Step 2: read latest broker equity from market_equity_history ─────────
    broker_equity: float | None = None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT broker_equity, date
            FROM market_equity_history
            ORDER BY date DESC, created_at DESC
            LIMIT 1
            """
        ).fetchone()
        conn.close()
        if row:
            broker_equity = float(row['broker_equity'])
    except Exception as exc:
        print(f"WARNING: check_equity_config_sum DB read failed: {exc}", file=sys.stderr)

    info = {
        'equity_sum': round(equity_sum, 2),
        'broker_equity': broker_equity,
        'tolerance': tolerance,
        'active_markets': active_equities,
    }

    if broker_equity is None:
        # Cannot assess — no snapshot available; treat as OK to avoid false positives
        info['status'] = 'UNKNOWN'
        info['reason'] = 'market_equity_history table empty or unreadable'
        return True, info

    limit = broker_equity * (1.0 + tolerance)
    ok = equity_sum <= limit
    info['limit'] = round(limit, 2)
    info['status'] = 'OK' if ok else 'VIOLATION'
    info['violated_by'] = round(equity_sum - limit, 2) if not ok else 0.0

    if not ok:
        alert_lines = [
            f"🚨 <b>Equity Config Drift — RCA #6 Guard</b>",
            f"Σ(starting_equity) = ${equity_sum:,.2f} exceeds broker equity × {1+tolerance:.0%} (${limit:,.2f})",
            f"Broker equity (last snapshot): ${broker_equity:,.2f}",
            f"Excess: ${equity_sum - limit:,.2f}",
            "",
            "Active markets:",
        ]
        for m, v in sorted(active_equities.items()):
            alert_lines.append(f"  • {m}: ${v:,.2f}")
        alert_lines.append("")
        alert_lines.append(
            "Fix: run <code>scripts/recalibrate_starting_equity.py --apply</code> "
            "or manually update <code>risk.starting_equity</code> in config/active/*.json"
        )
        alert_text = "\n".join(alert_lines)

        if dry_run:
            print(f"[DRY-RUN] Would send Telegram: {alert_text}")
        else:
            try:
                from utils.telegram import send_message
                send_message(alert_text)
            except Exception as tg_exc:
                print(f"WARNING: Telegram alert failed (non-fatal): {tg_exc}", file=sys.stderr)

    return ok, info


# ── Overlay evaluator backlog check ──────────────────────────────────────────

def _check_overlay_backlog(threshold: int = 5, dry_run: bool = False) -> bool:
    """Check for stale unevaluated overlay decisions and alert via Telegram.

    Wraps :func:`overlay.evaluator.check_evaluator_backlog`.  When the backlog
    exceeds *threshold* (decisions older than 2 days with outcome_evaluated=0),
    a WARN-level Telegram alert is sent.

    Parameters
    ----------
    threshold:
        Number of stale decisions above which the alert fires (default 5).
    dry_run:
        When ``True``, print the alert text instead of sending to Telegram.

    Returns
    -------
    bool
        ``True`` when healthy (backlog within threshold), ``False`` otherwise.
    """
    try:
        from overlay.evaluator import check_evaluator_backlog  # type: ignore
        is_healthy, backlog_count, oldest_age_days = check_evaluator_backlog(threshold=threshold)
    except Exception as exc:
        print(f"WARNING: _check_overlay_backlog import/call failed: {exc}", file=sys.stderr)
        return True  # non-fatal — don't block other checks

    if is_healthy:
        print(f"Overlay evaluator backlog OK: {backlog_count} stale decisions (threshold={threshold})")
        return True

    alert_text = (
        f"\u26a0\ufe0f <b>Overlay evaluator backlog</b>\n"
        f"{backlog_count} decisions are unevaluated and older than 2 days "
        f"(oldest: {oldest_age_days:.1f}d, threshold: {threshold}).\n"
        f"Run: <code>python3 -m overlay.cron --evaluate</code>"
    )
    print(f"WARNING: overlay evaluator backlog={backlog_count} oldest={oldest_age_days:.1f}d")

    if dry_run:
        print(f"[DRY-RUN] Would send Telegram: {alert_text}")
        return False

    try:
        from utils.telegram import send_message  # type: ignore
        send_message(alert_text)
    except Exception as exc:
        print(f"WARNING: Telegram send failed (non-fatal): {exc}", file=sys.stderr)

    return False

if __name__ == '__main__':
    main()
