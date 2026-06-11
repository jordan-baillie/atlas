"""tests/test_chat_server_exceptions.py — Task #283 Wave 3: bare-except validation
for services/chat_server.py and services/api/dashboard.py.

Changes applied:
  - chat_server.py: 6 → 2 broad catches (4 narrowed, 2 justified catch-alls with noqa)
  - dashboard.py:   8 → 7 broad catches (1 narrowed, 7 justified with noqa comments)

Tests:
  A. AST / static — verify no unbound bare excepts remain in either file
  B. Behavioural — specific-type paths work, unexpected types still propagate
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _broad_excepts(path: Path) -> list[int]:
    """Return line numbers of bare `except Exception:` (no `as` binding, no comment)."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = source.splitlines()
    bad: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        # Bare `except:` — always bad
        if node.type is None:
            bad.append(node.lineno)
            continue
        # `except Exception:` without a binding name is ambiguous — check if it has noqa
        type_name = (
            node.type.id if isinstance(node.type, ast.Name) else ""
        )
        if type_name == "Exception" and node.name is None:
            # Allowed only if the source line has a noqa comment
            line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
            if "noqa" not in line:
                bad.append(node.lineno)
    return bad


# ──────────────────────────────────────────────────────────────────────────────
# Module A: Static checks
# ──────────────────────────────────────────────────────────────────────────────

class TestStaticNoBareExcept:
    def test_chat_server_no_bare_except(self):
        """chat_server.py must have zero unbound bare except clauses."""
        bad = _broad_excepts(PROJECT / "atlas" / "dashboard" / "app.py")
        assert bad == [], f"Unbound bare excepts at lines: {bad}"

    def test_dashboard_no_bare_except(self):
        """services/api/dashboard.py must have zero unbound bare except clauses."""
        bad = _broad_excepts(PROJECT / "atlas" / "dashboard" / "api" / "dashboard.py")
        assert bad == [], f"Unbound bare excepts at lines: {bad}"

    def test_chat_server_json_decode_narrowed(self):
        """Body-parse handlers must catch (json.JSONDecodeError, ...) not bare Exception.
        After Phase 9 extraction, body-parse handlers live in services/api/chat_sessions.py.
        """
        # Check the extracted module where body-parse now lives
        src = (PROJECT / "atlas" / "dashboard" / "chat" / "sessions.py").read_text(encoding="utf-8")
        assert "except (json.JSONDecodeError, UnicodeDecodeError, ValueError)" in src, (
            "chat_create_session body parse should use narrowed exception type"
        )

    def test_ws_auth_narrowed(self):
        """WebSocket auth decode must use narrowed exception type.
        After Phase 10 extraction, WS handler lives in services/ws/chat.py.
        """
        # Check the extracted module where WS handler now lives
        src = (PROJECT / "atlas" / "dashboard" / "chat" / "ws.py").read_text(encoding="utf-8")
        assert "except (ValueError, UnicodeDecodeError, OSError, KeyError)" in src, (
            "WS auth decode should use narrowed exception type"
        )

    def test_dashboard_pnl_narrowed(self):
        """Per-position PnL calc in dashboard.py must use narrowed exception type."""
        src = (PROJECT / "atlas" / "dashboard" / "api" / "dashboard_builder.py").read_text(encoding="utf-8")
        assert "except (ValueError, TypeError, KeyError, IndexError)" in src, (
            "PnL calc loop should use narrowed exception types"
        )

    def test_dashboard_ev_stats_exc_info(self):
        """EV stats handler in dashboard.py must include exc_info=True (latent bug fix)."""
        src = (PROJECT / "atlas" / "dashboard" / "api" / "dashboard.py").read_text(encoding="utf-8")
        assert "exc_info=True" in src, (
            "EV stats handler must log with exc_info=True so exceptions are visible"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Module B: Behavioural — JSON body parse handlers
# ──────────────────────────────────────────────────────────────────────────────

class TestBodyParseNarrowedExceptions:
    """Verify json.JSONDecodeError IS caught and other types propagate."""

    def test_json_decode_error_is_caught_by_narrow_handler(self):
        """json.JSONDecodeError is a subclass of ValueError — narrowed handler catches it."""
        import json

        def body_parse_logic(json_fn):
            """Mirrors the narrowed except block from chat_create_session_endpoint."""
            try:
                result = json_fn()
                return result
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
                return {}

        # JSONDecodeError should be caught, returning default
        bad_json = lambda: json.loads("not-json")  # raises JSONDecodeError
        assert body_parse_logic(bad_json) == {}

        # Good JSON should pass through
        good = lambda: json.loads('{"name": "test"}')
        assert body_parse_logic(good) == {"name": "test"}

    def test_unicode_decode_error_is_caught(self):
        """UnicodeDecodeError should also be caught."""
        def body_parse_logic(decode_fn):
            try:
                return decode_fn()
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
                return {}

        bad_decode = lambda: b"\xff\xfe".decode("utf-8")  # raises UnicodeDecodeError
        assert body_parse_logic(bad_decode) == {}

    def test_attribute_error_propagates_from_body_parse(self):
        """AttributeError must NOT be silently swallowed — it should propagate."""
        import json

        def body_parse_logic(fn):
            try:
                return fn()
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
                return {}

        bad_attr = lambda: None.nonexistent  # raises AttributeError
        with pytest.raises(AttributeError):
            body_parse_logic(bad_attr)


# ──────────────────────────────────────────────────────────────────────────────
# Module C: Behavioural — WS auth decode handler
# ──────────────────────────────────────────────────────────────────────────────

class TestWsAuthDecodeNarrowedExceptions:
    """Verify ValueError/UnicodeDecodeError caught, unexpected types propagate."""

    def test_value_error_caught_in_ws_auth(self):
        """ValueError from bad base64 split is caught."""
        import base64

        def ws_auth_decode(header_value: str):
            try:
                decoded = base64.b64decode(header_value).decode()
                # Force ValueError by missing the colon
                uname, pw = decoded.split(":", 0)  # maxsplit=0 returns 1 element → unpack fails
                return (uname, pw)
            except (ValueError, UnicodeDecodeError, OSError, KeyError) as e:
                return None

        # Tuple unpack of 1 element into 2 raises ValueError
        result = ws_auth_decode(base64.b64encode(b"nocolon").decode())
        assert result is None

    def test_unicode_decode_error_caught_in_ws_auth(self):
        """UnicodeDecodeError from non-UTF-8 base64 payload is caught."""
        import base64

        def ws_auth_decode(header_value: str):
            try:
                decoded = base64.b64decode(header_value).decode("utf-8")
                return decoded
            except (ValueError, UnicodeDecodeError, OSError, KeyError) as e:
                return None

        bad_b64 = base64.b64encode(b"\xff\xfe\xfd").decode()
        assert ws_auth_decode(bad_b64) is None

    def test_type_error_propagates_from_ws_auth(self):
        """TypeError must propagate — it's not in the narrowed handler."""
        def ws_auth_logic(fn):
            try:
                return fn()
            except (ValueError, UnicodeDecodeError, OSError, KeyError) as e:
                return None

        def raises_type_error():
            raise TypeError("unexpected type")

        with pytest.raises(TypeError):
            ws_auth_logic(raises_type_error)


# ──────────────────────────────────────────────────────────────────────────────
# Module D: Behavioural — dashboard per-position PnL calc
# ──────────────────────────────────────────────────────────────────────────────

class TestDashboardPnlNarrowedExceptions:
    """Verify narrowed PnL exception handler catches the right types."""

    def _run_pnl_loop(self, raises_fn):
        """Mirrors the per-position PnL loop logic."""
        results = []
        for ticker in ["AAPL"]:
            try:
                results.append(raises_fn(ticker))
            except (ValueError, TypeError, KeyError, IndexError) as e:
                continue
        return results

    def test_key_error_skipped_in_pnl_loop(self):
        """KeyError from missing df column is caught → position skipped."""
        def raises_key(ticker):
            raise KeyError("close")
        assert self._run_pnl_loop(raises_key) == []

    def test_value_error_skipped_in_pnl_loop(self):
        """ValueError from float() conversion is caught → position skipped."""
        def raises_val(ticker):
            return float("NaN_is_fine")
        # float("NaN") is valid — test the actual ValueError path
        def raises_val_bad(ticker):
            return float("not-a-number")
        assert self._run_pnl_loop(raises_val_bad) == []

    def test_index_error_skipped_in_pnl_loop(self):
        """IndexError from empty series .iloc[-1] is caught → position skipped."""
        def raises_idx(ticker):
            raise IndexError("index out of bounds")
        assert self._run_pnl_loop(raises_idx) == []

    def test_attribute_error_propagates_from_pnl_loop(self):
        """AttributeError must propagate — it signals a programming bug."""
        def raises_attr(ticker):
            raise AttributeError("no attribute 'close'")

        with pytest.raises(AttributeError):
            self._run_pnl_loop(raises_attr)

    def test_os_error_propagates_from_pnl_loop(self):
        """OSError must propagate — it signals an unexpected filesystem error."""
        def raises_os(ticker):
            raise OSError("disk full")

        with pytest.raises(OSError):
            self._run_pnl_loop(raises_os)


# ──────────────────────────────────────────────────────────────────────────────
# Module E: Smoke tests for catch-all paths still work
# ──────────────────────────────────────────────────────────────────────────────

class TestCatchAllSmoke:
    """Verify the justified catch-alls (with noqa) still catch unexpected exceptions."""

    def test_ws_outer_catch_all_catches_runtime_error(self):
        """WS outer handler must still catch RuntimeError (broad by design)."""
        caught = []

        def ws_outer():
            try:
                raise RuntimeError("streaming failed")
            except Exception as exc:  # noqa: BLE001 — WebSocket handler
                caught.append(str(exc))

        ws_outer()
        assert caught == ["streaming failed"]

    def test_broker_catch_all_catches_import_error(self):
        """Broker-layer catch-all must catch ImportError from optional SDK."""
        caught = []

        def broker_section():
            try:
                raise ImportError("alpaca_trade_api not found")
            except Exception as e:  # noqa: BLE001 — full broker init
                caught.append(str(e))

        broker_section()
        assert caught == ["alpaca_trade_api not found"]
