"""
Atlas v2.0 — Typed SQLite access layer (re-export shim).

Every module in Atlas that needs persistent state goes through here.
No raw SQL scattered across the codebase.

Design rules:
- DB_PATH points to data/atlas.db (production)
- _db_path_override can be set for testing (call init_db(path) or set directly)
- get_db() is a context manager -- every function uses ``with get_db() as db:``
- JSON columns are serialized with json.dumps / json.loads
- Timestamps are ISO format strings
- get_ohlcv() returns a pandas DataFrame with date as index

Refactor 3.1 (2026-05-14): The implementation was split into db/<domain>.py modules.
This file retains the connection layer (get_db, init_db, DB_PATH) and re-exports
all domain functions for backward compat with 238+ caller files.

Sub-modules:
- db.trades    -- trades + paper_trades CRUD
- db.regime    -- regime_history CRUD
- db.ohlcv     -- ohlcv price data CRUD
- db.signals   -- signals CRUD
- db.plans     -- plans CRUD
- db.equity    -- equity_curve CRUD
- db.snapshots -- portfolio + position snapshots CRUD
- db.overlay   -- overlay decisions, shadow log, ceasefire, news intel
- db.research  -- research_experiments + research_best CRUD
- db.system_misc -- heartbeats, system_log, telegram messages
- db.macro     -- macro_indicators + treasury_curve CRUD
- db.risk_cache -- regime transitions + ruin probability + portfolio risk cache
- db.broker_orders -- broker_orders + fill-price oracle + protective records
- db.lifecycle -- strategy_lifecycle CRUD
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "atlas.db"

# Module-level override used by tests -- set via init_db(path) or directly.
# All CRUD functions call get_db() with no args; they use whatever is current.
_db_path_override: Optional[str] = None

# Test override for the broker state file directory used by _assert_state_file_parity.
# Set to a tmp_path str in tests; defaults to None (production path).
_state_dir_override: Optional[str] = None

# WAL mode persists at the DB file level; only needs to be set once per path
# per process. Avoids redundant PRAGMA on every connection.
_wal_initialized_paths: set = set()

# Risk cache tables creation guard (also patched by tests to force re-creation).
_risk_cache_tables_ensured: bool = False


# -- Connection ----------------------------------------------------------------

@contextmanager
def get_db(db_path: Optional[str] = None):
    """Context manager that yields a WAL-mode SQLite connection.

    Priority for path: explicit arg -> _db_path_override -> DB_PATH
    Commits on clean exit, rolls back on exception.
    """
    path = db_path if db_path is not None else (_db_path_override or str(DB_PATH))
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    if path not in _wal_initialized_paths:
        conn.execute("PRAGMA journal_mode=WAL")
        _wal_initialized_paths.add(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:  # Broad catch intentional: must roll back on any DB error; re-raised immediately
        conn.rollback()
        raise
    finally:
        conn.close()


# -- Schema init ---------------------------------------------------------------

def init_db(db_path: Optional[str] = None) -> None:
    """Create all tables from schema.sql (idempotent -- uses IF NOT EXISTS).

    When db_path is provided, sets the module-level override so all subsequent
    CRUD calls use the same database.  Used by tests to point at a tmp file.
    """
    global _db_path_override
    if db_path is not None:
        _db_path_override = db_path

    effective_path = _db_path_override or str(DB_PATH)
    if effective_path not in (":memory:",) and not effective_path.startswith("file:"):
        Path(effective_path).parent.mkdir(parents=True, exist_ok=True)

    schema_path = Path(__file__).resolve().parent / "schema.sql"
    schema_sql = schema_path.read_text()

    with get_db() as conn:
        conn.executescript(schema_sql)


# -- Domain re-exports --------------------------------------------------------
# IMPORTANT: get_db / init_db / DB_PATH / overrides are defined ABOVE this
# section.  Sub-modules do `import db.atlas_db as _adb` and call _adb.get_db()
# so that test patches on db.atlas_db.get_db propagate correctly.
# The circular import is safe: each sub-module's `import db.atlas_db as _adb`
# resolves to the partial module (which already has get_db defined).

from db.trades import *       # noqa: F401,F403,E402
from db.regime import *       # noqa: F401,F403,E402
from db.ohlcv import *        # noqa: F401,F403,E402
from db.signals import *      # noqa: F401,F403,E402
from db.plans import *        # noqa: F401,F403,E402
from db.equity import *       # noqa: F401,F403,E402
from db.snapshots import *    # noqa: F401,F403,E402
from db.overlay import *      # noqa: F401,F403,E402
from db.research import *     # noqa: F401,F403,E402
from db.system_misc import *  # noqa: F401,F403,E402
from db.macro import *        # noqa: F401,F403,E402
from db.risk_cache import *   # noqa: F401,F403,E402
from db.broker_orders import *  # noqa: F401,F403,E402
from db.lifecycle import *    # noqa: F401,F403,E402

# Explicit re-exports of private helpers that tests import directly from db.atlas_db
from db.trades import _group_performance, _assert_state_file_parity, _STRATEGY_SKIP  # noqa: F401,E402
from db.plans import _validate_plan_date, _decode_plan  # noqa: F401,E402
from db.snapshots import _decode_snapshot  # noqa: F401,E402
from db.lifecycle import _VALID_LIFECYCLE_STATES  # noqa: F401,E402
from db.risk_cache import _ensure_risk_cache_tables  # noqa: F401,E402
from db.macro import _MACRO_INDICATOR_COLS, _TREASURY_CURVE_COLS  # noqa: F401,E402
