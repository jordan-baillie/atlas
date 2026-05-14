"""db/connection — re-export shim for consumers who import from db.connection.

The canonical implementation of get_db / init_db lives in db.atlas_db so that
test patches on ``db.atlas_db._db_path_override`` (and ``db.atlas_db.get_db``)
propagate correctly to all CRUD functions.  This module just re-exports the
public connection API for consumers who prefer the semantic name.
"""

from __future__ import annotations

from db.atlas_db import (  # noqa: F401
    DB_PATH,
    _db_path_override,
    _state_dir_override,
    _wal_initialized_paths,
    get_db,
    init_db,
)

__all__ = [
    "DB_PATH",
    "get_db",
    "init_db",
]
