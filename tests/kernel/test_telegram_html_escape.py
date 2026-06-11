"""Regression tests for Telegram HTML escaping (P1-11).

Root cause: sync_protective_orders.py had a hardcoded literal
``account < $25k`` in a Telegram HTML message.  Telegram's HTML
parser rejects any unrecognised ``<tag>`` with a 400 error.

With PDT-deferred tickers this occurred ~23 times per day.

Fix:
    1. ``atlas.kernel.notify.tg_escape(s)`` — public helper for escaping
       dynamic content before inserting into HTML messages.
    2. All dynamic/variable content in sync_protective_orders.py
       Telegram messages wrapped with ``tg_escape()``.
    3. Hardcoded ``account < $25k`` changed to ``account &lt; $25k``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))


# ── tg_escape unit tests ──────────────────────────────────────────────────────

class TestTgEscape:
    """Unit tests for atlas.kernel.notify.tg_escape."""

    def test_escapes_angle_brackets(self) -> None:
        from atlas.kernel.notify import tg_escape
        assert tg_escape("<abc>") == "&lt;abc&gt;"

    def test_escapes_ampersand(self) -> None:
        from atlas.kernel.notify import tg_escape
        assert tg_escape("A & B") == "A &amp; B"

    def test_escapes_less_than(self) -> None:
        from atlas.kernel.notify import tg_escape
        assert tg_escape("A < B") == "A &lt; B"

    def test_escapes_greater_than(self) -> None:
        from atlas.kernel.notify import tg_escape
        assert tg_escape("A > B") == "A &gt; B"

    def test_none_returns_empty_string(self) -> None:
        """None input must return empty string — callers should never need None guard."""
        from atlas.kernel.notify import tg_escape
        assert tg_escape(None) == ""

    def test_plain_string_unchanged(self) -> None:
        """Strings without special chars pass through unmodified."""
        from atlas.kernel.notify import tg_escape
        assert tg_escape("hello world") == "hello world"

    def test_integer_input_stringified(self) -> None:
        from atlas.kernel.notify import tg_escape
        assert tg_escape(42) == "42"

    def test_json_blob_with_angle_brackets(self) -> None:
        """Broker error JSON that sneaks in angle-bracket-like content is escaped."""
        from atlas.kernel.notify import tg_escape
        raw = '{"message": "insufficient qty <4> for order"}'
        result = tg_escape(raw)
        assert "<" not in result
        assert "&lt;" in result

    def test_pdt_string_with_less_than(self) -> None:
        """The exact string that caused 23/day errors: account < $25k."""
        from atlas.kernel.notify import tg_escape
        raw = "account < $25k"
        result = tg_escape(raw)
        assert "<" not in result
        assert "&lt;" in result
        assert result == "account &lt; $25k"


