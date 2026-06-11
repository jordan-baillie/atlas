"""Tests for utils/error_fingerprint.py — deterministic error fingerprinting.

Test IDs match the spec list (1-20).
"""
from __future__ import annotations

import random
import string
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atlas.kernel.error_fingerprint import compute_fingerprint, normalize_message, _TICKER_KEEP


# ---------------------------------------------------------------------------
# normalize_message() tests
# ---------------------------------------------------------------------------

class TestNormalizeMessage:

    def test_empty_string_returns_empty(self):
        assert normalize_message("") == ""

    def test_none_equivalent_msg(self):
        """Empty string path from compute_fingerprint when message is None."""
        assert normalize_message("") == ""

    def test_iso_timestamp_with_T_collapsed(self):
        out = normalize_message("Event at 2026-04-29T10:00:00")
        assert "<TS>" in out
        assert "2026-04-29" not in out
        assert "10:00:00" not in out

    def test_iso_timestamp_with_tz_offset_collapsed(self):
        out = normalize_message("Error at 2026-04-29T10:00:00+10:00")
        assert "<TS>" in out

    def test_iso_timestamp_with_z_collapsed(self):
        out = normalize_message("Error at 2026-04-29T10:00:00Z")
        assert "<TS>" in out

    def test_date_only_collapsed(self):
        out = normalize_message("Trade date 2026-04-29 processed")
        assert "<DATE>" in out
        assert "2026-04-29" not in out

    def test_absolute_path_collapsed(self):
        out = normalize_message("Error in /root/atlas/brokers/live_executor.py")
        assert "<PATH>" in out
        assert "/root" not in out

    def test_number_collapsed(self):
        out = normalize_message("Retry 3 of 5 attempts")
        assert "<N>" in out
        assert "3" not in out

    def test_ticker_replaced(self):
        out = normalize_message("Order failed for AAPL today")
        assert "<TICKER>" in out
        assert "AAPL" not in out

    def test_ticker_keep_tokens_not_replaced(self):
        """Tokens in _TICKER_KEEP are preserved."""
        for tok in ("TODO", "API", "HTTP", "WARNING", "INFO", "ERROR", "DEBUG"):
            out = normalize_message(f"See {tok} for details")
            assert tok in out, f"{tok!r} should not be replaced"
            assert "<TICKER>" not in out

    def test_hex_string_collapsed(self):
        """8+ hex chars → <HEX>."""
        out = normalize_message("Commit abcdef12 failed")
        assert "<HEX>" in out
        assert "abcdef12" not in out

    def test_bracket_content_collapsed(self):
        out = normalize_message("Error [plan_id=42]")
        assert "[<...>]" in out
        assert "plan_id=42" not in out

    def test_multiline_message_no_crash(self):
        msg = "Error\nTraceback:\n  File /root/atlas/x.py line 42\n  ValueError: bad"
        out = normalize_message(msg)
        assert out  # non-empty, no crash

    def test_non_ascii_message_no_crash(self):
        out = normalize_message("Erreur: café at 2026-04-29T10:00:00")
        assert "<TS>" in out  # timestamp collapsed, no crash


# ---------------------------------------------------------------------------
# compute_fingerprint() tests
# ---------------------------------------------------------------------------

class TestComputeFingerprint:

    # 1 — deterministic
    def test_same_inputs_same_fingerprint(self):
        fp1 = compute_fingerprint("ConnectionError", "Order failed for AAPL", "live_executor.py", 474)
        fp2 = compute_fingerprint("ConnectionError", "Order failed for AAPL", "live_executor.py", 474)
        assert fp1 == fp2

    # 2 — same normalised message + file:line
    def test_same_normalised_message_same_fingerprint(self):
        fp1 = compute_fingerprint("ValueError", "failed 3 times", "x.py", 10)
        fp2 = compute_fingerprint("ValueError", "failed 3 times", "x.py", 10)
        assert fp1 == fp2

    # 3 — different tickers → same fingerprint
    def test_different_tickers_same_fingerprint(self):
        fp1 = compute_fingerprint("ConnectionError", "Order failed for AAPL", "live_executor.py", 474)
        fp2 = compute_fingerprint("ConnectionError", "Order failed for TSLA", "live_executor.py", 474)
        assert fp1 == fp2

    # 4 — different numbers → same fingerprint
    def test_different_numbers_same_fingerprint(self):
        fp1 = compute_fingerprint(None, "retry 3/5 failed", None, None)
        fp2 = compute_fingerprint(None, "retry 7/9 failed", None, None)
        assert fp1 == fp2

    # 5 — different timestamps → same fingerprint
    def test_different_timestamps_same_fingerprint(self):
        fp1 = compute_fingerprint(None, "Event at 2026-04-29T10:00:00", None, None)
        fp2 = compute_fingerprint(None, "Event at 2026-04-30T11:30:00Z", None, None)
        assert fp1 == fp2

    # 6 — different absolute paths → same fingerprint
    def test_different_paths_same_fingerprint(self):
        fp1 = compute_fingerprint(None, "Error in /root/atlas/live_executor.py", None, None)
        fp2 = compute_fingerprint(None, "Error in /tmp/atlas/live_executor.py", None, None)
        assert fp1 == fp2

    # 7 — different exc_type → different fingerprint
    def test_different_exc_type_different_fingerprint(self):
        fp1 = compute_fingerprint("ConnectionError", "failed", "file.py", 10)
        fp2 = compute_fingerprint("TimeoutError", "failed", "file.py", 10)
        assert fp1 != fp2

    # 8 — different file_path → different fingerprint
    def test_different_file_path_different_fingerprint(self):
        fp1 = compute_fingerprint("ValueError", "bad input", "file_a.py", 10)
        fp2 = compute_fingerprint("ValueError", "bad input", "file_b.py", 10)
        assert fp1 != fp2

    # 9 — different line_number → different fingerprint
    def test_different_line_number_different_fingerprint(self):
        fp1 = compute_fingerprint("ValueError", "bad input", "file.py", 10)
        fp2 = compute_fingerprint("ValueError", "bad input", "file.py", 20)
        assert fp1 != fp2

    # 10 — None/empty inputs handled gracefully
    def test_none_inputs_no_crash(self):
        fp = compute_fingerprint(None, "", None, None)
        assert len(fp) == 16

    def test_all_none_no_crash(self):
        fp = compute_fingerprint(None, None, None, None)
        assert len(fp) == 16

    # 11 — length is 16 hex chars
    def test_fingerprint_is_16_hex_chars(self):
        fp = compute_fingerprint("ValueError", "bad input", "file.py", 10)
        assert len(fp) == 16
        int(fp, 16)  # raises ValueError if not valid hex

    # 12 — _TICKER_KEEP tokens NOT replaced
    def test_ticker_keep_tokens_stable_in_fingerprint(self):
        """_TICKER_KEEP words don't vary the fingerprint when they stay literal."""
        fp1 = compute_fingerprint(None, "HTTP TODO WARNING in API", None, None)
        fp2 = compute_fingerprint(None, "HTTP TODO WARNING in API", None, None)
        assert fp1 == fp2
        # Verify the keep-list words are not in the ticker set that gets replaced
        for tok in ("TODO", "API", "HTTP", "WARNING"):
            assert tok in _TICKER_KEEP

    # 13 — ISO timestamps with timezone collapse to <TS>
    def test_iso_ts_with_tz_collapsed(self):
        fp1 = compute_fingerprint(None, "Failed at 2026-04-29T10:00:00+10:00", None, None)
        fp2 = compute_fingerprint(None, "Failed at 2026-04-30T11:00:00-05:00", None, None)
        assert fp1 == fp2

    # 14 — Bracket content collapse
    def test_bracket_content_collapse(self):
        msg1 = "Error [plan_id=42]"
        msg2 = "Error [plan_id=99]"
        fp1 = compute_fingerprint(None, msg1, None, None)
        fp2 = compute_fingerprint(None, msg2, None, None)
        assert fp1 == fp2

    # 15 — Hex string collapse
    def test_hex_string_collapse(self):
        msg1 = "Order abcdef12 submitted"
        msg2 = "Order 9876cafe submitted"
        fp1 = compute_fingerprint(None, msg1, None, None)
        fp2 = compute_fingerprint(None, msg2, None, None)
        assert fp1 == fp2

    # 16 — Property test: 1000 random fingerprints
    def test_property_no_length_surprise(self):
        rng = random.Random(42)
        chars = string.ascii_letters + string.digits + " !@#$%/._-"
        for i in range(1000):
            msg = "".join(rng.choices(chars, k=rng.randint(0, 200)))
            exc = rng.choice(["ValueError", "TypeError", "ConnectionError", None])
            lineno = rng.randint(1, 5000)
            fp = compute_fingerprint(exc, msg, "test.py", lineno)
            assert len(fp) == 16, f"Iteration {i}: unexpected length {len(fp)}"
            int(fp, 16)  # confirm valid hex

    # 17 — Atlas pattern: "Circuit breaker tripped" stable
    def test_circuit_breaker_pattern_stable(self):
        msg1 = "Circuit breaker tripped for AAPL at 2026-04-29T10:00:00"
        msg2 = "Circuit breaker tripped for MSFT at 2026-04-30T11:30:00Z"
        fp1 = compute_fingerprint("RuntimeError", msg1, "monitor/intraday.py", 245)
        fp2 = compute_fingerprint("RuntimeError", msg2, "monitor/intraday.py", 245)
        assert fp1 == fp2

    # 18 — Atlas pattern: "Execution blocked" stable
    def test_execution_blocked_pattern_stable(self):
        msg1 = "Execution blocked: Plan status is REJECTED at 2026-04-29"
        msg2 = "Execution blocked: Plan status is REJECTED at 2026-04-30"
        fp1 = compute_fingerprint("ValueError", msg1, "execute_approved.py", 88)
        fp2 = compute_fingerprint("ValueError", msg2, "execute_approved.py", 88)
        assert fp1 == fp2

    # 19 — Multi-line traceback in message handled
    def test_multiline_traceback_message(self):
        msg = (
            "Error\n"
            "Traceback (most recent call last):\n"
            "  File /root/atlas/brokers/live_executor.py, line 474\n"
            "    raise ConnectionError('broker down')\n"
            "ConnectionError: broker down"
        )
        fp = compute_fingerprint("ConnectionError", msg, "live_executor.py", 474)
        assert len(fp) == 16
        int(fp, 16)

    # 20 — Non-ASCII chars handled
    def test_non_ascii_message(self):
        fp = compute_fingerprint(
            "UnicodeError",
            "café failed at 2026-04-29T10:00:00 — données invalides",
            None,
            None,
        )
        assert len(fp) == 16
        int(fp, 16)
