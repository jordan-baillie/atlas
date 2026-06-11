"""Tests for brokers/retry.py — A6: API retry with exponential backoff.

Verifies:
- Retries on transient errors (ConnectionError, TimeoutError, 429, 503, 502)
- Immediate failure on non-retryable errors (400, 401, 403, 422)
- Correct delay progression (1s → 2s → 4s)
- Re-raises original exception after max retries
- Returns result on success after initial failures

Run:
    cd /root/atlas && python3 -m pytest tests/test_retry.py -v
"""
from __future__ import annotations

import sys
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from atlas.brokers.retry import (
    with_retry,
    broker_retry,
    _is_retryable_exception,
    _is_non_retryable_exception,
    DEFAULT_MAX_RETRIES,
    DEFAULT_BASE_DELAY,
)


# ── Helpers ───────────────────────────────────────────────────

class _HTTPLikeError(Exception):
    """Simulates alpaca-py / requests HTTP errors with a status_code attr."""
    def __init__(self, status_code: int, msg: str = ""):
        super().__init__(msg or f"HTTP {status_code}")
        self.status_code = status_code


class _URLLibHTTPError(urllib.error.HTTPError):
    def __init__(self, code: int):
        super().__init__(url="http://example.com", code=code, msg="", hdrs=None, fp=None)


# ── _is_retryable_exception ───────────────────────────────────

class TestIsRetryableException:
    def test_connection_error(self):
        assert _is_retryable_exception(ConnectionError("refused"))

    def test_timeout_error(self):
        assert _is_retryable_exception(TimeoutError("timed out"))

    def test_urllib_429(self):
        assert _is_retryable_exception(_URLLibHTTPError(429))

    def test_urllib_503(self):
        assert _is_retryable_exception(_URLLibHTTPError(503))

    def test_urllib_502(self):
        assert _is_retryable_exception(_URLLibHTTPError(502))

    def test_http_like_429(self):
        assert _is_retryable_exception(_HTTPLikeError(429))

    def test_http_like_503(self):
        assert _is_retryable_exception(_HTTPLikeError(503))

    def test_message_rate_limit(self):
        assert _is_retryable_exception(Exception("rate limit exceeded"))

    def test_message_service_unavailable(self):
        assert _is_retryable_exception(Exception("service unavailable"))

    def test_message_connection_reset(self):
        assert _is_retryable_exception(Exception("connection reset by peer"))

    def test_not_retryable_value_error(self):
        assert not _is_retryable_exception(ValueError("bad input"))

    def test_not_retryable_type_error(self):
        assert not _is_retryable_exception(TypeError("wrong type"))

    def test_http_like_401(self):
        assert not _is_retryable_exception(_HTTPLikeError(401))

    def test_http_like_403(self):
        assert not _is_retryable_exception(_HTTPLikeError(403))


# ── _is_non_retryable_exception ───────────────────────────────

class TestIsNonRetryableException:
    def test_http_like_400(self):
        assert _is_non_retryable_exception(_HTTPLikeError(400))

    def test_http_like_401(self):
        assert _is_non_retryable_exception(_HTTPLikeError(401))

    def test_http_like_403(self):
        assert _is_non_retryable_exception(_HTTPLikeError(403))

    def test_http_like_422(self):
        assert _is_non_retryable_exception(_HTTPLikeError(422))

    def test_message_unauthorized(self):
        assert _is_non_retryable_exception(Exception("401 unauthorized"))

    def test_message_forbidden(self):
        assert _is_non_retryable_exception(Exception("403 forbidden"))

    def test_message_bad_request(self):
        assert _is_non_retryable_exception(Exception("400 bad request"))

    def test_not_non_retryable_connection(self):
        assert not _is_non_retryable_exception(ConnectionError("refused"))

    def test_not_non_retryable_http_429(self):
        assert not _is_non_retryable_exception(_HTTPLikeError(429))


# ── with_retry decorator ──────────────────────────────────────

class TestWithRetry:
    def test_success_first_try(self):
        """No retries needed when first call succeeds."""
        call_count = 0

        @with_retry(max_retries=3, base_delay=0.0)
        def always_ok():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = always_ok()
        assert result == "ok"
        assert call_count == 1

    def test_retries_on_transient_error_then_succeeds(self):
        """Retries on ConnectionError and returns result when it succeeds."""
        attempts = []

        @with_retry(max_retries=3, base_delay=0.0)
        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionError("transient")
            return "success"

        result = flaky()
        assert result == "success"
        assert len(attempts) == 3

    def test_raises_after_max_retries_exhausted(self):
        """Re-raises the last exception after all retries are exhausted."""
        @with_retry(max_retries=3, base_delay=0.0)
        def always_fails():
            raise ConnectionError("always fails")

        with pytest.raises(ConnectionError, match="always fails"):
            always_fails()

    def test_no_retry_on_non_retryable_error(self):
        """Raises immediately on non-retryable HTTP 401 error."""
        call_count = 0

        @with_retry(max_retries=3, base_delay=0.0)
        def auth_error():
            nonlocal call_count
            call_count += 1
            raise _HTTPLikeError(401, "unauthorized")

        with pytest.raises(_HTTPLikeError):
            auth_error()
        assert call_count == 1, "Should not retry on 401"

    def test_no_retry_on_400_bad_request(self):
        """Raises immediately on 400 Bad Request."""
        call_count = 0

        @with_retry(max_retries=3, base_delay=0.0)
        def bad_request():
            nonlocal call_count
            call_count += 1
            raise _HTTPLikeError(400, "bad request")

        with pytest.raises(_HTTPLikeError):
            bad_request()
        assert call_count == 1

    def test_no_retry_on_value_error(self):
        """Raises immediately on non-recognised exceptions (ValueError)."""
        call_count = 0

        @with_retry(max_retries=3, base_delay=0.0)
        def value_error():
            nonlocal call_count
            call_count += 1
            raise ValueError("bad value")

        with pytest.raises(ValueError):
            value_error()
        assert call_count == 1

    def test_retries_on_429_rate_limit(self):
        """Retries on HTTP 429 Too Many Requests."""
        attempts = []

        @with_retry(max_retries=3, base_delay=0.0)
        def rate_limited():
            attempts.append(1)
            if len(attempts) < 2:
                raise _HTTPLikeError(429, "rate limit")
            return "ok"

        result = rate_limited()
        assert result == "ok"
        assert len(attempts) == 2

    def test_retries_on_urllib_503(self):
        """Retries on urllib HTTP 503 Service Unavailable."""
        attempts = []

        @with_retry(max_retries=3, base_delay=0.0)
        def service_unavailable():
            attempts.append(1)
            if len(attempts) < 3:
                raise _URLLibHTTPError(503)
            return "recovered"

        result = service_unavailable()
        assert result == "recovered"
        assert len(attempts) == 3

    def test_delay_progression(self):
        """Verifies exponential backoff: 1s → 2s after each retry."""
        sleep_calls = []

        @with_retry(max_retries=3, base_delay=1.0, backoff_factor=2.0)
        def always_fails():
            raise ConnectionError("always")

        with patch("atlas.brokers.retry.time.sleep", side_effect=lambda d: sleep_calls.append(d)):
            with pytest.raises(ConnectionError):
                always_fails()

        # Should sleep twice: 1.0s then 2.0s (3 attempts = 2 sleep intervals)
        assert len(sleep_calls) == 2, f"Expected 2 sleeps, got {sleep_calls}"
        assert sleep_calls[0] == pytest.approx(1.0)
        assert sleep_calls[1] == pytest.approx(2.0)

    def test_returns_original_exception_type(self):
        """The re-raised exception is the original type, not wrapped."""
        @with_retry(max_retries=2, base_delay=0.0)
        def timeout():
            raise TimeoutError("request timed out")

        with pytest.raises(TimeoutError, match="request timed out"):
            timeout()

    def test_max_retries_1_means_no_retry(self):
        """With max_retries=1, the function is called exactly once."""
        call_count = 0

        @with_retry(max_retries=1, base_delay=0.0)
        def one_shot():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("fail")

        with pytest.raises(ConnectionError):
            one_shot()
        assert call_count == 1


# ── broker_retry convenience decorator ───────────────────────

class TestBrokerRetry:
    def test_broker_retry_applies_defaults(self):
        """broker_retry uses 3 retries with 1s base delay."""
        attempts = []

        @broker_retry
        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionError("transient")
            return "ok"

        with patch("atlas.brokers.retry.time.sleep"):
            result = flaky()

        assert result == "ok"
        assert len(attempts) == 3

    def test_broker_retry_no_retry_on_auth_error(self):
        """broker_retry still skips retry for auth errors."""
        call_count = 0

        @broker_retry
        def auth_fail():
            nonlocal call_count
            call_count += 1
            raise _HTTPLikeError(403, "forbidden")

        with pytest.raises(_HTTPLikeError):
            auth_fail()
        assert call_count == 1
