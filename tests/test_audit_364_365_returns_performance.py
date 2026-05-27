"""Regression tests for Atlas tasks #364 + #365 \u2014 returns/performance audit fixes.

#364 \u2014 accounting source-of-truth failures
  T1  brokers.live_portfolio._atlas_slice exposes (positions_value, cash) with
      eq == positions_value + cash for the Atlas slice.
  T2  portfolio_summary returns slice-consistent cash and positions_value.
  T3  record_equity writes equity_curve rows with positions_value derived from
      the Atlas slice (no more impossible negative pv from `eq - broker_cash`).
  T4  scripts/audit_per_market_equity treats retired (non-active) markets as
      INFO only and hard-fails only on active-market reconciliation.

#365 \u2014 CLI regressions
  T5  brokers.execution_analytics.OrderStatus is importable (NameError fix).
  T6  scripts.cli.cmd_ledger reads from the SQLite trades table, not the
      retired JSON ledger file.
  T7  pi-package atlas-jobs buildCliInvocation does NOT forward --days to
      the `backtest` subcommand (CLI argparse has no --days for backtest).
  T7c Runtime probe: loading the atlas-jobs factory the same way the Pi
      jiti loader does and dry-running cli_backtest with days=252 produces
      a command without --days. Catches regressions the source-grep T7
      cannot (e.g. someone reorders consumeArg vs allowlist check).

All tests are local & deterministic \u2014 no broker connection, no live mutation.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ──────────────────────────────────────────────────────────────────────────────
# Helper: build a minimal in-memory LivePortfolio-like object
# ──────────────────────────────────────────────────────────────────────────────

def _make_position(ticker: str, shares: int, entry_price: float,
                   strategy: str = "momentum_breakout") -> SimpleNamespace:
    pos = SimpleNamespace()
    pos.ticker = ticker
    pos.shares = shares
    pos.entry_price = entry_price
    pos.strategy = strategy
    pos.entry_value = shares * entry_price
    pos.entry_date = "2026-05-01"
    pos.stop_price = entry_price * 0.97
    pos.take_profit = None
    pos.sector = "Tech"
    pos.mae = 0.01
    pos.mfe = 0.02
    pos.current_value = lambda px: pos.shares * px
    pos.unrealized_pnl = lambda px: (px - pos.entry_price) * pos.shares
    pos.unrealized_pnl_pct = lambda px: ((px - pos.entry_price) / pos.entry_price * 100
                                          if pos.entry_price else 0.0)
    pos.holding_days = lambda _d: 5
    return pos


@pytest.fixture
def portfolio():
    """Real LivePortfolio with side effects disabled.

    We don't call _load_local_state / save_state / broker connect; we just set
    the in-memory attributes used by _atlas_slice + record_equity.
    """
    from brokers.live_portfolio import LivePortfolio

    lp = LivePortfolio.__new__(LivePortfolio)  # bypass __init__
    lp.market_id = "sp500"
    lp.starting_equity = 971.0
    lp.positions = [
        _make_position("DXCM", 7, 71.44, "momentum_breakout"),
        _make_position("F",    32, 14.88, "momentum_breakout"),
    ]
    lp.closed_trades = [{"pnl": 25.85}, {"pnl": -10.49}, {"pnl": -5.44}]
    lp.closed_trades_quarantine = []
    lp.equity_history = []
    lp.cash = 4165.65           # FULL broker cash \u2014 must not leak into slice
    lp._broker_equity = 5164.33  # FULL broker equity
    lp.daily_high_water = 971.0
    lp.daily_high_water_date = None
    lp.halted = False
    lp.halt_reason = ""
    lp.commission_flat = 1.0
    lp.commission_pct = 0.0001
    lp.broker_data_valid = True
    lp._save_state_warned = False
    return lp


# ──────────────────────────────────────────────────────────────────────────────
# T1: _atlas_slice invariants
# ──────────────────────────────────────────────────────────────────────────────

def test_atlas_slice_consistency(portfolio):
    """T1 \u2014 atlas_pos_value + atlas_cash equals equity() for the slice."""
    prices = {"DXCM": 72.12, "F": 14.95}
    pos_value, cash = portfolio._atlas_slice(prices)
    eq = portfolio.equity(prices)

    assert pos_value > 0, f"positions_value must be > 0 with open positions, got {pos_value}"
    # Atlas-slice cash = starting (971) - entry_cost (500.08 + 476.16) + realized (10)
    entry_cost = 7 * 71.44 + 32 * 14.88  # 500.08 + 476.16 = 976.24
    realized = 25.85 - 10.49 - 5.44       # 9.92
    expected_cash = 971.0 - entry_cost + realized
    assert cash == pytest.approx(expected_cash, abs=0.01), (
        f"atlas_cash should be {expected_cash:.2f}, got {cash:.2f}"
    )
    assert eq == pytest.approx(pos_value + cash, abs=0.01), (
        f"equity ({eq}) must equal pos_value ({pos_value}) + cash ({cash})"
    )


# ──────────────────────────────────────────────────────────────────────────────
# T2: portfolio_summary slice-consistency
# ──────────────────────────────────────────────────────────────────────────────

def test_portfolio_summary_slice_consistent(portfolio):
    """T2 \u2014 summary[equity] == summary[cash] + summary[positions_value]."""
    prices = {"DXCM": 72.12, "F": 14.95}
    summary = portfolio.portfolio_summary(prices)

    assert summary["positions_value"] > 0
    assert summary["equity"] == pytest.approx(
        summary["cash"] + summary["positions_value"], abs=0.02
    ), (
        f"slice mismatch: equity={summary['equity']} "
        f"cash={summary['cash']} positions_value={summary['positions_value']}"
    )
    # Broker-level values still surfaced for callers that need them
    assert summary["broker_cash"] == 4165.65
    assert summary["broker_equity"] == 5164.33
    # And critically: cash is NOT the broker cash (that was the bug)
    assert summary["cash"] != summary["broker_cash"]


# ──────────────────────────────────────────────────────────────────────────────
# T3: record_equity writes consistent positions_value (no impossible negatives)
# ──────────────────────────────────────────────────────────────────────────────

def test_record_equity_no_negative_positions_value(portfolio, tmp_path, monkeypatch):
    """T3 \u2014 record_equity in-memory entry has eq == cash + positions_value."""
    # Stub save_state so we don't write to brokers/state/* during the test.
    portfolio.save_state = lambda: None
    # equity() is normally called with no prices override; force prices into
    # the position object so update_excursions style calls don't matter.
    portfolio.positions[0].entry_price = 71.44
    portfolio.positions[1].entry_price = 14.88
    # Drive record_equity with explicit current prices.
    portfolio.record_equity("2026-05-27", {"DXCM": 72.12, "F": 14.95})

    entry = portfolio.equity_history[-1]
    assert entry["date"] == "2026-05-27"
    assert entry["positions_value"] > 0, (
        f"impossible negative positions_value regression: {entry['positions_value']}"
    )
    assert entry["equity"] == pytest.approx(
        entry["cash"] + entry["positions_value"], abs=0.02
    )
    # And critically, the recorded cash must NOT be the broker cash (the bug)
    assert entry["cash"] != portfolio.cash, (
        "record_equity must record the Atlas slice cash, not portfolio.cash "
        "(full broker cash)"
    )


# ──────────────────────────────────────────────────────────────────────────────
# T4: audit_per_market_equity active-vs-retired separation
# ──────────────────────────────────────────────────────────────────────────────

def test_audit_filters_retired_markets(monkeypatch, tmp_path):
    """T4 \u2014 retired-market snapshots must NOT cause hard reconciliation failure.

    Builds an isolated DB where only sp500 is active but stale rows exist for
    sector_etfs / commodity_etfs. With the fix, the audit reports them as INFO
    and exits 0 because sp500 alone reconciles to broker_equity.
    """
    db_path = tmp_path / "atlas.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE market_equity_history (
            date TEXT NOT NULL,
            market_id TEXT NOT NULL,
            allocated_equity REAL,
            position_mv REAL,
            cash_attributed REAL,
            broker_equity REAL,
            broker_cash REAL,
            snapshot_time TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (date, market_id)
        )
    """)
    from datetime import date as _date
    today = _date.today().isoformat()
    rows = [
        # Active market: sp500 fully reconciles to broker_equity 5159.04
        (today, "sp500",          5159.04, 994.31, 4164.73, 5159.04, 4164.73),
        # Retired markets: sum to 3701 above broker_equity, would have hard-failed pre-fix
        (today, "sector_etfs",    2616.50,   0.00, 2616.50, 5159.04, 4164.73),
        (today, "commodity_etfs", 1085.31,   0.00, 1085.31, 5159.04, 4164.73),
    ]
    for r in rows:
        conn.execute(
            "INSERT INTO market_equity_history VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
            (*r, f"{today}T22:00:00+00:00"),
        )
    conn.commit()
    conn.close()

    # Active configs: only sp500
    config_dir = tmp_path / "config_active"
    config_dir.mkdir()
    (config_dir / "sp500.json").write_text(json.dumps({
        "market_id": "sp500",
        "trading": {"live_enabled": True},
        "risk": {"starting_equity": 971},
    }))
    (config_dir / "sector_etfs.json.bak").write_text("{}")  # ignored

    # Patch the audit module's constants to point at our temp data
    import importlib
    audit_mod = importlib.import_module("scripts.audit_per_market_equity")
    monkeypatch.setattr(audit_mod, "_CONFIG_DIR", config_dir)
    # _STATE_DIR can stay default; HWM check tolerates missing state files for inactive.
    monkeypatch.setattr("db.atlas_db._db_path_override", str(db_path))

    ok, report = audit_mod.check_snapshot_reconciliation()
    assert ok, f"audit should pass when only active markets are reconciled. Report:\n{report}"
    assert "retired" in report.lower(), (
        "retired-market rows must be surfaced INFO-only in the report"
    )


# ──────────────────────────────────────────────────────────────────────────────
# T5: OrderStatus import in execution_analytics
# ──────────────────────────────────────────────────────────────────────────────

def test_execution_analytics_imports_orderstatus():
    """T5 \u2014 execution_analytics must expose OrderStatus or it crashes
    cmd_history with NameError when a failed order is present.
    """
    from brokers import execution_analytics
    # The fix is that OrderStatus is in scope inside the module.
    assert hasattr(execution_analytics, "OrderStatus"), (
        "execution_analytics.py must import OrderStatus so get_execution_history "
        "does not raise NameError on failed orders (regression in #365)."
    )
    # Sanity: it's the right enum
    from brokers.base import OrderStatus as _OS
    assert execution_analytics.OrderStatus is _OS


# ──────────────────────────────────────────────────────────────────────────────
# T6: cmd_ledger uses SQLite trades table (not legacy JSON)
# ──────────────────────────────────────────────────────────────────────────────

def test_cmd_ledger_reads_sqlite(capsys, monkeypatch, tmp_path):
    """T6 \u2014 cmd_ledger must report closed trades from db.trades, not the
    retired journal/trade_ledger.json file (which is no longer written).
    """
    # Build a fresh isolated DB with a single closed trade for sp500.
    db_path = tmp_path / "atlas.db"
    import db.atlas_db as _adb
    monkeypatch.setattr(_adb, "_db_path_override", str(db_path))
    # Force the DB to (re)initialise schema in the isolated path.
    _adb._db_instance = None  # type: ignore[attr-defined]
    with _adb.get_db() as conn:
        # Ensure the schema is loaded
        schema_sql = (PROJECT_ROOT / "db" / "schema.sql").read_text()
        conn.executescript(schema_sql)
        conn.execute(
            "INSERT INTO trades "
            "(ticker, strategy, universe, direction, entry_date, entry_price, "
            "shares, stop_price, exit_date, exit_price, exit_reason, pnl, "
            "pnl_pct, hold_days, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("CAT", "momentum_breakout", "sp500", "long",
             "2026-05-20", 100.0, 5, 95.0,
             "2026-05-23", 105.0, "take_profit", 25.0, 5.0, 3, "closed"),
        )
        conn.commit()

    from scripts import cli as cli_mod
    args = SimpleNamespace(market="sp500", days=30)
    cli_mod.cmd_ledger(args)
    captured = capsys.readouterr().out

    assert "Total trades:   1" in captured, (
        f"cmd_ledger should show 1 SQLite-backed closed trade. Got:\n{captured}"
    )
    assert "CAT" in captured, "expected ticker CAT in ledger output"


# ──────────────────────────────────────────────────────────────────────────────
# T7: cli_backtest does NOT receive --days from atlas-jobs catalog
# ──────────────────────────────────────────────────────────────────────────────

def test_atlas_jobs_backtest_strips_days():
    """T7 \u2014 the atlas-jobs TypeScript catalog must not forward --days to
    the `backtest` subcommand, since scripts/cli.py's backtest subparser has
    no --days option.  Regression test reads the source and asserts the
    guard logic exists.
    """
    src = PROJECT_ROOT / "pi-package" / "atlas-ops" / "extensions" / "atlas-jobs" / "src" / "index.ts"
    text = src.read_text()
    assert "SUBCOMMANDS_ACCEPTING_DAYS" in text, (
        "buildCliInvocation must use a SUBCOMMANDS_ACCEPTING_DAYS allowlist "
        "to avoid emitting --days for subcommands that don't support it "
        "(e.g. backtest)."
    )
    # Must NOT contain "backtest" in the SUBCOMMANDS_ACCEPTING_DAYS set
    import re
    m = re.search(
        r"SUBCOMMANDS_ACCEPTING_DAYS\s*=\s*new\s+Set\(\s*\[([^\]]*)\]",
        text,
    )
    assert m, "could not locate SUBCOMMANDS_ACCEPTING_DAYS Set literal"
    allowed = m.group(1)
    assert "backtest" not in allowed, (
        f"`backtest` must NOT be in SUBCOMMANDS_ACCEPTING_DAYS allowlist; "
        f"current allowlist body: {allowed}"
    )


def test_cli_backtest_subcommand_has_no_days_argument():
    """T7b \u2014 belt-and-braces: scripts/cli.py's backtest subparser truly
    rejects --days (which is what makes T7 necessary).
    """
    import subprocess
    result = subprocess.run(
        ["python3", "scripts/cli.py", "-m", "sp500", "backtest", "--days", "252"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0, (
        "scripts/cli.py backtest --days 252 must fail \u2014 if this passes, "
        "either the CLI grew a --days option (update SUBCOMMANDS_ACCEPTING_DAYS) "
        "or the test is misconfigured."
    )
    assert "unrecognized" in (result.stderr + result.stdout).lower() or "error" in result.stderr.lower(), (
        f"expected argparse rejection; stderr: {result.stderr[:300]}"
    )


def test_atlas_jobs_runtime_strips_days_e2e():
    """T7c \u2014 runtime-level regression.

    Loads the default-exported factory from
    `pi-package/atlas-ops/extensions/atlas-jobs/src/index.ts` via `npx tsx`
    (mirroring the jiti-based path Pi's extension loader uses) and asserts
    that `atlas_jobs_run` dry-running cli_backtest with days=252 produces
    `python3 scripts/cli.py -m sp500 backtest` (no --days).

    Skipped when the local dev environment cannot resolve typebox or tsx
    (CI may not install peer deps). The source-grep T7 test still covers
    those paths.
    """
    import shutil
    import subprocess

    if shutil.which("npx") is None:
        pytest.skip("npx not available; runtime probe requires tsx + typebox")

    atlas_ops = PROJECT_ROOT / "pi-package" / "atlas-ops"
    verify_script = atlas_ops / "extensions" / "atlas-jobs" / "tests" / "verify.ts"
    typebox_paths = [
        atlas_ops / "node_modules" / "@sinclair" / "typebox",
        Path("/root/pi-mono/node_modules/typebox"),
    ]
    if not any(p.exists() for p in typebox_paths):
        pytest.skip(
            "@sinclair/typebox not resolvable for tsx; install or symlink "
            "pi-package/atlas-ops/node_modules/@sinclair/typebox -> typebox"
        )
    assert verify_script.exists(), f"missing verify script: {verify_script}"

    result = subprocess.run(
        ["npx", "-y", "tsx", str(verify_script)],
        cwd=str(atlas_ops),
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = (result.stdout + "\n" + result.stderr)
    assert result.returncode == 0, (
        "atlas-jobs verify script must pass.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "python3 scripts/cli.py -m sp500 backtest" in combined
    assert "--days" not in combined.split("cli_backtest \u2192")[-1].splitlines()[0], (
        f"cli_backtest line still contains --days: {combined}"
    )
