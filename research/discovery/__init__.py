"""Atlas Research Discovery Package.

Exports:
    discover_daily       — run today's paper → strategy pipeline
    discover_full        — run full sweep across all sources
    DailyReport          — result dataclass
    STRATEGY_UNIVERSE    — master dict of all strategies (from legacy discovery.py)
    queue_discovery_batch — generate new experiments for the queue
"""

from research.discovery.discovery import discover_daily, discover_full, DailyReport

# The standalone research/discovery.py is shadowed by this package directory.
# Load it explicitly so director_cron.py and other consumers can do:
#   from research.discovery import STRATEGY_UNIVERSE, queue_discovery_batch
import importlib.util as _iu
from pathlib import Path as _Path

_standalone_path = _Path(__file__).resolve().parent.parent / "discovery.py"
if _standalone_path.exists():
    try:
        _spec = _iu.spec_from_file_location("research._discovery_legacy", str(_standalone_path))
        _mod = _iu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        STRATEGY_UNIVERSE = _mod.STRATEGY_UNIVERSE
        queue_discovery_batch = _mod.queue_discovery_batch
    except Exception as _exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "Failed to load STRATEGY_UNIVERSE from standalone discovery.py: %s", _exc
        )
        STRATEGY_UNIVERSE = {}
        def queue_discovery_batch(max_count: int = 5) -> int:
            return 0
else:
    # Fallback: empty dict if standalone module is missing
    STRATEGY_UNIVERSE = {}
    def queue_discovery_batch(max_count: int = 5) -> int:
        return 0

__all__ = [
    "discover_daily", "discover_full", "DailyReport",
    "STRATEGY_UNIVERSE", "queue_discovery_batch",
]
