"""Atlas Research Discovery Package.

Exports:
    discover_daily  — run today's paper → strategy pipeline
    discover_full   — run full sweep across all sources
    DailyReport     — result dataclass
"""

from research.discovery.discovery import discover_daily, discover_full, DailyReport

__all__ = ["discover_daily", "discover_full", "DailyReport"]
