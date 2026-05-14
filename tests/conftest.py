"""Shared test fixtures for Atlas test suite."""
import copy
import importlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Ensure project root is on path
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from strategies.base import Signal  # noqa: E402

# ---------------------------------------------------------------------------
# Collection ignore — skip archived test files during collection.
# (was incorrectly in pytest.ini as collect_ignore_glob; that key is only
# valid in conftest.py, not pytest.ini).
# Path is relative to this conftest.py file, so "archive/*" means tests/archive/*.
# "_attic/**" resolves to <project_root>/_attic/** and excludes archived WIP tests.
# ---------------------------------------------------------------------------
collect_ignore_glob = ["archive/*", "../_attic/**"]

# ---------------------------------------------------------------------------
# importlib-mode compat: register this module as 'tests.conftest' in sys.modules
# ---------------------------------------------------------------------------
# pytest --import-mode=importlib imports conftest.py as 'conftest' (not as
# 'tests.conftest').  Test files that use absolute imports like
#   from tests.conftest import make_ohlcv_df
# fail collection unless we register an alias here.  This is safe: the module
# IS tests/conftest.py by definition — we're just adding a second sys.modules key.
_this_mod = sys.modules.get(__name__) or sys.modules.get('conftest')
if _this_mod is not None and 'tests.conftest' not in sys.modules:
    sys.modules['tests.conftest'] = _this_mod


# ---------------------------------------------------------------------------
# Test-log isolation — prevent pytest output from polluting prod atlas.log
# ---------------------------------------------------------------------------
import logging as _logging
import os as _os

@pytest.fixture(scope="session", autouse=True)
def _isolate_test_logs():
    """Redirect root logger file output to tests/logs/pytest.log for the session.

    Background: utils.logging_config.setup_logging() attaches a FileHandler
    pointing at logs/atlas.log. Importing modules that call setup_logging()
    during pytest causes test-time errors (mock failures, intentional bad
    inputs) to leak into the production log, where atlas-error-watchdog
    picks them up as real alerts.

    This fixture: at session start, removes any FileHandler whose baseFilename
    points at the prod atlas.log; replaces it with a FileHandler at
    tests/logs/pytest.log. At teardown, restores the original handlers.
    """
    project_root = Path(__file__).resolve().parent.parent
    prod_log = (project_root / "logs" / "atlas.log").resolve()
    test_log_dir = project_root / "tests" / "logs"
    test_log_dir.mkdir(parents=True, exist_ok=True)
    test_log = test_log_dir / "pytest.log"

    root = _logging.getLogger()
    original_handlers = list(root.handlers)
    removed = []
    for h in list(root.handlers):
        if isinstance(h, _logging.FileHandler):
            try:
                if Path(h.baseFilename).resolve() == prod_log:
                    root.removeHandler(h)
                    removed.append(h)
                    try:
                        h.close()
                    except Exception:
                        pass
            except Exception:
                pass

    test_handler = _logging.FileHandler(test_log, mode="a")
    test_handler.setFormatter(_logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(test_handler)

    # Also intercept future setup_logging() calls during the session by
    # marking the module as already-set-up. Prevents import-time re-attach.
    try:
        from utils import logging_config as _lc
        _lc._setup_done = True
    except Exception:
        pass

    yield

    # Teardown — restore original handlers
    root.removeHandler(test_handler)
    try:
        test_handler.close()
    except Exception:
        pass
    # Re-add any handlers we removed (in case parallel test discovery needs them)
    for h in removed:
        try:
            root.addHandler(h)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# DB isolation — prevent ANY test from writing to production data/atlas.db
# ---------------------------------------------------------------------------

def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers so -v output shows them cleanly."""
    config.addinivalue_line(
        "markers",
        "no_isolate_prod_db: opt out of prod DB isolation (test legitimately reads/writes real DB)",
    )


# ---------------------------------------------------------------------------
# Session-level DB isolation — runs BEFORE any module/class/function fixture.
# Catches writes from module-scoped fixtures (e.g. test_baseline_regression's
# baseline_result fixture which runs before function-scoped isolation).
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _isolate_prod_db_session(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Point atlas_db at a session-wide tmp DB from the very start of the run.

    Session-scope ensures this activates before module-scope fixtures, which
    would otherwise leak writes to production data/atlas.db before any
    function-scope fixture can intervene.
    """
    try:
        import db.atlas_db as _adb
        from db.atlas_db import init_db
    except Exception:
        yield
        return
    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    session_db = tmp_path_factory.mktemp("session_db") / "atlas_session.db"
    original = getattr(_adb, "_db_path_override", None)
    mp.setattr(_adb, "_db_path_override", str(session_db))
    try:
        init_db()
    except Exception:
        pass
    yield
    mp.setattr(_adb, "_db_path_override", original)
    mp.undo()


@pytest.fixture(autouse=True)
def _isolate_prod_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Redirect all atlas_db writes to a throw-away tmp file per test.

    Prevents production data/atlas.db contamination.  Tests that need real
    SQLite semantics still get them — just against a throw-away path.

    Tests that explicitly opt-in to the production DB (e.g. because they
    read prod data in a read-only way) can bypass via the marker:

        @pytest.mark.no_isolate_prod_db

    The marker MUST be accompanied by a comment explaining why prod access
    is needed and confirming the test does NOT write.
    """
    if "no_isolate_prod_db" in request.keywords:
        yield
        return

    try:
        import db.atlas_db as _adb
        from db.atlas_db import init_db
    except Exception:
        # Module not importable in this test environment — no isolation needed.
        yield
        return

    db_path = str(tmp_path / "isolated_atlas.db")
    monkeypatch.setattr(_adb, "_db_path_override", db_path)
    try:
        init_db()
    except Exception:
        # init_db may fail on obscure schema issues; isolation still holds
        # because _db_path_override is already set to the tmp path.
        pass
    yield
    # monkeypatch auto-restores _db_path_override on fixture teardown.


# ---------------------------------------------------------------------------
# _make_path_isolation_fixtures — factory for triple-isolation fixture sets
# Kept manual (cannot factory-ise): _isolate_test_logs (log-handler logic),
# _isolate_prod_db_* (init_db + marker opt-out), _isolate_state_dir
# (function-scope only, different attr), _zz_verify_no_state_file_pollution
# (checks 3 live_*.json files, not a single path).
# ---------------------------------------------------------------------------

def _make_path_isolation_fixtures(
    module_path: str,
    attr: str,
    session_tmp_name: str,
    tmp_filename: str,
    prod_path: str | None = None,
    *,
    label: str,
    extra_setattr: dict | None = None,
    is_dir: bool = False,
    cleanup_if_created: bool = False,
) -> tuple:
    """Generate (session_fixture, func_fixture, verify_fixture | None).

    Replaces the hand-coded (session-scope autouse, function-scope autouse,
    session-end verify) triple for one production artifact.  Assign results
    to module-level names so pytest discovers them:

        _sess, _func, _verify = _make_path_isolation_fixtures(
            "brokers.price_arbiter", "_THROTTLE_PATH",
            "pa_session", "throttle.json",
            "/root/atlas/data/price_arbiter_alert_throttle.json",
            label="price_arbiter",
        )

    prod_path=None          → verify is None (no pollution check).
    is_dir=True             → attr points at a dir; session uses mktemp() directly.
    cleanup_if_created=True → HALT-style: remove + fail if file created by tests.
    extra_setattr           → additional attrs patched alongside *attr*.
    """

    @pytest.fixture(scope="session", autouse=True)
    def _session_iso(tmp_path_factory: pytest.TempPathFactory) -> None:
        try:
            mod = importlib.import_module(module_path)
        except Exception:
            yield
            return
        from _pytest.monkeypatch import MonkeyPatch
        mp = MonkeyPatch()
        if is_dir:
            tmp = tmp_path_factory.mktemp(session_tmp_name)
        else:
            tmp = tmp_path_factory.mktemp(session_tmp_name) / tmp_filename
        mp.setattr(mod, attr, tmp)
        for k, v in (extra_setattr or {}).items():
            mp.setattr(mod, k, v)
        yield
        mp.undo()

    _session_iso.__name__ = f"_isolate_{label}_session"
    _session_iso.__qualname__ = _session_iso.__name__

    @pytest.fixture(autouse=True)
    def _func_iso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        try:
            mod = importlib.import_module(module_path)
        except Exception:
            yield
            return
        if is_dir:
            tmp = tmp_path / tmp_filename
            tmp.mkdir(parents=True, exist_ok=True)
        else:
            tmp = tmp_path / tmp_filename
        monkeypatch.setattr(mod, attr, tmp)
        for k, v in (extra_setattr or {}).items():
            monkeypatch.setattr(mod, k, v)
        yield

    _func_iso.__name__ = f"_isolate_{label}"
    _func_iso.__qualname__ = _func_iso.__name__

    if prod_path is None:
        return _session_iso, _func_iso, None

    @pytest.fixture(scope="session", autouse=True)
    def _verify_iso() -> None:
        import os as _os
        pre_exists = _os.path.exists(prod_path)
        pre_mtime = _os.path.getmtime(prod_path) if pre_exists else None
        pre_size = _os.path.getsize(prod_path) if pre_exists else None
        yield
        post_exists = _os.path.exists(prod_path)
        if cleanup_if_created and not pre_exists and post_exists:
            try:
                _os.remove(prod_path)
            except Exception:
                pass
            pytest.fail(
                f"Test session created {prod_path} — {label} isolation broken.  "
                f"A test called code that writes to {attr} without patching it."
            )
        if pre_exists and post_exists:
            cur_mtime = _os.path.getmtime(prod_path)
            cur_size = _os.path.getsize(prod_path)
            if cur_mtime != pre_mtime:
                pytest.fail(
                    f"PROD POLLUTION: {prod_path} mtime changed during pytest "
                    f"({pre_mtime:.3f} → {cur_mtime:.3f}). "
                    f"Some test wrote to production {label}."
                )
            if cur_size != pre_size:
                pytest.fail(
                    f"PROD POLLUTION: {prod_path} size changed during pytest "
                    f"({pre_size} → {cur_size} bytes)."
                )

    _verify_iso.__name__ = f"_zz_verify_no_{label}_pollution"
    _verify_iso.__qualname__ = _verify_iso.__name__

    return _session_iso, _func_iso, _verify_iso


# HALT file isolation: cleanup_if_created=True removes /root/atlas/data/HALT
# and fails loudly if a test creates it (root cause 2026-04-29).
(
    _isolate_halt_file_session,
    _isolate_halt_file,
    _zz_verify_no_halt_pollution,
) = _make_path_isolation_fixtures(
    "brokers.kill_switch", "_HALT_FILE",
    "halt_session", "HALT",
    "/root/atlas/data/HALT",
    label="halt",
    cleanup_if_created=True,
)


# ---------------------------------------------------------------------------
# Minimal config that satisfies all strategy constructors (no network calls)
# ---------------------------------------------------------------------------

MINIMAL_CONFIG: dict = {
    "version": "test-v1.0",
    "market": "sp500",
    "description": "Test configuration",
    "risk": {
        "starting_equity": 10_000.0,
        "max_risk_per_trade_pct": 0.01,
        "min_confidence": 0.65,
        "max_open_positions": 5,
        "max_sector_concentration": 2,
        "max_daily_drawdown_pct": 0.05,
        "require_stop_loss": True,
        "require_planned_exit": True,
    },
    "fees": {
        "commission_per_trade": 0,
        "commission_pct": 0.0,
        "slippage_pct": 0.0005,
        "min_position_value": 100.0,
        "flat_fee_threshold": 0,
    },
    "trading": {
        "mode": "paper",
        "broker": "alpaca",
        "live_enabled": False,
        "live_safety": {
            "max_order_value": 5000,
            "max_daily_orders": 10,
        },
    },
    "strategies": {
        "mean_reversion": {
            "enabled": True,
            "rsi_period": 14,
            "rsi_oversold": 35,
            "zscore_lookback": 30,
            "zscore_entry": -2.0,
            "atr_period": 20,
            "atr_stop_mult": 1.5,
            "profit_target_atr_mult": 2.5,
            "max_hold_days": 20,
            "sma200_filter": False,
            "ibs_max": 1.0,  # disabled
            "volume": {
                "lookback": 20,
                "min_ratio": 0.5,
                "surge_threshold": 1.5,
                "surge_boost": 0.0,
                "dry_penalty": 0.0,
            },
            "earnings_blackout": {"enabled": False},
        },
        "momentum_breakout": {
            "enabled": True,
            "lookback_days": 15,
            "atr_period": 20,
            "atr_stop_mult": 1.5,
            "max_hold_days": 15,
            "trend_ma_period": 20,
        },
        "trend_following": {
            "enabled": True,
            "fast_ma": 15,
            "slow_ma": 20,
            "pullback_pct": 0.04,
            "atr_period": 14,
            "atr_stop_mult": 2.0,
            "trailing_stop_atr_mult": 2.5,
            "max_hold_days": 15,
            "sma200_filter": False,
            "volume": {
                "lookback": 20,
                "min_ratio": 0.5,
                "boost_threshold": 1.5,
                "boost_amount": 0.1,
                "penalty_amount": 0.05,
            },
        },
        "opening_gap": {
            "enabled": True,
            "gap_threshold": -0.008,
            "ibs_confirm": 0.7,
            "rsi14_max": 35,
            "vol_surge_threshold": 1.5,
            "atr_period": 25,
            "atr_stop_mult": 1.0,
            "sma_exit_period": 7,
            "ibs_exit_threshold": 0.8,
            "max_hold_days": 10,
            "sma200_filter": False,
            "earnings_blackout": {"enabled": False},
        },
        "sector_rotation": {
            "enabled": True,
            "sector_momentum_period": 60,
            "top_sectors": 3,
            "bottom_sectors": 2,
            "rebalance_days": 20,
            "atr_period": 14,
            "atr_stop_mult": 3.0,
        },
        "short_term_mr": {
            "enabled": True,
            "rsi_period": 2,
            "rsi_oversold": 15,
            "ibs_oversold": 0.2,
            "sma_period": 5,
            "atr_period": 14,
            "atr_stop_mult": 1.5,
            "profit_target_atr_mult": 1.0,
            "max_hold_days": 5,
            "rsi_overbought_exit": 70,
            "volume": {"lookback": 20, "min_ratio": 0.5},
            "earnings_blackout": {"enabled": False},
        },
        "connors_rsi2": {
            "enabled": True,
            "rsi_period": 4,
            "rsi_entry": 40,
            "sma_trend_period": 150,
            "sma200_filter": False,
            "min_consecutive_down": 1,
            "ibs_max": 0.5,
            "ibs_filter_enabled": False,
            "volume": {"lookback": 20, "min_ratio": 0.5},
            "sma_exit_period": 5,
            "rsi_exit": 65,
            "exit_mode": "sma",
            "max_hold_days": 10,
            "atr_period": 14,
            "atr_stop_mult": 1.2,
        },
    },
    "backtest": {
        "train_window_days": 252,
        "test_window_days": 63,
        "step_days": 21,
        "min_history_days": 60,
    },
    "data": {
        "source": "yfinance",
        "history_years": 7,
        "cache_dir": "data/cache",
        "raw_dir": "data/raw",
        "processed_dir": "data/processed",
    },
    "allocation": {
        "enabled": False,  # disabled in tests for simplicity
        "mode": "soft_pool",
        "overflow_enabled": True,
        "pools": {
            "mean_reversion": {"max_positions": 2, "weight": 0.2},
            "momentum_breakout": {"max_positions": 1, "weight": 0.1},
            "trend_following": {"max_positions": 2, "weight": 0.2},
            "opening_gap": {"max_positions": 2, "weight": 0.2},
            "sector_rotation": {"max_positions": 2, "weight": 0.2},
            "short_term_mr": {"max_positions": 1, "weight": 0.05},
            "connors_rsi2": {"max_positions": 1, "weight": 0.05},
            "_other": {"max_positions": 1},
        },
    },
    "universe": {
        "method": "top_liquid",
        "top_n": 100,
        "min_median_daily_value": 5_000_000,
        "min_price": 5.0,
        "min_market_cap": 2_000_000_000,
        "exclusions": [],
        "benchmark_ticker": "SPY",
    },
}


# ---------------------------------------------------------------------------
# Helper: build a synthetic OHLCV DataFrame
# ---------------------------------------------------------------------------

def make_ohlcv_df(
    ticker: str = "TEST",
    n_days: int = 252,
    base_price: float = 100.0,
    seed: int = 42,
    trend: float = 0.0005,
    daily_vol: float = 0.015,
    *,
    closes: "np.ndarray | None" = None,
    volumes: "np.ndarray | float | int | None" = None,
    flat_price: float | None = None,
    high_mult: float = 1.005,
    low_mult: float = 0.995,
    dates: "pd.DatetimeIndex | list[str] | None" = None,
    end_date: str = "2024-12-31",
) -> pd.DataFrame:
    """Create a synthetic OHLCV DataFrame with DatetimeIndex.

    Default path (no keyword args): lognormal walk with drift *trend* and daily
    vol *daily_vol*.  All existing callers are unaffected — defaults unchanged.

    Optional keyword-only overrides
    --------------------------------
    closes      : pre-built close series (array-like); skips lognormal generation.
    volumes     : scalar → broadcast to all bars; array → used directly; None → RNG.
    flat_price  : constant open/close (mutually exclusive with *closes*).
    high_mult   : high = close * high_mult  (closes / flat_price paths only).
    low_mult    : low  = close * low_mult   (closes / flat_price paths only).
    dates       : explicit DatetimeIndex or list[str]; length overrides n_days.
    end_date    : trailing date for auto-generated index (default "2024-12-31").

    OHLCV invariant always enforced:
        low <= min(open, close) <= max(open, close) <= high
    """
    if closes is not None and flat_price is not None:
        raise ValueError("'closes' and 'flat_price' are mutually exclusive")

    # ── Determine date index ────────────────────────────────────────────────
    if dates is not None:
        idx = pd.DatetimeIndex(dates) if not isinstance(dates, pd.DatetimeIndex) else dates
        n = len(idx)
    elif closes is not None:
        n = len(np.asarray(closes))
        idx = pd.date_range(end=end_date, periods=n, freq="B")
    else:
        n = n_days
        idx = pd.date_range(end=end_date, periods=n, freq="B")

    # ── RNG (lognormal path + fallback random volumes) ──────────────────────
    rng = np.random.default_rng(seed)

    # ── Build OHLCV arrays ──────────────────────────────────────────────────
    if flat_price is not None:
        open_ = np.full(n, float(flat_price))
        close = np.full(n, float(flat_price))
        raw_high = np.full(n, float(flat_price) * high_mult)
        raw_low = np.full(n, float(flat_price) * low_mult)
    elif closes is not None:
        close = np.asarray(closes, dtype=float)
        open_ = close * 0.999
        raw_high = close * high_mult
        raw_low = close * low_mult
    else:
        # Lognormal path (original behaviour — unchanged)
        returns = rng.normal(trend, daily_vol, n)
        close = base_price * np.exp(np.cumsum(returns))
        open_ = close * np.exp(rng.normal(0, 0.004, n))
        raw_high = np.maximum(open_, close) * np.exp(rng.uniform(0, 0.008, n))
        raw_low = np.minimum(open_, close) * np.exp(-rng.uniform(0, 0.008, n))

    # ── Enforce OHLCV invariants ────────────────────────────────────────────
    high = np.maximum(raw_high, np.maximum(open_, close))
    low = np.minimum(raw_low, np.minimum(open_, close))

    # ── Volumes ─────────────────────────────────────────────────────────────
    if volumes is None:
        vol = rng.integers(1_000_000, 5_000_000, n).astype(float)
    elif np.ndim(volumes) == 0:
        vol = np.full(n, float(volumes))
    else:
        vol = np.asarray(volumes, dtype=float)

    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol, "ticker": ticker},
        index=idx,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_config() -> dict:
    """Return a deep copy of MINIMAL_CONFIG (mutate freely in tests)."""
    return copy.deepcopy(MINIMAL_CONFIG)


@pytest.fixture
def mock_ohlcv_data():
    """Factory fixture: call with (n_tickers, n_days) to get OHLCV dict."""
    def _factory(n_tickers: int = 5, n_days: int = 252) -> dict[str, pd.DataFrame]:
        tickers = [f"TICK{i}" for i in range(n_tickers)]
        return {
            t: make_ohlcv_df(t, n_days=n_days, base_price=50 + 30 * i, seed=i * 7)
            for i, t in enumerate(tickers)
        }
    return _factory


@pytest.fixture
def mock_positions():
    """Factory fixture: call with (n) to get list of position dicts."""
    def _factory(n: int = 3) -> list[dict]:
        pool = ["AAPL", "MSFT", "GOOG", "META", "AMZN"]
        positions = []
        for i in range(n):
            ticker = pool[i % len(pool)]
            entry_price = 100.0 + i * 10
            positions.append(
                {
                    "ticker": ticker,
                    "strategy": "mean_reversion",
                    "direction": "long",
                    "entry_date": (datetime.now() - timedelta(days=i + 1)).strftime("%Y-%m-%d"),
                    "fill_price": entry_price,
                    "entry_price": entry_price,
                    "shares": 10,
                    "position_value": entry_price * 10,
                    "stop_price": entry_price * 0.95,
                    "confidence": 0.75,
                    "features": {"rsi": 28.0, "zscore": -2.3},
                    "sector": "Technology",
                }
            )
        return positions
    return _factory


@pytest.fixture
def mock_signal() -> Signal:
    """Return a valid Signal object."""
    return Signal(
        ticker="AAPL",
        strategy="mean_reversion",
        direction="long",
        entry_price=150.0,
        stop_price=145.0,
        take_profit=165.0,
        position_size=10,
        position_value=1500.0,
        risk_amount=50.0,
        confidence=0.75,
        rationale="RSI oversold test signal",
        features={"rsi": 28.0, "zscore": -2.5},
    )


# ---------------------------------------------------------------------------
# State-dir isolation — prevent ANY test from writing to brokers/state/live_*.json
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect atlas_db._state_dir_override to a per-test tmp dir.

    Background: db.atlas_db._assert_state_file_parity() appends missing tickers
    to brokers/state/live_{universe}.json.  Any test that calls record_trade_entry()
    with an existing state file will inject fake tickers into the live state.
    This fixture short-circuits the write to a throw-away tmp directory.

    Only test_state_file_sqlite_parity.py should opt-out (it explicitly needs
    to test the parity mechanism and manages its own override).

    Kept manual: function-scope only (no session layer or verify needed);
    uses a different attr name (_state_dir_override, not a Path constant).
    """
    try:
        import db.atlas_db as _adb
    except Exception:
        yield
        return

    state_tmp = tmp_path / "broker_state"
    state_tmp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_adb, "_state_dir_override", str(state_tmp))
    yield


# LivePortfolio + reconcile_positions state-file isolation.
# Verify is the manual _zz_verify_no_state_file_pollution (3 files, can't use factory).
(
    _isolate_live_portfolio_state_session,
    _isolate_live_portfolio_state,
    _lp_noop_verify,  # None — _zz_verify_no_state_file_pollution covers this
) = _make_path_isolation_fixtures(
    "brokers.live_portfolio", "_STATE_DIR",
    "live_portfolio_state_session", "lp_state",
    label="live_portfolio_state",
    is_dir=True,
)

(
    _isolate_reconcile_positions_state_dir_session,
    _isolate_reconcile_positions_state_dir,
    _rp_noop_verify,  # None — no single prod path verify needed for this dir
) = _make_path_isolation_fixtures(
    "scripts.reconcile_positions", "_STATE_DIR",
    "reconcile_positions_state_session", "rp_state",
    label="reconcile_positions_state_dir",
    is_dir=True,
)


@pytest.fixture(scope="session", autouse=True)
def _zz_verify_no_state_file_pollution() -> None:
    """Session-end: assert brokers/state/live_*.json files were NOT modified.

    Named _zz_ so teardown runs first (after all tests complete), verifying
    production state files are intact.  Fails loudly with mtime+size details
    if any production state file was touched.

    Kept manual: checks 3 specific live_*.json files simultaneously — cannot
    be expressed as a single prod_path in the factory.

    See: tests/test_state_file_isolation.py for self-tests.
    """
    import os as _os

    state_files = [
        "/root/atlas/brokers/state/live_sp500.json",
        "/root/atlas/brokers/state/live_commodity_etfs.json",
        "/root/atlas/brokers/state/live_sector_etfs.json",
    ]
    pre_state: dict[str, tuple[float, int]] = {}
    for f in state_files:
        if _os.path.exists(f):
            pre_state[f] = (_os.path.getmtime(f), _os.path.getsize(f))

    yield

    leaks: list[str] = []
    for f, (pre_mtime, pre_size) in pre_state.items():
        if _os.path.exists(f):
            cur_mtime = _os.path.getmtime(f)
            cur_size = _os.path.getsize(f)
            if cur_mtime != pre_mtime:
                leaks.append(
                    f"{f}: mtime changed {pre_mtime:.3f} → {cur_mtime:.3f}"
                )
            elif cur_size != pre_size:
                leaks.append(
                    f"{f}: size changed {pre_size} → {cur_size} bytes"
                )

    if leaks:
        pytest.fail(
            "Production state file pollution detected — a test wrote to "
            "brokers/state/live_*.json:\n" + "\n".join(leaks)
        )


# chat_db / price_arbiter / reconcile_shadow — pure factory fits.
# extra_setattr for chat_db patches _chat_db_path_override = None alongside CHAT_DB_PATH.
(
    _isolate_chat_db_session,
    _isolate_chat_db,
    _zz_verify_no_chat_db_pollution,
) = _make_path_isolation_fixtures(
    "services.chat_db", "CHAT_DB_PATH",
    "chat_db_session", "chat.db",
    "/root/atlas/data/chat.db",
    label="chat_db",
    extra_setattr={"_chat_db_path_override": None},
)

(
    _isolate_price_arbiter_session,
    _isolate_price_arbiter,
    _zz_verify_no_price_arbiter_pollution,
) = _make_path_isolation_fixtures(
    "brokers.price_arbiter", "_THROTTLE_PATH",
    "price_arbiter_session", "price_arbiter_throttle.json",
    "/root/atlas/data/price_arbiter_alert_throttle.json",
    label="price_arbiter",
)

(
    _isolate_reconcile_shadow_session,
    _isolate_reconcile_shadow,
    _zz_verify_no_reconcile_shadow_pollution,
) = _make_path_isolation_fixtures(
    "scripts.reconcile_shadow", "_ALERT_STATE_FILE",
    "reconcile_shadow_session", "reconcile_shadow_alert_state.json",
    "/root/atlas/data/reconcile_shadow_alert_state.json",
    label="reconcile_shadow",
)
