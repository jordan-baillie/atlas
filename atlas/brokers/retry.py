"""Retry utilities for Atlas broker API calls.

Provides exponential-backoff retry decorators for transient broker/network errors.

Retry policy:
    - 3 attempts total (1 initial + 2 retries)
    - Delays: 1s → 2s → 4s (exponential backoff)
    - Retry on: ConnectionError, TimeoutError, HTTP 429, HTTP 503, HTTP 502
    - Do NOT retry on: HTTP 400, HTTP 401, HTTP 403, HTTP 422

Usage::

    from atlas.brokers.retry import with_retry, RetryableError

    @with_retry()
    def my_api_call():
        ...

    # Or wrap inline:
    result = with_retry()(lambda: my_api_call())()
"""
from __future__ import annotations

import functools
import logging
import time
import urllib.error
from typing import Any, Callable, Optional, Type, Tuple

logger = logging.getLogger("atlas.broker.retry")

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

DEFAULT_MAX_RETRIES = 3       # total attempts (initial + retries)
DEFAULT_BASE_DELAY = 1.0      # seconds before first retry
DEFAULT_BACKOFF_FACTOR = 2.0  # multiply delay by this each retry

# HTTP status codes that should trigger a retry (transient / server-side)
_RETRYABLE_HTTP_STATUS = {429, 502, 503, 504}

# HTTP status codes that should NOT be retried (client errors / auth)
_NON_RETRYABLE_HTTP_STATUS = {400, 401, 403, 422}


# ---------------------------------------------------------------------------
# Exception detection helpers
# ---------------------------------------------------------------------------

def _is_retryable_exception(exc: BaseException) -> bool:
    """Return True if *exc* is a transient error worth retrying.

    Handles:
    - Python built-ins: ConnectionError, TimeoutError, OSError (ECONNRESET)
    - urllib.error.HTTPError with retryable status codes (429, 502, 503, 504)
    - alpaca-py APIError wrapping retryable HTTP status codes

    Never retries non-retryable HTTP codes (400, 401, 403, 422).
    """
    # Standard network errors — always retryable
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True

    # urllib HTTP errors
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _RETRYABLE_HTTP_STATUS

    # alpaca-py SDK errors — inspect the message / status code
    exc_type_name = type(exc).__name__
    exc_str = str(exc)

    # Check for HTTPError-like objects from alpaca-py / requests
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status_code is not None:
        try:
            code = int(status_code)
        except (ValueError, TypeError):
            code = None
        if code is not None:
            if code in _NON_RETRYABLE_HTTP_STATUS:
                return False
            if code in _RETRYABLE_HTTP_STATUS:
                return True

    # Detect rate-limit / server errors by message content
    lower = exc_str.lower()
    if any(kw in lower for kw in ("rate limit", "too many requests", "429")):
        return True
    if any(kw in lower for kw in ("service unavailable", "bad gateway", "503", "502")):
        return True
    if any(kw in lower for kw in ("connection", "timeout", "timed out", "reset")):
        return True

    return False


def _is_non_retryable_exception(exc: BaseException) -> bool:
    """Return True if *exc* is a hard failure that should NOT be retried."""
    exc_str = str(exc)

    # Auth / validation errors — never retry
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status_code is not None:
        try:
            code = int(status_code)
        except (ValueError, TypeError):
            code = None
        if code in _NON_RETRYABLE_HTTP_STATUS:
            return True

    lower = exc_str.lower()
    if any(kw in lower for kw in ("401", "403", "unauthorized", "forbidden")):
        return True
    if any(kw in lower for kw in ("400", "422", "bad request", "unprocessable")):
        return True

    return False


# ---------------------------------------------------------------------------
# Core retry logic
# ---------------------------------------------------------------------------

def with_retry(
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    label: Optional[str] = None,
) -> Callable:
    """Return a decorator that retries a function on transient errors.

    Args:
        max_retries:    Total number of attempts (including initial try).
                        Default: 3 (initial + 2 retries).
        base_delay:     Delay before the first retry, in seconds. Default: 1.0.
        backoff_factor: Multiply delay by this factor each retry. Default: 2.0.
        label:          Human-readable label for log messages (default: function name).

    Returns:
        Decorator that wraps the function with retry logic.

    The decorated function will:
        1. Attempt the call
        2. On transient error, wait `base_delay * backoff_factor^attempt` seconds
        3. Retry up to (max_retries - 1) additional times
        4. Re-raise the last exception if all retries exhausted
        5. Immediately raise on non-retryable errors (auth, validation)

    Example::

        @with_retry(max_retries=3, base_delay=1.0)
        def place_order(...):
            return client.submit_order(...)
    """
    def decorator(func: Callable) -> Callable:
        fn_label = label or func.__qualname__

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Optional[BaseException] = None
            delay = base_delay

            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)

                except BaseException as exc:
                    last_exc = exc

                    # Non-retryable: raise immediately
                    if _is_non_retryable_exception(exc):
                        logger.debug(
                            "[retry] %s: non-retryable error on attempt %d/%d — %s: %s",
                            fn_label, attempt, max_retries, type(exc).__name__, exc,
                        )
                        raise

                    # Not a recognised transient error: raise immediately
                    if not _is_retryable_exception(exc):
                        logger.debug(
                            "[retry] %s: non-transient error on attempt %d/%d — %s: %s",
                            fn_label, attempt, max_retries, type(exc).__name__, exc,
                        )
                        raise

                    # Transient — decide whether to retry
                    if attempt < max_retries:
                        logger.warning(
                            "[retry] %s: transient error on attempt %d/%d "
                            "(retrying in %.1fs) — %s: %s",
                            fn_label, attempt, max_retries, delay,
                            type(exc).__name__, exc,
                        )
                        time.sleep(delay)
                        delay *= backoff_factor
                    else:
                        logger.error(
                            "[retry] %s: all %d attempts exhausted — %s: %s",
                            fn_label, max_retries, type(exc).__name__, exc,
                        )

            # All retries exhausted — raise the last exception
            assert last_exc is not None
            raise last_exc

        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Convenience alias
# ---------------------------------------------------------------------------

def broker_retry(func: Callable) -> Callable:
    """Convenience decorator with Atlas broker default retry settings.

    Equivalent to ``@with_retry(max_retries=3, base_delay=1.0, backoff_factor=2.0)``.
    """
    return with_retry(
        max_retries=DEFAULT_MAX_RETRIES,
        base_delay=DEFAULT_BASE_DELAY,
        backoff_factor=DEFAULT_BACKOFF_FACTOR,
    )(func)
