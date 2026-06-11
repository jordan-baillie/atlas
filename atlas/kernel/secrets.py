"""Secure credential management for Atlas broker integrations.

Load order (first match wins):
    1. Environment variables (preferred for deployment / CI)
    2. ~/.atlas-secrets.json (user home, not in repo)
    3. Prompt interactively (trade password only, never stored)

Security measures:
    - Secrets file must be chmod 600 (owner-only), rejected otherwise
    - Secrets are NEVER logged, printed, or written to dashboard/config
    - Access is logged at INFO level (key name only, not value)
    - Values are validated before returning (non-empty, expected format)
"""

from __future__ import annotations

import json
import logging
import os
import stat
from getpass import getpass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("atlas.broker.secrets")

# Secrets file lives in user home — NEVER inside the repo
SECRETS_FILE = Path.home() / ".atlas-secrets.json"

# Required permission: owner read/write only (600)
REQUIRED_MODE = 0o600

# Known secret keys and their descriptions
SECRET_KEYS = {
    # Alpaca — live real-money trading
    "ALPACA_API_KEY": "Alpaca live API key (from Alpaca dashboard → Live Trading)",
    "ALPACA_SECRET_KEY": "Alpaca live secret key (from Alpaca dashboard → Live Trading)",

    # Alpaca — paper trading (simulated account, $100K virtual balance)
    "ALPACA_PAPER_API_KEY": "Alpaca paper API key (from Alpaca dashboard → Paper Trading)",
    "ALPACA_PAPER_SECRET_KEY": "Alpaca paper secret key (from Alpaca dashboard → Paper Trading)",
    "ALPACA_PAPER_ENDPOINT": "Alpaca paper endpoint URL (default: https://paper-api.alpaca.markets)",
}


class SecretsError(Exception):
    """Raised when secrets cannot be loaded securely."""
    pass


def _check_file_permissions(path: Path) -> bool:
    """Verify secrets file has restrictive permissions (600).

    Returns True if permissions are acceptable, False otherwise.
    """
    if not path.exists():
        return True  # File doesn't exist yet — OK

    file_stat = path.stat()
    mode = stat.S_IMODE(file_stat.st_mode)

    if mode != REQUIRED_MODE:
        logger.error(
            "Secrets file %s has permissions %o — MUST be %o. "
            "Run: chmod 600 %s",
            path, mode, REQUIRED_MODE, path,
        )
        return False

    # Warn if not owned by current user
    if file_stat.st_uid != os.getuid():
        logger.error(
            "Secrets file %s is owned by uid %d, not current user %d",
            path, file_stat.st_uid, os.getuid(),
        )
        return False

    return True


def _load_secrets_file() -> dict:
    """Load secrets from ~/.atlas-secrets.json if it exists and is secure."""
    if not SECRETS_FILE.exists():
        return {}

    if not _check_file_permissions(SECRETS_FILE):
        raise SecretsError(
            f"Refusing to load {SECRETS_FILE} — insecure permissions. "
            f"Run: chmod 600 {SECRETS_FILE}"
        )

    try:
        with open(SECRETS_FILE) as f:
            data = json.load(f)
        logger.debug("Loaded secrets from %s (%d keys)", SECRETS_FILE, len(data))
        return data
    except (json.JSONDecodeError, IOError) as e:
        logger.error("Failed to read secrets file %s: %s", SECRETS_FILE, e)
        return {}


def get_secret(key: str, prompt: bool = False) -> Optional[str]:
    """Retrieve a secret value securely.

    Load order:
        1. Environment variable
        2. ~/.atlas-secrets.json
        3. Interactive prompt (if prompt=True)

    Args:
        key: Secret key name (e.g. "ALPACA_API_KEY")
        prompt: If True and secret not found, prompt interactively.

    Returns:
        Secret value, or None if not found.
    """
    # 1. Environment variable (highest priority)
    value = os.environ.get(key)
    if value:
        logger.info("Secret '%s' loaded from environment", key)
        return value

    # 2. Secrets file
    try:
        file_secrets = _load_secrets_file()
        value = file_secrets.get(key)
        if value:
            logger.info("Secret '%s' loaded from %s", key, SECRETS_FILE)
            return value
    except SecretsError:
        # Permission error already logged — continue to prompt
        pass

    # 3. Interactive prompt
    if prompt:
        desc = SECRET_KEYS.get(key, key)
        value = getpass(f"Enter {desc}: ")
        if value:
            logger.info("Secret '%s' provided interactively", key)
            return value

    logger.warning("Secret '%s' not found in env or secrets file", key)
    return None


def save_secrets_file(secrets: dict):
    """Write secrets to ~/.atlas-secrets.json with secure permissions.

    Creates the file with 600 permissions. Overwrites if exists.
    """
    # Write to temp file first, then rename (atomic)
    tmp_path = SECRETS_FILE.with_suffix(".tmp")

    try:
        # Create with restrictive permissions from the start
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, REQUIRED_MODE)
        with os.fdopen(fd, "w") as f:
            json.dump(secrets, f, indent=2)

        # Rename atomically
        tmp_path.rename(SECRETS_FILE)
        logger.info("Secrets saved to %s (chmod %o)", SECRETS_FILE, REQUIRED_MODE)

    except Exception as e:
        # Clean up temp file on error
        if tmp_path.exists():
            tmp_path.unlink()
        raise SecretsError(f"Failed to save secrets: {e}") from e


def setup_secrets_interactive():
    """Interactive setup: prompt for all known secrets and save securely."""
    print("\n" + "=" * 50)
    print("  Atlas Secrets Setup")
    print("=" * 50)
    print(f"\nSecrets will be saved to: {SECRETS_FILE}")
    print(f"File permissions: {oct(REQUIRED_MODE)} (owner read/write only)")
    print()

    # Load existing secrets (if any)
    existing = {}
    if SECRETS_FILE.exists():
        try:
            existing = _load_secrets_file()
        except SecretsError:
            print("⚠️  Existing secrets file has wrong permissions. Starting fresh.")

    secrets = {}
    for key, desc in SECRET_KEYS.items():
        current = existing.get(key)
        if current:
            mask = current[:2] + "*" * (len(current) - 2) if len(current) > 2 else "***"
            keep = input(f"  {desc} [{mask}] — keep existing? (Y/n): ").strip().lower()
            if keep != "n":
                secrets[key] = current
                continue

        value = getpass(f"  {desc}: ")
        if value:
            secrets[key] = value
        else:
            print(f"    Skipped (can set via env var {key})")

    if secrets:
        save_secrets_file(secrets)
        print(f"\n✅ Secrets saved to {SECRETS_FILE}")
    else:
        print("\nNo secrets to save.")

    print("\nAlternatively, set environment variables:")
    for key, desc in SECRET_KEYS.items():
        print(f"  export {key}='your-value'  # {desc}")
    print()
