"""Shared fixtures for the Atlas test suite.

Restores the isolation layer that was lost in the 2026-06 'old Atlas is no
more' refactor (the previous conftest imported deleted modules and went down
with them). Three production artifacts are isolated:

1. logs/atlas.log        — test noise must not feed real alerting
2. data/atlas.db         — every test gets a throw-away SQLite file
3. price-arbiter throttle file
"""
from __future__ import annotations

import logging as _logging
import sys
from pathlib import Path

import pytest

# Ensure repo root is on sys.path (tests run from anywhere)
PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "no_isolate_prod_db: opt out of prod DB isolation (test legitimately reads/writes real DB)",
    )
    config.addinivalue_line("markers", "integration: marks integration tests")


# ---------------------------------------------------------------------------
# Log isolation — pytest output must not pollute prod logs/atlas.log
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _isolate_test_logs(tmp_path_factory: pytest.TempPathFactory):
    """Redirect any atlas.log FileHandler to a session tmp log file."""
    prod_log = (PROJECT / "logs" / "atlas.log").resolve()
    test_log = tmp_path_factory.mktemp("logs") / "pytest.log"

    root = _logging.getLogger()
    removed = []
    for h in list(root.handlers):
        if isinstance(h, _logging.FileHandler):
            try:
                if Path(h.baseFilename).resolve() == prod_log:
                    root.removeHandler(h)
                    removed.append(h)
                    h.close()
            except Exception:
                pass

    test_handler = _logging.FileHandler(test_log, mode="a")
    test_handler.setFormatter(_logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(test_handler)

    # Prevent setup_logging() calls during tests from re-attaching the prod handler.
    try:
        from atlas.kernel import logging_config as _lc
        _lc._setup_done = True
    except Exception:
        pass

    yield

    root.removeHandler(test_handler)
    try:
        test_handler.close()
    except Exception:
        pass
    for h in removed:
        root.addHandler(h)


# ---------------------------------------------------------------------------
# DB isolation — no test may touch production data/atlas.db
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _isolate_prod_db_session(tmp_path_factory: pytest.TempPathFactory):
    """Point atlas.db at a session-wide tmp DB before any module fixture runs."""
    try:
        import atlas.db as _adb
        from atlas.db import init_db
    except Exception:
        yield
        return
    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    session_db = tmp_path_factory.mktemp("session_db") / "atlas_session.db"
    mp.setattr(_adb, "_db_path_override", str(session_db))
    try:
        init_db()
    except Exception:
        pass
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _isolate_prod_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    """Per-test throw-away DB (real SQLite semantics, disposable path).

    Opt out with @pytest.mark.no_isolate_prod_db (read-only prod access only).
    """
    if "no_isolate_prod_db" in request.keywords:
        yield
        return
    try:
        import atlas.db as _adb
        from atlas.db import init_db
    except Exception:
        yield
        return
    db_path = str(tmp_path / "isolated_atlas.db")
    monkeypatch.setattr(_adb, "_db_path_override", db_path)
    try:
        init_db()
    except Exception:
        # Isolation still holds: the override already points at the tmp path.
        pass
    yield


# ---------------------------------------------------------------------------
# Price-arbiter throttle isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_price_arbiter_throttle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    try:
        from atlas.brokers import price_arbiter as _pa
    except Exception:
        yield
        return
    monkeypatch.setattr(_pa, "_THROTTLE_PATH", tmp_path / "throttle.json")
    yield


# ---------------------------------------------------------------------------
# Live-data isolation — execution tests must never touch real data/live/
# (data/live/<name>/ is shared runtime state with Crucible on the VPS)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_live_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    try:
        import atlas.execution.daily as _daily
        import atlas.execution.providers as _prov
        import atlas.execution.record_returns as _rr
        import atlas.execution.virtual_book as _vb
        import atlas.execution.registry as _reg
    except Exception:
        yield
        return
    live_tmp = tmp_path / "live"
    live_tmp.mkdir(exist_ok=True)
    for mod in (_daily, _prov, _rr, _vb):
        monkeypatch.setattr(mod, "LIVE_DATA", live_tmp)
    monkeypatch.setattr(_reg, "REGISTRY_PATH", tmp_path / "live_strategies.json")
    # provider registrations must not leak between tests
    monkeypatch.setattr(_reg, "PROVIDERS", dict(_reg.PROVIDERS))
    yield


# ---------------------------------------------------------------------------
# Kill-switch halt-file guard — tests must not leave a real data/HALT behind
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _zz_verify_no_halt_file_pollution():
    halt_files = [PROJECT / "data" / "HALT", PROJECT / "data" / "AUTO_REMEDIATION_HALT"]
    pre = {f: f.exists() for f in halt_files}
    yield
    leaked = [str(f) for f, existed in pre.items() if not existed and f.exists()]
    for f in leaked:
        Path(f).unlink()
    if leaked:
        pytest.fail(f"Test run created real halt file(s): {leaked} — removed; fix the offending test.")
