"""
Atlas-ASX Configuration Management
===================================
Load, save, and version-manage JSON configuration files.

Usage:
    from utils.config import load_config, get_active_config, save_config_version, list_versions
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
ACTIVE_CONFIG_PATH = CONFIG_DIR / "active_config.json"


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load a JSON configuration file.

    Args:
        path: Path to config file. Defaults to active_config.json.

    Returns:
        Dictionary containing the configuration.

    Raises:
        FileNotFoundError: If config file does not exist.
        json.JSONDecodeError: If config file is not valid JSON.
    """
    config_path = Path(path) if path else ACTIVE_CONFIG_PATH

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
        path: Destination path. Defaults to active_config.json.

    Returns:
        Path where config was saved.
    """
    config_path = Path(path) if path else ACTIVE_CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)

    with open(config_path, "w") as f:
        json.dump(config, f, indent=2, default=str)

    logger.info(f"Saved config to {config_path}")
    return config_path


def get_active_config() -> Dict[str, Any]:
    """Load the currently active configuration.

    Returns:
        Dictionary containing the active configuration.
    """
    return load_config(ACTIVE_CONFIG_PATH)


def save_config_version(config: Dict[str, Any], version: Optional[str] = None) -> Path:
    """Save a new versioned copy of the configuration.

    Creates a versioned backup (e.g., config_v1.1.json) and updates
    the active config to point to the new version.

    Args:
        config: Configuration dictionary to save.
        version: Version string (e.g., 'v1.1'). If None, auto-increments
                 the minor version from the current active config.

    Returns:
        Path to the new versioned config file.
    """
    if version is None:
        version = _next_version()

    config["version"] = version
    config["_version_metadata"] = {
        "created_at": datetime.now().isoformat(),
        "previous_version": _get_current_version(),
    }

    # Save versioned copy
    version_path = CONFIG_DIR / f"config_{version}.json"
    save_config(config, version_path)

    # Update active config
    save_config(config, ACTIVE_CONFIG_PATH)

    logger.info(f"Created config version {version} at {version_path}")
    return version_path


def list_versions() -> List[Dict[str, Any]]:
    """List all saved configuration versions.

    Returns:
        List of dicts with 'version', 'path', 'modified' for each version file.
    """
    versions = []
    if not CONFIG_DIR.exists():
        return versions

    for f in sorted(CONFIG_DIR.glob("config_v*.json")):
        try:
            with open(f, "r") as fh:
                cfg = json.load(fh)
            versions.append({
                "version": cfg.get("version", "unknown"),
                "path": str(f),
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                "description": cfg.get("description", ""),
            })
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read version file {f}: {e}")

    return versions


def _get_current_version() -> str:
    """Get the version string from the active config."""
    try:
        config = load_config(ACTIVE_CONFIG_PATH)
        return config.get("version", "v0.0")
    except (FileNotFoundError, json.JSONDecodeError):
        return "v0.0"


def _next_version() -> str:
    """Auto-increment the minor version number.

    Examples:
        v1.0 -> v1.1
        v2.3 -> v2.4
    """
    current = _get_current_version()
    try:
        # Strip 'v' prefix, split on '.'
        parts = current.lstrip("v").split(".")
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        return f"v{major}.{minor + 1}"
    except (ValueError, IndexError):
        return "v1.0"


if __name__ == "__main__":
    # Quick self-test
    logging.basicConfig(level=logging.INFO)
    print("=== Config Module Self-Test ===")

    cfg = get_active_config()
    print(f"Active config version: {cfg['version']}")
    print(f"Project: {cfg['project']}")
    print(f"Universe top_n: {cfg['universe']['top_n']}")
    print(f"Risk per trade: {cfg['risk']['max_risk_per_trade_pct']}")

    versions = list_versions()
    print(f"\nSaved versions ({len(versions)}):")
    for v in versions:
        print(f"  {v['version']} - {v['path']} (modified: {v['modified']})")

    print("\n=== Config Module OK ===")
