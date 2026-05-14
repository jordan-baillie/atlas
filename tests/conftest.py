"""Shared test fixtures for Atlas test suite."""
import copy
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
# HALT file isolation — prevent ANY test from writing to production data/HALT
#
# Root cause (2026-04-29): tests calling check_daily_drawdown() without
# patching kill_switch.halt → write real /root/atlas/data/HALT → blocks
# live trading.  Same class of bug as the prod-DB leak fixed 2026-04-20.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _isolate_halt_file_session(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Session-scope: redirect kill_switch._HALT_FILE to a tmp dir from the
    very start of the run.

    Session-scope ensures this fires before module-scope fixtures that might
    call check_daily_drawdown().  The function-scope layer below gives each
    individual test a fresh HALT path so tests don't bleed halt state into
    each other.

    See lessons learned 2026-04-29 — pytest run wrote real HALT file at
    /root/atlas/data/HALT, blocking pre-market execute_approved.
    """
    try:
        import brokers.kill_switch as _ks
    except Exception:
        yield
        return

    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    session_halt_dir = tmp_path_factory.mktemp("halt_session")
    session_halt_file = session_halt_dir / "HALT"
    mp.setattr(_ks, "_HALT_FILE", session_halt_file)
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _isolate_halt_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Function-scope: each test gets a fresh HALT file location.

    Layered on top of _isolate_halt_file_session — the function-scope
    fixture wins for individual tests, giving them a per-test HALT path.
    This prevents halt state written by one test from affecting the next.
    """
    try:
        import brokers.kill_switch as _ks
    except Exception:
        yield
        return

    fn_halt_file = tmp_path / "HALT"
    monkeypatch.setattr(_ks, "_HALT_FILE", fn_halt_file)
    yield
    # monkeypatch auto-restores _HALT_FILE on fixture teardown.


@pytest.fixture(scope="session", autouse=True)
def _zz_verify_no_halt_pollution() -> None:
    """Session-end assertion: production /root/atlas/data/HALT was not touched.

    Named _zz_... so it runs AFTER all _isolate_... fixtures in lexical order,
    meaning its post-yield (teardown) runs first — verifies the production
    path while isolation is still active.

    If this fixture fails, a test leaked past the isolation layer — investigate
    kill_switch usage in the failing test and add explicit patching.
    """
    import os as _os
    halt_path = "/root/atlas/data/HALT"
    pre_exists = _os.path.exists(halt_path)
    pre_mtime = _os.path.getmtime(halt_path) if pre_exists else None

    yield

    post_exists = _os.path.exists(halt_path)
    post_mtime = _os.path.getmtime(halt_path) if post_exists else None

    if not pre_exists and post_exists:
        # Clean up so live trading isn't blocked, then fail loudly
        try:
            _os.remove(halt_path)
        except Exception:
            pass
        pytest.fail(
            f"Test session created /root/atlas/data/HALT (mtime={post_mtime}) — "
            f"kill_switch isolation broken.  A test wrote to the production HALT "
            f"path.  Check which test calls kill_switch.halt() without patching "
            f"_HALT_FILE or mocking the halt() function."
        )
    if pre_exists and post_exists and pre_mtime != post_mtime:
        pytest.fail(
            f"Test session modified /root/atlas/data/HALT "
            f"(mtime {pre_mtime} → {post_mtime}) — kill_switch isolation broken."
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

# ---------------------------------------------------------------------------
# LivePortfolio state-file isolation
# Prevents ANY test from writing to brokers/state/live_*.json (real broker state).
# Root cause 2026-04-29: pytest run emptied live_sp500.json positions (CAT lost).
# Same pattern as _isolate_halt_file — session-scope + function-scope layers.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _isolate_live_portfolio_state_session(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Session-scope: redirect brokers.live_portfolio._STATE_DIR to tmp from session start.

    Session-scope fires before module-scope fixtures, ensuring that even
    LivePortfolio instances created at module-import time (e.g. in module-level
    fixtures) write to tmp instead of production.

    See: tests/test_state_file_isolation.py for self-tests.
    Root cause: test calls to record_equity()/record_closed_trade()/save_state()
    on a LivePortfolio with broker_data_valid=True + no path redirect wrote
    positions=[] to brokers/state/live_sp500.json.
    """
    try:
        import brokers.live_portfolio as _lp
    except Exception:
        yield
        return

    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    session_state_dir = tmp_path_factory.mktemp("live_portfolio_state_session")
    mp.setattr(_lp, "_STATE_DIR", session_state_dir)
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _isolate_live_portfolio_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Function-scope: each test gets a fresh LivePortfolio state directory.

    Layered on top of _isolate_live_portfolio_state_session — the per-test
    fixture wins, giving each test an isolated empty state dir.  Tests that
    need to seed a state file create it at `tmp_path / "lp_state" / "live_{mkt}.json"`.

    Covers:
    - LivePortfolio.save_state() writes  
    - LivePortfolio.record_equity() writes (calls save_state)
    - LivePortfolio.record_closed_trade() writes (calls save_state)
    - LivePortfolio._update_state_positions() reads+writes
    - LivePortfolio._load_local_state() reads (returns empty if file not present)
    """
    try:
        import brokers.live_portfolio as _lp
    except Exception:
        yield
        return

    state_dir = tmp_path / "lp_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_lp, "_STATE_DIR", state_dir)
    yield


# ---------------------------------------------------------------------------
# scripts.reconcile_positions state-file isolation
# Prevents ANY test from triggering save_internal_state() writes to
# brokers/state/live_*.json. Same root cause class as
# brokers.live_portfolio._STATE_DIR (commit 4ea328fa).
# Discovered 2026-04-30: test_reconcile_positions_fix_idempotent wrote
# positions=[] to live_sp500.json, wiping CAT/FCX/MU.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _isolate_reconcile_positions_state_dir_session(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Session-scope: redirect scripts.reconcile_positions._STATE_DIR to tmp."""
    try:
        import scripts.reconcile_positions as _rp
    except Exception:
        yield
        return

    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    session_state_dir = tmp_path_factory.mktemp("reconcile_positions_state_session")
    mp.setattr(_rp, "_STATE_DIR", session_state_dir)
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _isolate_reconcile_positions_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Function-scope: each test gets a fresh reconcile_positions state dir."""
    try:
        import scripts.reconcile_positions as _rp
    except Exception:
        yield
        return

    state_dir = tmp_path / "rp_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_rp, "_STATE_DIR", state_dir)
    yield


@pytest.fixture(scope="session", autouse=True)
def _zz_verify_no_state_file_pollution() -> None:
    """Session-end: assert brokers/state/live_*.json files were NOT modified.

    Named _zz_ so teardown runs first (after all tests complete), verifying
    production state files are intact.  Fails loudly with mtime+size details
    if any production state file was touched.

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


# ---------------------------------------------------------------------------
# chat_db isolation
# Prevents ANY test from writing to data/chat.db (production chat database).
# services.chat_db uses CHAT_DB_PATH (Path) and _chat_db_path_override (str|None).
# Both are patched so every write path goes to a tmp file.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _isolate_chat_db_session(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Session-scope: redirect services.chat_db.CHAT_DB_PATH to tmp from session start.

    CRITICAL: session-scope fires before module-scope fixtures, ensuring that
    even chat_db connections opened during module-level setup write to tmp.

    See: tests/test_state_isolation_self.py for self-tests.
    Root cause class: module-level hardcoded paths — same as kill_switch._HALT_FILE
    and live_portfolio._STATE_DIR (commits dede8d62 / 4ea328fa).
    """
    try:
        import services.chat_db as _cdb
    except Exception:
        yield
        return

    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    session_tmp = tmp_path_factory.mktemp("chat_db_session")
    session_path = session_tmp / "chat_session.db"
    mp.setattr(_cdb, "CHAT_DB_PATH", session_path)
    mp.setattr(_cdb, "_chat_db_path_override", None)
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _isolate_chat_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Function-scope: each test gets a fresh chat.db path in its own tmp dir."""
    try:
        import services.chat_db as _cdb
    except Exception:
        yield
        return

    fn_db_path = tmp_path / "chat_test.db"
    monkeypatch.setattr(_cdb, "CHAT_DB_PATH", fn_db_path)
    monkeypatch.setattr(_cdb, "_chat_db_path_override", None)
    yield


@pytest.fixture(scope="session", autouse=True)
def _zz_verify_no_chat_db_pollution() -> None:
    """Session-end: assert data/chat.db was NOT modified during pytest.

    Named _zz_ so teardown runs after all tests complete.
    """
    import os as _os

    prod_path = "/root/atlas/data/chat.db"
    pre_mtime = _os.path.getmtime(prod_path) if _os.path.exists(prod_path) else None
    pre_size = _os.path.getsize(prod_path) if _os.path.exists(prod_path) else None

    yield

    if pre_mtime is not None and _os.path.exists(prod_path):
        cur_mtime = _os.path.getmtime(prod_path)
        cur_size = _os.path.getsize(prod_path)
        assert cur_mtime == pre_mtime, (
            f"PROD POLLUTION: {prod_path} mtime changed during pytest "
            f"({pre_mtime:.3f} → {cur_mtime:.3f}). Some test wrote to prod chat.db."
        )
        assert cur_size == pre_size, (
            f"PROD POLLUTION: {prod_path} size changed during pytest "
            f"({pre_size} → {cur_size} bytes)."
        )


# ---------------------------------------------------------------------------
# price_arbiter isolation
# Prevents ANY test from writing to data/price_arbiter_alert_throttle.json.
# brokers.price_arbiter uses module-level _THROTTLE_PATH (Path).
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _isolate_price_arbiter_session(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Session-scope: redirect brokers.price_arbiter._THROTTLE_PATH to tmp.

    See: tests/test_state_isolation_self.py for self-tests.
    """
    try:
        import brokers.price_arbiter as _pa
    except Exception:
        yield
        return

    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    session_tmp = tmp_path_factory.mktemp("price_arbiter_session")
    session_path = session_tmp / "throttle_session.json"
    mp.setattr(_pa, "_THROTTLE_PATH", session_path)
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _isolate_price_arbiter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Function-scope: each test gets a fresh throttle path in its own tmp dir."""
    try:
        import brokers.price_arbiter as _pa
    except Exception:
        yield
        return

    fn_throttle_path = tmp_path / "price_arbiter_throttle.json"
    monkeypatch.setattr(_pa, "_THROTTLE_PATH", fn_throttle_path)
    yield


@pytest.fixture(scope="session", autouse=True)
def _zz_verify_no_price_arbiter_pollution() -> None:
    """Session-end: assert data/price_arbiter_alert_throttle.json was NOT modified."""
    import os as _os

    prod_path = "/root/atlas/data/price_arbiter_alert_throttle.json"
    pre_mtime = _os.path.getmtime(prod_path) if _os.path.exists(prod_path) else None
    pre_size = _os.path.getsize(prod_path) if _os.path.exists(prod_path) else None

    yield

    if pre_mtime is not None and _os.path.exists(prod_path):
        cur_mtime = _os.path.getmtime(prod_path)
        cur_size = _os.path.getsize(prod_path)
        assert cur_mtime == pre_mtime, (
            f"PROD POLLUTION: {prod_path} mtime changed during pytest "
            f"({pre_mtime:.3f} → {cur_mtime:.3f}). Some test wrote to prod throttle."
        )
        assert cur_size == pre_size, (
            f"PROD POLLUTION: {prod_path} size changed during pytest "
            f"({pre_size} → {cur_size} bytes)."
        )


# ---------------------------------------------------------------------------
# reconcile_shadow isolation
# Prevents ANY test from writing to data/reconcile_shadow_alert_state.json.
# scripts.reconcile_shadow uses module-level _ALERT_STATE_FILE (Path).
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _isolate_reconcile_shadow_session(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Session-scope: redirect scripts.reconcile_shadow._ALERT_STATE_FILE to tmp.

    See: tests/test_state_isolation_self.py for self-tests.
    """
    try:
        import scripts.reconcile_shadow as _rs
    except Exception:
        yield
        return

    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    session_tmp = tmp_path_factory.mktemp("reconcile_shadow_session")
    session_path = session_tmp / "alert_state_session.json"
    mp.setattr(_rs, "_ALERT_STATE_FILE", session_path)
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _isolate_reconcile_shadow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Function-scope: each test gets a fresh alert state path in its own tmp dir."""
    try:
        import scripts.reconcile_shadow as _rs
    except Exception:
        yield
        return

    fn_alert_path = tmp_path / "reconcile_shadow_alert_state.json"
    monkeypatch.setattr(_rs, "_ALERT_STATE_FILE", fn_alert_path)
    yield


@pytest.fixture(scope="session", autouse=True)
def _zz_verify_no_reconcile_shadow_pollution() -> None:
    """Session-end: assert data/reconcile_shadow_alert_state.json was NOT modified."""
    import os as _os

    prod_path = "/root/atlas/data/reconcile_shadow_alert_state.json"
    pre_mtime = _os.path.getmtime(prod_path) if _os.path.exists(prod_path) else None
    pre_size = _os.path.getsize(prod_path) if _os.path.exists(prod_path) else None

    yield

    if pre_mtime is not None and _os.path.exists(prod_path):
        cur_mtime = _os.path.getmtime(prod_path)
        cur_size = _os.path.getsize(prod_path)
        assert cur_mtime == pre_mtime, (
            f"PROD POLLUTION: {prod_path} mtime changed during pytest "
            f"({pre_mtime:.3f} → {cur_mtime:.3f}). Some test wrote to prod alert state."
        )
        assert cur_size == pre_size, (
            f"PROD POLLUTION: {prod_path} size changed during pytest "
            f"({pre_size} → {cur_size} bytes)."
        )
