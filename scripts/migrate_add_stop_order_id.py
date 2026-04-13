#!/usr/bin/env python3
"""Migration: add stop_order_id column to trades table.

Safe to run multiple times — checks if column already exists.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "atlas.db"

def migrate():
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    # Check if column already exists
    cursor.execute("PRAGMA table_info(trades)")
    columns = [row[1] for row in cursor.fetchall()]
    
    added = []
    if "stop_order_id" not in columns:
        cursor.execute("ALTER TABLE trades ADD COLUMN stop_order_id TEXT DEFAULT ''")
        added.append("stop_order_id")
    
    if "tp_order_id" not in columns:
        cursor.execute("ALTER TABLE trades ADD COLUMN tp_order_id TEXT DEFAULT ''")
        added.append("tp_order_id")
    
    conn.commit()
    conn.close()
    
    if added:
        print(f"Added columns: {', '.join(added)}")
    else:
        print("Columns already exist — no changes needed")

if __name__ == "__main__":
    migrate()
