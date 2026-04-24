"""Regression tests for Telegram HTML escaping (P1-11).

Root cause: sync_protective_orders.py had a hardcoded literal
``account < $25k`` in a Telegram HTML message.  Telegram's HTML
parser rejects any unrecognised ``<tag>`` with a 400 error.

With PDT-deferred tickers this occurred ~23 times per day.

Fix:
    1. ``utils.telegram.tg_escape(s)`` — public helper for escaping
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

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# ── tg_escape unit tests ──────────────────────────────────────────────────────

class TestTgEscape:
    """Unit tests for utils.telegram.tg_escape."""

    def test_escapes_angle_brackets(self) -> None:
        from utils.telegram import tg_escape
        assert tg_escape("<abc>") == "&lt;abc&gt;"

    def test_escapes_ampersand(self) -> None:
        from utils.telegram import tg_escape
        assert tg_escape("A & B") == "A &amp; B"

    def test_escapes_less_than(self) -> None:
        from utils.telegram import tg_escape
        assert tg_escape("A < B") == "A &lt; B"

    def test_escapes_greater_than(self) -> None:
        from utils.telegram import tg_escape
        assert tg_escape("A > B") == "A &gt; B"

    def test_none_returns_empty_string(self) -> None:
        """None input must return empty string — callers should never need None guard."""
        from utils.telegram import tg_escape
        assert tg_escape(None) == ""

    def test_plain_string_unchanged(self) -> None:
        """Strings without special chars pass through unmodified."""
        from utils.telegram import tg_escape
        assert tg_escape("hello world") == "hello world"

    def test_integer_input_stringified(self) -> None:
        from utils.telegram import tg_escape
        assert tg_escape(42) == "42"

    def test_json_blob_with_angle_brackets(self) -> None:
        """Broker error JSON that sneaks in angle-bracket-like content is escaped."""
        from utils.telegram import tg_escape
        raw = '{"message": "insufficient qty <4> for order"}'
        result = tg_escape(raw)
        assert "<" not in result
        assert "&lt;" in result

    def test_pdt_string_with_less_than(self) -> None:
        """The exact string that caused 23/day errors: account < $25k."""
        from utils.telegram import tg_escape
        raw = "account < $25k"
        result = tg_escape(raw)
        assert "<" not in result
        assert "&lt;" in result
        assert result == "account &lt; $25k"


# ── format_telegram_message escaping tests ───────────────────────────────────

class TestFormatTelegramMessage:
    """Verify format_telegram_message escapes dynamic content."""

    def _make_market_result(
        self,
        market_id: str = "sp500",
        error: str | None = None,
        pdt_deferred: int = 0,
        ticker_errors: dict | None = None,
    ) -> dict:
        return {
            "market_id": market_id,
            "error": error,
            "counts": {
                "positions_checked": 5,
                "sl_placed": 0,
                "tp_placed": 0,
                "sl_already_exists": 5,
                "tp_already_exists": 0,
                "sl_skipped": 0,
                "tp_skipped": 5,
                "errors": len(ticker_errors or {}),
                "pdt_deferred": pdt_deferred,
                "orphans_cancelled": 0,
            },
            "results": ticker_errors or {},
        }

    def test_pdt_deferred_message_no_angle_brackets(self) -> None:
        """PDT-deferred count line must NOT produce raw < in output (root cause)."""
        sys.path.insert(0, str(PROJECT / "scripts"))
        from sync_protective_orders import format_telegram_message

        results = [self._make_market_result(pdt_deferred=2)]
        msg = format_telegram_message(results, "2026-04-24", dry_run=False)

        # The hardcoded "account < $25k" must be escaped
        assert "account < $25k" not in msg, (
            "Unescaped 'account < $25k' found — this causes Telegram 400 errors"
        )
        assert "account &lt; $25k" in msg

    def test_market_error_with_angle_brackets_escaped(self) -> None:
        """Market-level errors containing < > must be escaped."""
        sys.path.insert(0, str(PROJECT / "scripts"))
        from sync_protective_orders import format_telegram_message

        results = [self._make_market_result(error="broker returned <error>: unknown")]
        msg = format_telegram_message(results, "2026-04-24", dry_run=False)

        # The raw < must not appear outside of our intentional tags
        # (we check the body line — not the entire message which has <b> etc.)
        assert "<error>" not in msg
        assert "&lt;error&gt;" in msg

    def test_ticker_error_with_angle_brackets_escaped(self) -> None:
        """Per-ticker error strings containing < > must be escaped."""
        sys.path.insert(0, str(PROJECT / "scripts"))
        from sync_protective_orders import format_telegram_message

        ticker_errors = {
            "CCJ": {
                "errors": ['insufficient qty <4> for order'],
                "sl_action": "error",
            }
        }
        results = [self._make_market_result(ticker_errors=ticker_errors)]
        msg = format_telegram_message(results, "2026-04-24", dry_run=False)

        assert "<4>" not in msg
        assert "&lt;4&gt;" in msg

    def test_message_with_no_errors_is_valid_html(self) -> None:
        """Clean message (no errors, no PDT) must produce well-formed-ish HTML."""
        sys.path.insert(0, str(PROJECT / "scripts"))
        from sync_protective_orders import format_telegram_message

        results = [self._make_market_result()]
        msg = format_telegram_message(results, "2026-04-24", dry_run=False)

        # Must contain our intentional formatting tags
        assert "<b>" in msg
        assert "</b>" in msg

    def test_would_have_failed_before_fix(self) -> None:
        """Integration: construct a message with the old bad string and verify
        it does NOT appear after the fix."""
        sys.path.insert(0, str(PROJECT / "scripts"))
        from sync_protective_orders import format_telegram_message

        # Trigger PDT path
        results = [self._make_market_result(pdt_deferred=1)]
        msg = format_telegram_message(results, "2026-04-24", dry_run=False)

        # Simulate sending via Telegram — check for unrecognised tags
        # (Telegram rejects any < that isn't part of a recognised tag)
        import re
        allowed_tags = {"b", "/b", "i", "/i", "code", "/code", "pre", "/pre", "s", "/s", "u", "/u", "a", "/a"}
        raw_tags = re.findall(r"<([^>]+)>", msg)
        unknown = [t for t in raw_tags if t.lower().split()[0] not in allowed_tags]
        assert unknown == [], (
            f"Message contains unrecognised HTML tags that Telegram will reject: {unknown}\n"
            f"Full message:\n{msg}"
        )
