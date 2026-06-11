"""atlas.kernel.paths — single source of truth for project-root resolution.

Production root is /root/atlas (Linux VPS). Dev/test environments override via
the ATLAS_PROJECT_ROOT env var, or fall back to the repo this package lives in.

data/ and config/ under PROJECT_ROOT are runtime STATE shared with Crucible
(target.json handoff, live_strategies.json registry, sharadar cache) — their
disk layout is a cross-repo contract; never derive them any other way.
"""
import os
from pathlib import Path

_DEFAULT_PROD_ROOT = Path("/root/atlas")
_ENV_OVERRIDE = os.environ.get("ATLAS_PROJECT_ROOT")

if _ENV_OVERRIDE:
    PROJECT_ROOT = Path(_ENV_OVERRIDE)
elif _DEFAULT_PROD_ROOT.exists():
    PROJECT_ROOT = _DEFAULT_PROD_ROOT
else:
    # atlas/kernel/paths.py -> atlas/kernel -> atlas -> repo root
    PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = PROJECT_ROOT / "data"
CONFIG_DIR = PROJECT_ROOT / "config"
LIVE_DATA_DIR = DATA_DIR / "live"
