"""
Atlas Configuration Management
===================================
Load, save, and version-manage JSON configuration files.
Supports per-market configurations (asx, sp500, etc.).

Usage:
    from atlas.kernel.config import load_config, get_active_config, save_config_version, list_versions

    # Default (sp500) — canonical
    cfg = get_active_config()

    # Specific market
    cfg = get_active_config("sp500")

    # Research / backtest — bypass override layer
    cfg = get_raw_config("sp500")
    # or equivalently:
    cfg = get_active_config("sp500", apply_overrides=False)
"""
from __future__ import annotations

import copy
import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from atlas.kernel.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

# Project root - all paths relative to this

CONFIG_DIR = PROJECT_ROOT / "config"
ACTIVE_DIR = CONFIG_DIR / "active"
VERSIONS_DIR = CONFIG_DIR / "versions"

# Default market when none specified
DEFAULT_MARKET = "sp500"

# ── In-process cache ──────────────────────────────────────────────────────────
# TTL 5 seconds. Key = (market_id_norm, apply_overrides_bool).
# Invalidated on TTL expiry only. Write APIs call clear_config_cache() after
# every successful write — provides ~immediate consistency for dashboard UX.

_CACHE_TTL_SECONDS: float = 5.0
_config_cache: dict = {}  # {(market_id, apply_overrides): (loaded_dict, expiry_ts)}


def _cache_get(key: tuple) -> Optional[Dict[str, Any]]:
    """Return cached config or None if missing/expired."""
    entry = _config_cache.get(key)
    if entry is None:
        return None
    cfg, expiry = entry
    if time.monotonic() > expiry:
        _config_cache.pop(key, None)
        return None
    return cfg


def _cache_put(key: tuple, cfg: Dict[str, Any]) -> None:
    """Insert or replace a cache entry with TTL."""
    _config_cache[key] = (cfg, time.monotonic() + _CACHE_TTL_SECONDS)


def clear_config_cache() -> None:
    """Clear the in-process config cache.

    Called by API write handlers after every successful override write and
    by tests that need immediate re-reads without TTL wait.
    """
    _config_cache.clear()


# ── Override query helpers ─────────────────────────────────────────────────────

def _query_active_override(scope: str, key: str) -> Optional[Dict[str, Any]]:
    """Query DB for active, non-expired override. Returns row dict or None.

    Lazy expiry: a row past its expires_at is treated as inactive but the
    active=1 flag is NOT mutated here (that's the expiry sweep's job, deferred).

    NEVER raises — any DB error falls back to raw config (safe for live trading).
    """
    try:
        from atlas.db import get_db
    except ImportError:
        return None
    try:
        with get_db() as db:
            row = db.execute(
                """SELECT id, scope, key, state, reason, created_by, created_at,
                          expires_at, prev_state, active
                   FROM config_overrides
                   WHERE scope=? AND key=? AND active=1
                     AND (expires_at IS NULL OR expires_at > datetime('now'))
                   LIMIT 1""",
                (scope, key),
            ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        # NEVER fail the read path on override DB errors — fall back to raw config.
        logger.warning(
            "Override DB query failed for %s/%s: %s — falling back to raw config",
            scope, key, e,
        )
        return None


def resolve_universe_state(market_id: str, raw_config: Dict[str, Any]) -> Tuple[str, bool]:
    """Returns (effective_mode, effective_live_enabled) after applying any active universe override.

    Three-state model:
      'live'     → mode='live',    live_enabled=True
      'passive'  → mode='passive', live_enabled=True
      'disabled' → mode='passive', live_enabled=False  (full kill: broker skips this market)
    """
    override = _query_active_override("universe", market_id)
    if override is None:
        return (
            raw_config.get("trading", {}).get("mode", "live"),
            bool(raw_config.get("trading", {}).get("live_enabled", False)),
        )
    state = override["state"]
    if state == "live":
        return "live", True
    if state == "passive":
        return "passive", True
    if state == "disabled":
        return "passive", False
    raise ValueError(f"unknown override state: {state!r}")


def resolve_strategy_enabled(market_id: str, strategy: str, raw_config: Dict[str, Any]) -> bool:
    """Returns effective enabled flag after applying any active strategy override."""
    raw_enabled = bool(
        raw_config.get("strategies", {}).get(strategy, {}).get("enabled", False)
    )
    override = _query_active_override("strategy", f"{market_id}.{strategy}")
    if override is None:
        return raw_enabled
    return override["state"] == "enabled"


def _apply_overrides(raw: Dict[str, Any], market_id: str) -> Dict[str, Any]:
    """Layer DB overrides on top of raw JSON config. Returns deep-copied modified config.

    Only sets _overrides_applied=True when something actually changed — clean signal
    for callers that want to distinguish "override was in play" from "table was empty".
    """
    cfg = copy.deepcopy(raw)
    mode, live_enabled = resolve_universe_state(market_id, raw)
    raw_mode = raw.get("trading", {}).get("mode")
    raw_le = raw.get("trading", {}).get("live_enabled")
    changed = False
    if mode != raw_mode or live_enabled != raw_le:
        cfg.setdefault("trading", {})
        cfg["trading"]["mode"] = mode
        cfg["trading"]["live_enabled"] = live_enabled
        changed = True
    for strat_name in list(cfg.get("strategies", {}).keys()):
        eff = resolve_strategy_enabled(market_id, strat_name, raw)
        raw_eff = bool(raw.get("strategies", {}).get(strat_name, {}).get("enabled", False))
        if eff != raw_eff:
            cfg["strategies"][strat_name]["enabled"] = eff
            changed = True
    if changed:
        cfg["_overrides_applied"] = True
    return cfg


# ── Core path helpers ─────────────────────────────────────────────────────────

def _active_config_path(market_id: Optional[str] = None) -> Path:
    """Return the active config path for a market."""
    market_id = (market_id or DEFAULT_MARKET).lower().strip()
    return ACTIVE_DIR / f"{market_id}.json"


# ── Public loaders ────────────────────────────────────────────────────────────

def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load a JSON configuration file (raw, no override layer).

    Args:
        path: Path to config file. Defaults to active sp500 config.

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

    logger.info(
        "Loaded config from %s (version: %s)",
        config_path, config.get("version", "unknown"),
    )
    return config


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


def get_active_config(
    market_id: Optional[str] = None,
    apply_overrides: bool = True,
) -> Dict[str, Any]:
    """Load the currently active configuration for a market.

    Args:
        market_id: Market identifier (e.g., 'asx', 'sp500'). Defaults to 'sp500'.
        apply_overrides: If True (default), layer DB overrides from config_overrides
                         table on top of raw JSON. Research/backtest code should pass
                         False to get the raw "what we'd do absent overrides" baseline.
                         See get_raw_config() for an explicit alias.

                         Convention: research/backtest/sweep code should use
                         ``apply_overrides=False`` or ``get_raw_config()`` once
                         overrides are in production use. In Phase 1 the override
                         table is empty so both paths are byte-identical.

    Returns:
        Dictionary containing the effective configuration.
    """
    market_id_norm = (market_id or DEFAULT_MARKET).lower().strip()
    cache_key = (market_id_norm, apply_overrides)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    raw = load_config(_active_config_path(market_id_norm))
    validate_config(raw)
    if not apply_overrides:
        _cache_put(cache_key, raw)
        return raw
    effective = _apply_overrides(raw, market_id_norm)
    _cache_put(cache_key, effective)
    return effective


def get_raw_config(market_id: Optional[str] = None) -> Dict[str, Any]:
    """Explicit alias for get_active_config(market_id, apply_overrides=False).

    Use in research/backtest paths where you want the raw JSON config baseline,
    independent of any operator overrides. This is the correct call for any
    code that compares "what we'd do if enabled" against "what we're doing".
    """
    return get_active_config(market_id, apply_overrides=False)


# ── Versioning ────────────────────────────────────────────────────────────────

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

    logger.info("Saved config to %s", config_path)
    return config_path


def save_config_version(
    config: Dict[str, Any],
    version: Optional[str] = None,
    market_id: Optional[str] = None,
) -> Path:
    """Save a new versioned copy of the configuration.

    Creates a versioned backup and updates the active config for the market.

    Args:
        config: Configuration dictionary to save.
        version: Version string (e.g., 'v1.1'). If None, auto-increments.
        market_id: Market identifier. If None, reads from config or defaults to 'sp500'.

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

    logger.info("Created config version %s for %s at %s", version, market_id, version_path)
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
                logger.warning("Could not read version file %s: %s", f, e)

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
            logger.warning("Could not read version file %s: %s", f, e)

    return versions


def _get_current_version(market_id: Optional[str] = None) -> str:
    """Get the version string from the active config for a market."""
    try:
        config = get_raw_config(market_id)
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
    from atlas.kernel.logging_config import setup_logging
    setup_logging("config_test", telegram_errors=False)
    print("=== Config Module Self-Test ===")

    cfg = get_active_config()
    print(f"Active config version: {cfg['version']}")
    print(f"Project: {cfg.get('project', 'Atlas')}")
    print(f"Market: {cfg.get('market', 'sp500')}")
    print(f"Universe top_n: {cfg['universe']['top_n']}")
    print(f"Risk per trade: {cfg['risk']['max_risk_per_trade_pct']}")
    print(f"Overrides applied: {cfg.get('_overrides_applied', False)}")

    raw = get_raw_config()
    print(f"\nRaw config version: {raw['version']}")
    print(f"apply_overrides=False bypasses DB: {raw.get('_overrides_applied', False) is False}")

    versions = list_versions()
    print(f"\nSaved versions ({len(versions)}):")
    for v in versions:
        print(f"  [{v.get('market', '?')}] {v['version']} - {v['path']}")

    print("\n=== Config Module OK ===")
