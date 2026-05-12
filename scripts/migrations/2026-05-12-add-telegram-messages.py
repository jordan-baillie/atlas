#!/usr/bin/env python3
"""Add telegram_messages table for bidirectional message capture.

Idempotent — uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
Safe to re-run.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from db.atlas_db import get_db  # noqa: E402


SQL = """
CREATE TABLE IF NOT EXISTS telegram_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    direction    TEXT NOT NULL CHECK (direction IN ('outbound', 'inbound')),
    chat_id      TEXT NOT NULL,
    message_id   INTEGER,
    user_id      TEXT,
    username     TEXT,
    body         TEXT NOT NULL,
    parse_mode   TEXT,
    sent_at      TEXT NOT NULL,
    api_status   INTEGER,
    api_error    TEXT,
    is_command   INTEGER DEFAULT 0,
    command_name TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tgm_chat_time ON telegram_messages(chat_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_tgm_direction_time ON telegram_messages(direction, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_tgm_command ON telegram_messages(command_name) WHERE command_name IS NOT NULL;
"""


def main() -> int:
    with get_db() as db:
        db.executescript(SQL)
        # Verify
        cur = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='telegram_messages'")
        row = cur.fetchone()
        if not row:
            print("ERROR: telegram_messages table not present after migration", file=sys.stderr)
            return 1
        # Count rows (zero on first run)
        n = db.execute("SELECT COUNT(*) FROM telegram_messages").fetchone()[0]
        print(f"OK: telegram_messages table present, {n} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
