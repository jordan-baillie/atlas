"""Integration tests for Telegram HTML-escape at sender paths (C1).

Verifies that all dynamic/user-supplied content inserted into Telegram HTML
messages is correctly escaped with tg_escape(), preventing HTTP 400
'can't parse entities' errors when the broker returns strings containing
'<', '>', or '"'.

Test cases:
1. Ticker with XSS-like content -> body has &lt;script&gt;, not <script>
2. Broker-error JSON with '<reason>' -> '<' escaped in captured body
3. tg_escape('"quoted" & amp') -> &quot;quoted&quot; &amp; amp
4. End-to-end: sync_protective_orders notify helper with hostile broker error
   -- no raw '<', '>', '"' outside template tags in captured payload
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_urlopen_mock(captured):
    """Return a urlopen context-manager mock that captures the request body."""
    response = MagicMock()
    response.read.return_value = json.dumps({"ok": True, "result": {}}).encode()
    response.__enter__ = lambda s: response
    response.__exit__ = MagicMock(return_value=False)

    def _urlopen(request, timeout=15):
        captured.append(request.data)  # bytes of the JSON payload
        return response

    return _urlopen


# ---------------------------------------------------------------------------
# Test 1 -- ticker with hostile content
# ---------------------------------------------------------------------------

class TestTickerEscaping:
    """Ticker strings with HTML meta-chars must be escaped before transmission."""

    def test_script_tag_in_ticker_escaped_in_body(self) -> None:
        """Ticker '<script>alert(1)</script>' must not appear unescaped in payload."""
        captured = []
        with patch("urllib.request.urlopen", side_effect=_make_urlopen_mock(captured)):
            from utils.telegram import send_message, tg_escape
            hostile_ticker = "<script>alert(1)</script>"
            msg = "<b>Order for</b> " + tg_escape(hostile_ticker)
            send_message(msg)

        assert captured, "send_message made no HTTP call"
        body_str = captured[0].decode("utf-8")
        assert "<script>" not in body_str, (
            "Raw <script> tag found in Telegram payload -- tg_escape not applied"
        )
        assert "&lt;script&gt;" in body_str, (
            "&lt;script&gt; not found -- escaping did not produce expected entity"
        )


# ---------------------------------------------------------------------------
# Test 2 -- broker-error JSON with angle brackets
# ---------------------------------------------------------------------------

class TestBrokerErrorEscaping:
    """Broker error JSONs containing '<reason>' must be escaped."""

    def test_broker_error_json_angle_brackets_escaped(self) -> None:
        broker_error = '{"code":40310100,"message":"denied <reason>"}'
        captured = []
        with patch("urllib.request.urlopen", side_effect=_make_urlopen_mock(captured)):
            from utils.telegram import send_message, tg_escape
            msg = "<b>Error</b>\n<pre>" + tg_escape(broker_error) + "</pre>"
            send_message(msg)

        assert captured
        body_str = captured[0].decode("utf-8")
        assert "<reason>" not in body_str, (
            "Raw '<reason>' found in payload -- broker error JSON not escaped"
        )
        assert "&lt;reason&gt;" in body_str


# ---------------------------------------------------------------------------
# Test 3 -- double-quote and ampersand escaping
# ---------------------------------------------------------------------------

class TestQuoteAndAmpersandEscaping:
    """tg_escape must escape double-quotes, ampersands, and angle brackets."""

    def test_double_quotes_and_ampersand_escaped(self) -> None:
        from utils.telegram import tg_escape
        result = tg_escape('"quoted" & amp')
        assert result == "&quot;quoted&quot; &amp; amp", (
            "Expected '&quot;quoted&quot; &amp; amp', got " + repr(result)
        )

    def test_ampersand_only(self) -> None:
        from utils.telegram import tg_escape
        assert tg_escape("A & B") == "A &amp; B"

    def test_less_than_only(self) -> None:
        from utils.telegram import tg_escape
        assert tg_escape("x < y") == "x &lt; y"

    def test_greater_than_only(self) -> None:
        from utils.telegram import tg_escape
        assert tg_escape("x > y") == "x &gt; y"

    def test_combined_hostile_string(self) -> None:
        from utils.telegram import tg_escape
        raw = '<b onclick="alert(1)">click</b>'
        result = tg_escape(raw)
        assert "<" not in result
        assert ">" not in result
        assert '"' not in result
        assert result == "&lt;b onclick=&quot;alert(1)&quot;&gt;click&lt;/b&gt;"


# ---------------------------------------------------------------------------
# Test 4 -- end-to-end: sync_protective_orders with hostile content
# ---------------------------------------------------------------------------

class TestSyncProtectiveOrdersEndToEnd:
    """Full pipeline: format_telegram_message + send_message with hostile broker error."""

    _ALLOWED_TAGS = frozenset({
        "b", "/b", "i", "/i", "code", "/code", "pre", "/pre",
        "s", "/s", "u", "/u", "a", "/a",
    })

    def _extract_unknown_tags(self, body):
        raw_tags = re.findall(r"<([^>]+)>", body)
        return [t for t in raw_tags if t.strip().lower().split()[0] not in self._ALLOWED_TAGS]

    def test_hostile_market_error_no_unknown_tags(self) -> None:
        """Market-level broker error with hostile chars must produce clean HTML."""
        sys.path.insert(0, str(PROJECT / "scripts"))
        from sync_protective_orders import format_telegram_message

        hostile_error = 'broker returned {"code":400,"message":"denied <API_LIMIT>"} for ticker'
        results = [
            {
                "market_id": "sp500",
                "error": hostile_error,
                "counts": {},
                "results": {},
            }
        ]
        msg = format_telegram_message(results, "2026-04-24", dry_run=False)

        unknown = self._extract_unknown_tags(msg)
        assert unknown == [], (
            "Unknown HTML tags in message (will cause Telegram 400): " + str(unknown) +
            "\nFull message:\n" + msg
        )
        assert "<API_LIMIT>" not in msg
        assert "&lt;API_LIMIT&gt;" in msg

    def test_hostile_ticker_error_no_unknown_tags(self) -> None:
        """Per-ticker error strings with hostile chars must be escaped."""
        sys.path.insert(0, str(PROJECT / "scripts"))
        from sync_protective_orders import format_telegram_message

        ticker_errors = {
            "NVDA": {
                "errors": ["order rejected: qty <100> exceeds limit"],
                "sl_action": "error",
            }
        }
        results = [
            {
                "market_id": "sp500",
                "error": "",
                "counts": {
                    "positions_checked": 1,
                    "sl_placed": 0, "sl_already_exists": 0,
                    "tp_placed": 0, "tp_already_exists": 0,
                    "sl_skipped": 0, "tp_skipped": 0,
                    "errors": 1, "pdt_deferred": 0, "orphans_cancelled": 0,
                },
                "results": ticker_errors,
            }
        ]
        msg = format_telegram_message(results, "2026-04-24", dry_run=False)

        unknown = self._extract_unknown_tags(msg)
        assert unknown == [], (
            "Unknown HTML tags: " + str(unknown) + "\nMessage:\n" + msg
        )
        assert "<100>" not in msg
        assert "&lt;100&gt;" in msg

    def test_end_to_end_send_no_raw_hostile_chars_in_payload(self) -> None:
        """Full send_telegram_summary pipeline: captured HTTP body must not contain
        raw hostile chars outside intentional HTML template tags."""
        sys.path.insert(0, str(PROJECT / "scripts"))
        from sync_protective_orders import send_telegram_summary

        hostile_broker_error = (
            'AlpacaError: {"code":40310100,'
            '"message":"insufficient qty <4> for NVDA"}'
        )
        results = [
            {
                "market_id": "sp500",
                "error": hostile_broker_error,
                "counts": {},
                "results": {},
            }
        ]

        captured = []
        with patch("urllib.request.urlopen", side_effect=_make_urlopen_mock(captured)):
            ok = send_telegram_summary(results, "2026-04-24", dry_run=False)

        assert captured, (
            "send_telegram_summary made no HTTP call -- "
            "set TELEGRAM_BOT_TOKEN=test TELEGRAM_CHAT_ID=123 in environment"
        )

        payload = json.loads(captured[0].decode("utf-8"))
        text = payload["text"]

        unknown_tags = self._extract_unknown_tags(text)
        assert unknown_tags == [], (
            "Unrecognised HTML tags that Telegram would reject: " + str(unknown_tags) +
            "\nTransmitted text:\n" + text
        )
        assert "<4>" not in text, "Raw '<4>' found in transmitted payload"

    def test_send_telegram_summary_returns_true_on_success(self) -> None:
        """send_telegram_summary returns True when Telegram API responds ok=True."""
        sys.path.insert(0, str(PROJECT / "scripts"))
        from sync_protective_orders import send_telegram_summary

        results = [
            {
                "market_id": "sp500",
                "error": "",
                "counts": {
                    "positions_checked": 2,
                    "sl_placed": 1, "sl_already_exists": 1,
                    "tp_placed": 0, "tp_already_exists": 2,
                    "sl_skipped": 0, "tp_skipped": 0,
                    "errors": 0, "pdt_deferred": 0, "orphans_cancelled": 0,
                },
                "results": {
                    "AAPL": {"sl_action": "placed", "stop_price": 180.0, "qty": 10},
                    "MSFT": {"sl_action": "skipped"},
                },
            }
        ]

        captured = []
        with patch("urllib.request.urlopen", side_effect=_make_urlopen_mock(captured)):
            ok = send_telegram_summary(results, "2026-04-24", dry_run=False)

        assert ok is True
        assert captured
