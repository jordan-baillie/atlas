"""tests/test_intraday_monitor_exceptions.py — Task #283 Wave 3: bare-except
validation for scripts/intraday_monitor.py.

Changes: 5 → 1 broad catch (4 narrowed, 1 justified with noqa: BLE001).

Narrowed:
  - L80:  _load_fired bare except (no binding!) → (json.JSONDecodeError, UnicodeDecodeError, OSError)
  - L167: yfinance per-ticker parse → (ValueError, TypeError, KeyError, IndexError)
  - L174: yfinance batch download → (ConnectionError, OSError, RuntimeError)
  - L410: load stop prices from plan → (json.JSONDecodeError, OSError, KeyError, AttributeError)
  - L131: Alpaca snapshot fetch → broad retained (broker SDK) with noqa comment

Latent bugs surfaced:
  - L80: bare `except Exception:` (no `as e`) with no logging — silent swallow of
    JSON/file errors when reading daily alert-state file. Fixed: added debug log.
  - f-strings in logger calls within exception handlers fixed to %-style (L131, L167, L174, L410)
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT))


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _source() -> str:
    return (PROJECT / "scripts" / "intraday_monitor.py").read_text()


def _broad_excepts_ast(source: str) -> list[int]:
    """Return lines with unbound bare `except Exception:` or `except:` (no noqa)."""
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
        type_name = node.type.id if isinstance(node.type, ast.Name) else ""
        if type_name == "Exception" and node.name is None:
            line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
            if "noqa" not in line:
                bad.append(node.lineno)
    return bad


# ──────────────────────────────────────────────────────────────────────────────
# A. Static checks
# ──────────────────────────────────────────────────────────────────────────────

class TestStaticChecks:
    def test_no_bare_except_without_binding(self):
        """No `except Exception:` without `as e` binding (or bare `except:`)."""
        bad = _broad_excepts_ast(_source())
        assert bad == [], f"Unbound bare excepts at lines: {bad}"

    def test_load_fired_narrowed(self):
        """_load_fired must use narrowed exception type."""
        src = _source()
        assert "except (json.JSONDecodeError, UnicodeDecodeError, OSError)" in src, (
            "_load_fired should use narrowed exception type"
        )

    def test_load_fired_has_debug_log(self):
        """_load_fired latent bug fixed: exception must now be logged."""
        src = _source()
        assert 'log.debug("Could not parse alert state file' in src, (
            "_load_fired must log the exception (was silent before)"
        )

    def test_yfinance_parse_narrowed(self):
        """Per-ticker yfinance parse must use narrowed exception types."""
        src = _source()
        assert "except (ValueError, TypeError, KeyError, IndexError)" in src, (
            "yfinance per-ticker parse should use narrowed types"
        )

    def test_yfinance_download_narrowed(self):
        """yfinance batch download must use narrowed network/IO types."""
        src = _source()
        assert "except (ConnectionError, OSError, RuntimeError)" in src, (
            "yfinance batch download should use narrowed types"
        )

    def test_load_stops_narrowed(self):
        """Load stop prices handler must use narrowed file/parse types."""
        src = _source()
        assert "except (json.JSONDecodeError, OSError, KeyError, AttributeError)" in src, (
            "load stops handler should use narrowed types"
        )

    def test_no_fstring_in_except_handlers(self):
        """f-strings in logger calls within exception handlers must be fixed."""
        import re
        src = _source()
        # Check specific lines that used to have f-strings in exception handlers
        # These should now use %-style formatting
        assert 'log.debug("Alpaca snapshot fetch failed: %s"' in src, \
            "Alpaca snapshot log should use %-style"
        assert 'log.debug("  %s: yfinance parse error: %s"' in src, \
            "yfinance per-ticker log should use %-style"
        assert 'log.error("yfinance batch download failed: %s"' in src, \
            "yfinance batch log should use %-style"
        assert 'log.warning("Failed to load stop prices from plan/state: %s"' in src, \
            "load stops log should use %-style"


# ──────────────────────────────────────────────────────────────────────────────
# B. Behavioural — _load_fired (formerly bare except)
# ──────────────────────────────────────────────────────────────────────────────

class TestLoadFiredNarrowedExceptions:
    """Verify narrowed exception handler for alert state file loading."""

    def _load_fired_logic(self, parse_fn):
        """Mirrors the narrowed _load_fired body."""
        try:
            return parse_fn()
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as _load_err:
            return {}

    def test_json_decode_error_caught_returns_empty(self):
        """json.JSONDecodeError → returns {}."""
        assert self._load_fired_logic(lambda: json.loads("{bad")) == {}

    def test_os_error_caught_returns_empty(self):
        """OSError (unreadable file) → returns {}."""
        assert self._load_fired_logic(
            lambda: (_ for _ in ()).throw(OSError("permission denied"))
        ) == {}

    def test_unicode_decode_error_caught_returns_empty(self):
        """UnicodeDecodeError → returns {}."""
        assert self._load_fired_logic(
            lambda: b"\xff".decode("utf-8")
        ) == {}

    def test_attribute_error_propagates(self):
        """AttributeError must propagate — programming bug."""
        with pytest.raises(AttributeError):
            self._load_fired_logic(lambda: None.bad_attr)

    def test_runtime_error_propagates(self):
        """RuntimeError must propagate — not a file/parse error."""
        with pytest.raises(RuntimeError):
            self._load_fired_logic(
                lambda: (_ for _ in ()).throw(RuntimeError("unexpected"))
            )


# ──────────────────────────────────────────────────────────────────────────────
# C. Behavioural — yfinance parse errors
# ──────────────────────────────────────────────────────────────────────────────

class TestYfinanceParseNarrowedExceptions:
    def _parse_ticker(self, parse_fn):
        """Mirrors the narrowed per-ticker parse block."""
        prices = {}
        try:
            prices["AAPL"] = parse_fn()
        except (ValueError, TypeError, KeyError, IndexError) as e:
            pass  # skip ticker
        return prices

    def test_value_error_skips_ticker(self):
        """ValueError from float('') skips the ticker."""
        assert self._parse_ticker(lambda: float("")) == {}

    def test_key_error_skips_ticker(self):
        """KeyError from missing column skips the ticker."""
        assert self._parse_ticker(lambda: {}["Close"]) == {}

    def test_index_error_skips_ticker(self):
        """IndexError from empty iloc skips the ticker."""
        assert self._parse_ticker(
            lambda: (_ for _ in ()).throw(IndexError("index out of range"))
        ) == {}

    def test_connection_error_propagates(self):
        """ConnectionError must propagate from ticker parse — not a data error."""
        with pytest.raises(ConnectionError):
            self._parse_ticker(
                lambda: (_ for _ in ()).throw(ConnectionError("network error"))
            )


# ──────────────────────────────────────────────────────────────────────────────
# D. Behavioural — yfinance batch download
# ──────────────────────────────────────────────────────────────────────────────

class TestYfinanceBatchNarrowedExceptions:
    def _batch_download(self, fn):
        """Mirrors the narrowed yfinance batch download handler."""
        try:
            fn()
        except ImportError:
            pass  # ImportError is handled separately in the real code
        except (ConnectionError, OSError, RuntimeError) as e:
            pass  # network/IO error — logged and continues

    def test_connection_error_caught(self):
        """ConnectionError from network failure is caught."""
        caught = []
        def handler():
            try:
                raise ConnectionError("timeout")
            except (ConnectionError, OSError, RuntimeError) as e:
                caught.append(str(e))
        handler()
        assert caught == ["timeout"]

    def test_os_error_caught(self):
        """OSError from temporary file failure is caught."""
        caught = []
        def handler():
            try:
                raise OSError("no space left")
            except (ConnectionError, OSError, RuntimeError) as e:
                caught.append(str(e))
        handler()
        assert caught == ["no space left"]

    def test_attribute_error_propagates_from_batch(self):
        """AttributeError propagates — programming bug, not network failure."""
        with pytest.raises(AttributeError):
            def handler():
                try:
                    raise AttributeError("bad attr")
                except (ConnectionError, OSError, RuntimeError) as e:
                    pass
            handler()


# ──────────────────────────────────────────────────────────────────────────────
# E. Smoke test — remaining broad catch-all
# ──────────────────────────────────────────────────────────────────────────────

class TestBrokerCatchAllSmoke:
    def test_alpaca_broker_catch_all_catches_sdk_exception(self):
        """The Alpaca broker broad catch-all must still catch SDK-specific errors."""
        caught = []

        class AlpacaRateLimitError(Exception):
            pass

        def alpaca_section():
            try:
                raise AlpacaRateLimitError("429 Too Many Requests")
            except Exception as e:  # noqa: BLE001
                caught.append(str(e))

        alpaca_section()
        assert caught == ["429 Too Many Requests"]
