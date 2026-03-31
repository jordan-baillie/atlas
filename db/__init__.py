"""
Atlas v2.0 — db package.

Single entry point for all database access.
No raw SQL outside of atlas_db.py.
"""

from .atlas_db import DB_PATH, get_db, init_db

__all__ = ["DB_PATH", "get_db", "init_db"]
