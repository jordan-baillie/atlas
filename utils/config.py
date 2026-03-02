"""
Atlas Configuration Management
===================================
Load, save, and version-manage JSON configuration files.
Supports per-market configurations (asx, sp500, etc.).

Usage:
    from utils.config import load_config, get_active_config, save_config_version, list_versions

    # Default (asx) — backward compatible
    cfg = get_active_config()

    # Specific market
    cfg = get_active_config("sp500")
"""

import json
import shutil
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Project root - all paths relative to this
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
ACTIVE_DIR = CONFIG_DIR / "active"
VERSIONS_DIR = CONFIG_DIR / "versions"

# Default market when none specified
DEFAULT_MARKET = "asx"


def _active_config_path(market_id: Optional[str] = None) -> Path:
    """Return the active config path for a market."""
    market_id = (market_id or DEFAULT_MARKET).lower().strip()
    return ACTIVE_DIR / f"{market_id}.json"


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load a JSON configuration file.

    Args:
        path: Path to config file. Defaults to active ASX config.

    Returns:
        Dictionary containing the configuration.

    Raises:
        FileNotFoundError: If config file does not exist.
        json.JSONDecodeError: If config file is not valid JSON.
    """
    config_path = Path(path) if path else _active_config_path()

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r") as f:
        config = json.load(f)

    logger.info(f"Loaded config from {config_path} (version: {config.get('version', 'unknown')})")
    return config


def save_config(config: Dict[str, Any], path: Optional[Path] = None) -> Path:
    """Save a configuration dictionary to a JSON file.

    Args:
        config: Configuration dictionary to save.
        path: Destination path. Defaults to active config for config's market.

    Returns:
        Path where config was saved.
    """
    if path is None:
        market_id = config.get("market", DEFAULT_MARKET)
        path = ACTIVE_DIR / f"{market_id}.json"

    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    with open(config_path, "w") as f:
        json.dump(config, f, indent=2, default=str)

    logger.info(f"Saved config to {config_path}")
    return config_path


def validate_config(config: Dict[str, Any]) -> None:
    """Audit M3: Validate required config fields exist."""
    required = {
        "risk.starting_equity": config.get("risk", {}).get("starting_equity"),
        "risk.max_open_positions": config.get("risk", {}).get("max_open_positions"),
        "fees": config.get("fees"),
        "strategies": config.get("strategies"),
    }
    missing = [k for k, v in required.items() if v is None]
    if missing:
        raise ValueError(f"Config missing required fields: {', '.join(missing)}")


def get_active_config(market_id: Optional[str] = None) -> Dict[str, Any]:
    """Load the currently active configuration for a market.

    Args:
        market_id: Market identifier (e.g., 'asx', 'sp500').
                   Defaults to 'asx' for backward compatibility.

    Returns:
        Dictionary containing the active configuration.
    """
    config = load_config(_active_config_path(market_id))
    validate_config(config)
    return config


def save_config_version(config: Dict[str, Any], version: Optional[str] = None,
                        market_id: Optional[str] = None) -> Path:
    """Save a new versioned copy of the configuration.

    Creates a versioned backup and updates the active config for the market.

    Args:
        config: Configuration dictionary to save.
        version: Version string (e.g., 'v1.1'). If None, auto-increments.
        market_id: Market identifier. If None, reads from config or defaults to 'asx'.

    Returns:
        Path to the new versioned config file.
    """
    market_id = market_id or config.get("market", DEFAULT_MARKET)
    config["market"] = market_id

    if version is None:
        version = _next_version(market_id)

    config["version"] = version
    config["_version_metadata"] = {
        "created_at": datetime.now().isoformat(),
        "previous_version": _get_current_version(market_id),
        "market": market_id,
    }

    # Save versioned copy
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    version_path = VERSIONS_DIR / f"{market_id}_{version}.json"
    save_config(config, version_path)

    # Update active config for this market
    active_path = ACTIVE_DIR / f"{market_id}.json"
    save_config(config, active_path)

    logger.info(f"Created config version {version} for {market_id} at {version_path}")
    return version_path


def list_versions(market_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all saved configuration versions, optionally filtered by market.

    Args:
        market_id: If provided, only list versions for this market.

    Returns:
        List of dicts with 'version', 'path', 'modified', 'market' for each version.
    """
    versions = []

    # Check versions directory
    if VERSIONS_DIR.exists():
        for f in sorted(VERSIONS_DIR.glob("*.json")):
            try:
                with open(f, "r") as fh:
                    cfg = json.load(fh)
                mkt = cfg.get("market", DEFAULT_MARKET)
                if market_id and mkt != market_id:
                    continue
                versions.append({
                    "version": cfg.get("version", "unknown"),
                    "market": mkt,
                    "path": str(f),
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                    "description": cfg.get("description", ""),
                })
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Could not read version file {f}: {e}")

    # Also check legacy config_v*.json in CONFIG_DIR
    for f in sorted(CONFIG_DIR.glob("config_v*.json")):
        try:
            with open(f, "r") as fh:
                cfg = json.load(fh)
            mkt = cfg.get("market", DEFAULT_MARKET)
            if market_id and mkt != market_id:
                continue
            versions.append({
                "version": cfg.get("version", "unknown"),
                "market": mkt,
                "path": str(f),
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                "description": cfg.get("description", ""),
            })
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read version file {f}: {e}")

    return versions


def _get_current_version(market_id: Optional[str] = None) -> str:
    """Get the version string from the active config for a market."""
    try:
        config = get_active_config(market_id)
        return config.get("version", "v0.0")
    except (FileNotFoundError, json.JSONDecodeError):
        return "v0.0"


def _next_version(market_id: Optional[str] = None) -> str:
    """Auto-increment the minor version number.

    Examples:
        v1.0 -> v1.1
        v2.3 -> v2.4
    """
    current = _get_current_version(market_id)
    try:
        # Strip 'v' prefix and any suffix after underscore
        ver = current.lstrip("v").split("_")[0]
        parts = ver.split(".")
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        return f"v{major}.{minor + 1}"
    except (ValueError, IndexError):
        return "v1.0"


# ---------------------------------------------------------------------------
# Backward compatibility aliases
# ---------------------------------------------------------------------------

ACTIVE_CONFIG_PATH = ACTIVE_DIR / f"{DEFAULT_MARKET}.json"


if __name__ == "__main__":
    # Quick self-test
    from utils.logging_config import setup_logging
    setup_logging("config_test", telegram_errors=False)
    print("=== Config Module Self-Test ===")

    cfg = get_active_config()
    print(f"Active config version: {cfg['version']}")
    print(f"Project: {cfg.get('project', 'Atlas')}")
    print(f"Market: {cfg.get('market', 'asx')}")
    print(f"Universe top_n: {cfg['universe']['top_n']}")
    print(f"Risk per trade: {cfg['risk']['max_risk_per_trade_pct']}")

    versions = list_versions()
    print(f"\nSaved versions ({len(versions)}):")
    for v in versions:
        print(f"  [{v.get('market', '?')}] {v['version']} - {v['path']}")

    print("\n=== Config Module OK ===")
