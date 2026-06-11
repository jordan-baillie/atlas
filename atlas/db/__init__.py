"""atlas.db — typed SQLite access layer for data/atlas.db (dashboard read side).

Every module that needs persistent state goes through here.

Design rules:
- DB_PATH points to data/atlas.db (production)
- _db_path_override can be set for testing (call init_db(path) or set directly)
- get_db() is a context manager -- every function uses ``with get_db() as db:``
- JSON columns are serialized with json.dumps / json.loads
- Timestamps are ISO format strings

This package IS the connection layer (get_db, init_db, DB_PATH) and re-exports
the domain functions so consumers and test patches share one module object.

Sub-modules:
- atlas.db.trades      -- trade reads (open positions, performance summary)
- atlas.db.regime      -- regime_history reads
- atlas.db.equity      -- equity_curve reads
- atlas.db.system_misc -- heartbeats + telegram outbound audit
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from atlas.kernel.paths import DATA_DIR

DB_PATH = DATA_DIR / "atlas.db"

# Module-level override used by tests -- set via init_db(path) or directly.
# All CRUD functions call get_db() with no args; they use whatever is current.
_db_path_override: Optional[str] = None

# WAL mode persists at the DB file level; only needs to be set once per path
# per process. Avoids redundant PRAGMA on every connection.
_wal_initialized_paths: set = set()


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
    schema_sql = schema_path.read_text(encoding="utf-8")

    with get_db() as conn:
        conn.executescript(schema_sql)


# -- Domain re-exports --------------------------------------------------------
# IMPORTANT: get_db / init_db / DB_PATH / overrides are defined ABOVE this
# section.  Sub-modules do `import atlas.db as _adb` and call _adb.get_db()
# so that test patches on atlas.db.get_db propagate correctly.
# The circular import is safe: each sub-module's `import atlas.db as _adb`
# resolves to the partial module (which already has get_db defined).

from atlas.db.trades import *       # noqa: F401,F403,E402
from atlas.db.regime import *       # noqa: F401,F403,E402
from atlas.db.equity import *       # noqa: F401,F403,E402
from atlas.db.system_misc import *  # noqa: F401,F403,E402

# Explicit re-export of a private helper that tests use directly
from atlas.db.trades import _group_performance  # noqa: F401,E402
