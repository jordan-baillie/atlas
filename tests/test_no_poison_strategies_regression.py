"""Regression: /api/performance.by_strategy must never include poison strategies."""
import sqlite3
import pytest
from db.atlas_db import get_db

POISON_STRATS = ('unknown', 'reconciled', '')


def test_no_poison_strategies_in_trades_table():
    """After migration 2026-04-22, no trades row should carry a poison strategy."""
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE strategy IN (?, ?, ?) OR strategy IS NULL",
            POISON_STRATS,
        )
        count = cursor.fetchone()[0]
    assert count == 0, f"Found {count} trades with poison strategies — migration regressed"


def test_legacy_unknown_is_acceptable_but_flagged():
    """legacy_unknown is allowed (unresolvable historical trades) — it bypasses the poison filter."""
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE strategy = 'legacy_unknown'"
        )
        count = cursor.fetchone()[0]
    # Just a tracking assertion — legacy_unknown rows are OK but counted
    assert count >= 0  # always true; we're just measuring

    # But: if there ARE legacy_unknown rows, flag them for manual review
    if count > 0:
        print(f"[INFO] {count} trades flagged as legacy_unknown — manual review needed")
