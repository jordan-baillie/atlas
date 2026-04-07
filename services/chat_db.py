"""Chat persistence layer — SQLite backed.

Separate database at data/chat.db (not the main atlas.db).
Follows the same context-manager + WAL-mode pattern as db/atlas_db.py.
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

CHAT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "chat.db"

# Module-level override for tests
_chat_db_path_override: Optional[str] = None


# ── Schema ───────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    pi_session_path TEXT,
    model       TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES chat_sessions(id),
    role        TEXT NOT NULL,   -- 'user', 'assistant', 'system'
    content     TEXT NOT NULL,
    metadata    TEXT,            -- JSON blob
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON chat_messages (session_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON chat_sessions (updated_at DESC);
"""


# ── Connection ───────────────────────────────────────────────────────────────

@contextmanager
def get_db(db_path: Optional[str] = None):
    """Context manager yielding a WAL-mode SQLite connection to chat.db.

    Commits on clean exit, rolls back on exception, always closes connection.
    """
    path = db_path if db_path is not None else (_chat_db_path_override or str(CHAT_DB_PATH))
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema init ──────────────────────────────────────────────────────────────

def init_db(db_path: Optional[str] = None) -> None:
    """Create chat_sessions and chat_messages tables (idempotent).

    When db_path is provided the module-level path override is updated so all
    subsequent CRUD functions use the same database — useful for tests.
    """
    global _chat_db_path_override
    if db_path is not None:
        _chat_db_path_override = db_path

    effective_path = _chat_db_path_override or str(CHAT_DB_PATH)
    if effective_path not in (":memory:",) and not effective_path.startswith("file:"):
        Path(effective_path).parent.mkdir(parents=True, exist_ok=True)

    with get_db() as conn:
        conn.executescript(_SCHEMA)


# ── Session CRUD ─────────────────────────────────────────────────────────────

def create_session(
    name: Optional[str] = None,
    model: str = "claude-sonnet-4-6",
    pi_session_path: Optional[str] = None,
) -> dict:
    """Create a new chat session.

    Returns a dict with keys: id, name, model, status, pi_session_path, created_at.
    """
    session_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO chat_sessions (id, name, model, pi_session_path, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'active', ?, ?)
            """,
            (session_id, name, model, pi_session_path, now, now),
        )
    return {
        "id": session_id,
        "name": name,
        "model": model,
        "pi_session_path": pi_session_path,
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }


def get_session(session_id: str) -> Optional[dict]:
    """Return a session dict by ID, or None if not found."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    return dict(row) if row else None


def list_sessions(limit: int = 20) -> list:
    """Return most-recently-updated sessions, newest first."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_sessions WHERE status = 'active' ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_session(session_id: str, **kwargs) -> None:
    """Update arbitrary session columns (e.g. name, status, pi_session_path)."""
    if not kwargs:
        return
    kwargs["updated_at"] = datetime.utcnow().isoformat()
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [session_id]
    with get_db() as conn:
        conn.execute(f"UPDATE chat_sessions SET {cols} WHERE id = ?", vals)


# ── Message CRUD ─────────────────────────────────────────────────────────────

def add_message(
    session_id: str,
    role: str,
    content: str,
    metadata: Optional[dict] = None,
) -> int:
    """Insert a message and return its auto-assigned ID.

    Also bumps the session's updated_at timestamp.
    """
    now = datetime.utcnow().isoformat()
    meta_json = json.dumps(metadata) if metadata else None
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO chat_messages (session_id, role, content, metadata, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, role, content, meta_json, now),
        )
        msg_id: int = cursor.lastrowid  # type: ignore[assignment]
        conn.execute(
            "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )
    return msg_id


def get_messages(
    session_id: str,
    limit: int = 50,
    before_id: Optional[int] = None,
) -> list:
    """Return messages for a session, newest first.

    Supports cursor-based pagination via *before_id*: pass the smallest ID
    seen by the client to fetch the next page of older messages.
    """
    with get_db() as conn:
        if before_id is not None:
            rows = conn.execute(
                """
                SELECT id, session_id, role, content, metadata, created_at
                FROM chat_messages
                WHERE session_id = ? AND id < ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, before_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, session_id, role, content, metadata, created_at
                FROM chat_messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()

    result = []
    for r in rows:
        row = dict(r)
        if row.get("metadata"):
            try:
                row["metadata"] = json.loads(row["metadata"])
            except (ValueError, TypeError):
                pass
        result.append(row)
    return result


def get_latest_session() -> Optional[dict]:
    """Return the most recently updated active session, or None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM chat_sessions WHERE status = 'active' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None
