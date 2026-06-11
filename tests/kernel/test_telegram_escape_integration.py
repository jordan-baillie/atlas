"""Integration tests for Telegram HTML-escape at sender paths (C1).

Verifies that all dynamic/user-supplied content inserted into Telegram HTML
messages is correctly escaped with tg_escape(), preventing HTTP 400
'can't parse entities' errors when the broker returns strings containing
'<', '>', or '"'.

Test cases:
1. Ticker with XSS-like content -> body has &lt;script&gt;, not <script>
2. Broker-error JSON with '<reason>' -> '<' escaped in captured body
3. tg_escape('"quoted" & amp') -> &quot;quoted&quot; &amp; amp
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))


@pytest.fixture(autouse=True)
def _fake_telegram_creds(monkeypatch):
    """send_message loads creds before building the request — fake them so the
    mocked urlopen is actually reached (urlopen is always patched; no network)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "000:test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")


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
            from atlas.kernel.notify import send_message, tg_escape
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
            from atlas.kernel.notify import send_message, tg_escape
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
        from atlas.kernel.notify import tg_escape
        result = tg_escape('"quoted" & amp')
        assert result == "&quot;quoted&quot; &amp; amp", (
            "Expected '&quot;quoted&quot; &amp; amp', got " + repr(result)
        )

    def test_ampersand_only(self) -> None:
        from atlas.kernel.notify import tg_escape
        assert tg_escape("A & B") == "A &amp; B"

    def test_less_than_only(self) -> None:
        from atlas.kernel.notify import tg_escape
        assert tg_escape("x < y") == "x &lt; y"

    def test_greater_than_only(self) -> None:
        from atlas.kernel.notify import tg_escape
        assert tg_escape("x > y") == "x &gt; y"

    def test_combined_hostile_string(self) -> None:
        from atlas.kernel.notify import tg_escape
        raw = '<b onclick="alert(1)">click</b>'
        result = tg_escape(raw)
        assert "<" not in result
        assert ">" not in result
        assert '"' not in result
        assert result == "&lt;b onclick=&quot;alert(1)&quot;&gt;click&lt;/b&gt;"


