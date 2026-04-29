"""Atlas auto-remediation triage classifier.

Deterministic, YAML-driven (NOT LLM). Reads a single error row from the
errors table and returns a classification + reason. The classifier is the
single most important safety bound in the system — every rule must default
to ESCALATE for unknown patterns.

Output classes:
  AUTO_FIX:               Whitelisted error class, eligible for autonomous fix (Phase 3+)
  ASSIST:                 Agent proposes branch; human merges (Phase 2+)
  ESCALATE:               Telegram alert; human acts (always)
  IGNORE:                 Suppress (known noise)
  ESCALATE_DEFERRED:      Defer until market close (during RTH on real failures)
  IGNORE_PENDING_CLEAR:   Don't alert while halted

Layered checks (order is load-bearing):
  1. NEVER list (file_globs / function_names / error_class / message patterns) → ESCALATE always
  2. Trading kill switch active → IGNORE_PENDING_CLEAR
  3. Market hours active → ESCALATE_DEFERRED for real failures (preserves capture)
  4. IGNORE_PATTERNS list (Circuit breaker / Execution blocked) → IGNORE
  5. Day-1 AUTO_FIX whitelist match → AUTO_FIX (only when phase_3_enabled=true)
  6. Permanent_assist paths → ASSIST
  7. Default → ESCALATE (never ASSIST or AUTO_FIX on unknown patterns)
"""
from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CFG_PATH = PROJECT_ROOT / "config" / "auto_remediation.yaml"
DEFAULT_DENY_PATH = PROJECT_ROOT / "config" / "auto_fix_deny.yaml"
DEFAULT_FUNCS_PATH = PROJECT_ROOT / "config" / "safety_critical_functions.txt"
HALT_FILES = (
    PROJECT_ROOT / "data" / "HALT",
    PROJECT_ROOT / ".live_halt",
    PROJECT_ROOT / "data" / "AUTO_REMEDIATION_HALT",
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TriageResult:
    classification: str       # 'AUTO_FIX'|'ASSIST'|'ESCALATE'|'IGNORE'|'ESCALATE_DEFERRED'|'IGNORE_PENDING_CLEAR'
    reason: str               # Human-readable rule that fired
    rule_id: str              # Stable rule identifier for audit log (e.g. 'never_fix.path:brokers/**')
    tier: int                 # 0=trading-path, 1=ASSIST, 2=AUTO_FIX-eligible, 99=unknown


class TriageClassifier:
    def __init__(
        self,
        config_path: Optional[Path] = None,
        deny_path: Optional[Path] = None,
        funcs_path: Optional[Path] = None,
    ):
        self.config_path = Path(config_path) if config_path else DEFAULT_CFG_PATH
        self.deny_path = Path(deny_path) if deny_path else DEFAULT_DENY_PATH
        self.funcs_path = Path(funcs_path) if funcs_path else DEFAULT_FUNCS_PATH
        self._load()

    def _load(self) -> None:
        with open(self.config_path) as f:
            self.cfg = yaml.safe_load(f) or {}
        with open(self.deny_path) as f:
            self.deny = yaml.safe_load(f) or {}

        # Resolve __ref__ for function names blocked list
        fn_blocked = self.deny.get("function_names_blocked") or {}
        if isinstance(fn_blocked, dict) and "__ref__" in fn_blocked:
            ref_path = PROJECT_ROOT / fn_blocked["__ref__"]
            with open(ref_path) as f:
                self._blocked_functions = {
                    ln.strip() for ln in f if ln.strip() and not ln.startswith("#")
                }
        elif self.funcs_path.exists():
            with open(self.funcs_path) as f:
                self._blocked_functions = {
                    ln.strip() for ln in f if ln.strip() and not ln.startswith("#")
                }
        else:
            self._blocked_functions = set()

        self._never_globs: list[str] = list(self.deny.get("file_globs") or [])
        self._never_exc_patterns: list[re.Pattern[str]] = [
            re.compile(p, re.IGNORECASE)
            for p in (self.deny.get("error_class_patterns") or [])
        ]
        self._never_msg_patterns: list[str] = [
            p.lower() for p in (self.deny.get("message_patterns") or [])
        ]
        self._ignore_patterns: list[str] = list(self.cfg.get("ignore_patterns") or [])
        self._whitelist_classes: list[str] = list(
            self.cfg.get("day1_auto_fix_whitelist") or []
        )
        self._permanent_assist_globs: list[str] = list(
            (self.cfg.get("permanent_assist") or {}).get("paths") or []
        )
        self._never_paths_user: list[str] = list(
            (self.cfg.get("never_fix") or {}).get("paths") or []
        )
        self._phase_3_enabled: bool = bool(
            (self.cfg.get("phase") or {}).get("phase_3_enabled")
        )

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _glob_match(path: str | None, glob: str) -> bool:
        if not path:
            return False
        # For globs with **, also check that the path starts with the prefix before **
        if "**" in glob:
            prefix = glob.split("**")[0].rstrip("/")
            if prefix and not path.startswith(prefix):
                return False
            return fnmatch.fnmatch(path, glob.replace("**", "*"))
        return fnmatch.fnmatch(path, glob)

    @staticmethod
    def is_halt_active(halt_paths: tuple[Path, ...] = HALT_FILES) -> bool:
        return any(p.exists() for p in halt_paths)

    @staticmethod
    def is_market_hours_now(now: Optional[datetime] = None) -> bool:
        """09:30–16:00 ET, Mon-Fri.

        Uses 13:30–21:00 UTC as a conservative approximation covering EDT.
        EST shifts this ±1h (13:30 UTC = 08:30 EST, still conservatively safe).
        """
        if now is None:
            now = datetime.now(timezone.utc)
        if now.weekday() >= 5:
            return False
        hh, mm = now.hour, now.minute
        start = time(13, 30)
        end = time(21, 0)
        return start <= time(hh, mm) <= end

    # ── Rule layers ────────────────────────────────────────────────────

    def _check_never_list(self, error: dict) -> Optional[TriageResult]:
        """Layer 1: NEVER list — highest precedence. Any match → ESCALATE."""
        fp = error.get("file_path") or ""

        # File-glob check (deny.yaml globs)
        for glob in self._never_globs:
            if self._glob_match(fp, glob):
                return TriageResult(
                    "ESCALATE",
                    f"NEVER list file glob match: {glob}",
                    f"never_fix.path:{glob}",
                    tier=0,
                )

        # User-defined never_fix paths from auto_remediation.yaml (belt-and-suspenders)
        for glob in self._never_paths_user:
            if self._glob_match(fp, glob):
                return TriageResult(
                    "ESCALATE",
                    f"NEVER list (user config) glob match: {glob}",
                    f"never_fix.user_path:{glob}",
                    tier=0,
                )

        # Error class pattern match
        exc_type = error.get("exc_type") or ""
        for pat in self._never_exc_patterns:
            if pat.search(exc_type):
                return TriageResult(
                    "ESCALATE",
                    f"NEVER list error class pattern: {pat.pattern}",
                    f"never_fix.exc:{pat.pattern}",
                    tier=0,
                )

        # Message substring match (case-insensitive — patterns already lowercased)
        msg = (error.get("message") or "").lower()
        for sub in self._never_msg_patterns:
            if sub in msg:
                return TriageResult(
                    "ESCALATE",
                    f"NEVER list message pattern: {sub!r}",
                    f"never_fix.msg:{sub}",
                    tier=0,
                )

        # Safety-critical function name
        fn = error.get("function_name") or ""
        if fn and fn in self._blocked_functions:
            return TriageResult(
                "ESCALATE",
                f"NEVER list safety-critical function: {fn}",
                f"never_fix.fn:{fn}",
                tier=0,
            )

        # Causal chain: traceback references a trading-path module
        tb = error.get("traceback") or ""
        for glob in self._never_globs:
            # Extract the non-wildcard prefix for a quick substring scan
            simple = glob.split("**")[0].rstrip("/").rstrip("*").rstrip("/")
            if simple and simple in tb:
                return TriageResult(
                    "ESCALATE",
                    f"Causal chain: traceback references {simple!r}",
                    f"never_fix.causal_chain:{simple}",
                    tier=0,
                )

        return None

    def _check_ignore_patterns(self, error: dict) -> Optional[TriageResult]:
        """Layer 4: Known-noise IGNORE patterns."""
        msg = error.get("message") or ""
        for pat in self._ignore_patterns:
            if pat in msg:
                return TriageResult(
                    "IGNORE",
                    f"Known-noise pattern: {pat!r}",
                    f"ignore_pattern:{pat}",
                    tier=2,
                )
        return None

    def _check_permanent_assist(self, error: dict) -> Optional[TriageResult]:
        """Layer 6: Permanent-ASSIST paths — human must merge."""
        fp = error.get("file_path") or ""
        for glob in self._permanent_assist_globs:
            if self._glob_match(fp, glob):
                return TriageResult(
                    "ASSIST",
                    f"Permanent-ASSIST path: {glob}",
                    f"permanent_assist.path:{glob}",
                    tier=1,
                )
        return None

    def _check_auto_fix_whitelist(self, error: dict) -> Optional[TriageResult]:
        """Layer 5: AUTO_FIX whitelist — only active when phase_3_enabled=true.

        Phase 1 stub: returns None always (phase_3_enabled is false).
        Real whitelist matching ships in Phase 3.
        """
        if not self._phase_3_enabled:
            return None
        # Phase 3+ implementation placeholder
        return None

    # ── Public API ─────────────────────────────────────────────────────

    def classify(self, error: dict) -> TriageResult:
        """Classify a single error row from the errors table.

        Args:
            error: dict with keys matching errors table columns:
                   file_path, exc_type, message, traceback, function_name,
                   market_hours, halt_active, etc.

        Returns:
            TriageResult with classification, reason, rule_id, and tier.
        """
        # 1. NEVER list — absolute precedence; no other rule can override
        result = self._check_never_list(error)
        if result:
            return result

        # 2. Trading kill switch active → suppress all auto-remediation
        if self.is_halt_active():
            return TriageResult(
                "IGNORE_PENDING_CLEAR",
                "Trading kill switch active (HALT/AUTO_REMEDIATION_HALT/.live_halt)",
                "halt_active",
                tier=99,
            )

        # 3. Market hours — defer real failures; IGNORE patterns still apply first
        if self.is_market_hours_now():
            result = self._check_ignore_patterns(error)
            if result:
                return result
            return TriageResult(
                "ESCALATE_DEFERRED",
                "Market hours active — defer classification to off-hours",
                "market_hours_defer",
                tier=99,
            )

        # 4. IGNORE patterns (off-hours path)
        result = self._check_ignore_patterns(error)
        if result:
            return result

        # 5. AUTO_FIX whitelist (Phase 3 only — stub returns None in Phase 1)
        result = self._check_auto_fix_whitelist(error)
        if result:
            return result

        # 6. Permanent-ASSIST paths
        result = self._check_permanent_assist(error)
        if result:
            return result

        # 7. Default-deny: ESCALATE on all unknown patterns
        return TriageResult(
            "ESCALATE",
            "No rule matched — default ESCALATE for unknown error pattern",
            "default_deny",
            tier=99,
        )


# ── Module-level convenience ───────────────────────────────────────────────

_default_classifier: Optional[TriageClassifier] = None


def classify_error(error: dict) -> TriageResult:
    """Classify an error dict using the default (real-config) classifier.

    Thread-safety: CPython GIL protects the lazy-init assignment.
    """
    global _default_classifier
    if _default_classifier is None:
        _default_classifier = TriageClassifier()
    return _default_classifier.classify(error)


def reload_classifier() -> None:
    """Force reload of the YAML config (e.g. after live edits)."""
    global _default_classifier
    _default_classifier = None
