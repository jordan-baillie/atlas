"""Deterministic error fingerprinting for the auto-remediation system.

The fingerprint collapses semantically-identical errors that differ only
in tickers, timestamps, paths, or numbers. Used to dedup the errors table
and to drive cooldown/rate-limit logic.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

# Note on ordering in normalize_message():
# _TICKER_RE runs FIRST so the <TS> / <PATH> / <HEX> placeholders introduced
# by later patterns are never re-processed by the ticker substitution.
# The path-before-numbers ordering (per the comment below) is preserved
# because _TICKER_RE only matches pure [A-Z]{2,5} tokens, not path segments
# or numeric strings.

_PATH_RE = re.compile(r"(?:/[A-Za-z0-9_.\-]+)+")
_ISO_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+\-]\d{2}:?\d{2}|Z)?"
)
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_TICKER_RE = re.compile(r"\b[A-Z]{2,5}\b")
_NUM_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_HEX_RE = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
_BRACKET_RE = re.compile(r"\[[^\]]+\]")

# Tokens we never normalise (common English ALL_CAPS that are not stock tickers)
_TICKER_KEEP = {
    "TODO", "FIXME", "WARN", "INFO", "ERROR", "DEBUG", "HTTP", "HTTPS", "JSON",
    "API", "URL", "SQL", "CPU", "RAM", "RTH", "UTC", "AEST", "ET", "NULL",
    "TRUE", "FALSE", "AND", "OR", "NOT", "IN", "IS", "ON", "OFF", "ALL",
    "NEW", "OLD", "SET", "GET", "PUT", "WARNING", "CRITICAL",
}


def normalize_message(msg: str) -> str:
    """Strip variable parts from an error message for fingerprinting.

    Ordering rationale:
    1. _TICKER_RE first — avoids re-processing the <TS>/<PATH>/<HEX>
       placeholders that subsequent steps introduce.
    2. _ISO_TS_RE before _DATE_RE — a full ISO timestamp absorbs the
       embedded date so _DATE_RE doesn't double-replace it.
    3. _PATH_RE before _NUM_RE — avoids "/tmp/run-1234" becoming
       "/tmp/run-<N>" before the whole path is collapsed to "<PATH>".
    """
    if not msg:
        return ""

    # Step 1: ticker substitution on raw message text
    def _sub_ticker(m: re.Match) -> str:
        tok = m.group(0)
        return "<TICKER>" if tok not in _TICKER_KEEP else tok

    out = _TICKER_RE.sub(_sub_ticker, msg)

    # Steps 2-7: structural normalisation (order within this group matters)
    out = _ISO_TS_RE.sub("<TS>", out)        # full timestamps first
    out = _DATE_RE.sub("<DATE>", out)         # bare dates next
    out = _PATH_RE.sub("<PATH>", out)         # paths before bare numbers
    out = _BRACKET_RE.sub("[<...>]", out)     # bracket content
    out = _HEX_RE.sub("<HEX>", out)           # hex strings
    out = _NUM_RE.sub("<N>", out)             # remaining numbers

    return out.strip()


def compute_fingerprint(
    exc_type: Optional[str],
    message: str | None,
    file_path: Optional[str] = None,
    line_number: Optional[int] = None,
) -> str:
    """Return sha256(exc_type + normalize(message) + file:line)[:16].

    All four inputs are normalised so semantically-identical errors that differ
    only in tickers, timestamps, numbers, or paths produce the same fingerprint.
    """
    parts = [
        exc_type or "",
        normalize_message(message or ""),
        file_path or "",
        ":" + str(line_number) if line_number else "",
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
