"""
Secure credential management.

Load order:
1. Environment variables (COINBASE_API_KEY, COINBASE_API_SECRET)
2. .env file (if python-dotenv available)
3. Secrets file (secrets/credentials.yaml)

NEVER logs credential values — only confirms source.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("Credentials")


@dataclass(frozen=True)
class Credentials:
    """Validated API credentials. Immutable."""

    api_key: str
    api_secret: str
    source: str  # "env", "dotenv", "secrets_file"

    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_secret)


def _load_from_env() -> Optional[Credentials]:
    key = os.environ.get("COINBASE_API_KEY") or os.environ.get("COINBASE_KEY_NAME", "")
    secret = os.environ.get("COINBASE_API_SECRET") or os.environ.get("COINBASE_KEY_SECRET", "")
    if key and secret:
        return Credentials(api_key=key, api_secret=secret, source="env")
    return None


def _load_from_dotenv() -> Optional[Credentials]:
    try:
        from dotenv import load_dotenv
        load_dotenv()
        return _load_from_env()
    except ImportError:
        return None


def _load_from_secrets_file(path: str = "secrets/credentials.yaml") -> Optional[Credentials]:
    p = Path(path)
    if not p.exists():
        return None
    try:
        import yaml
        with open(p) as f:
            data = yaml.safe_load(f) or {}
        key = data.get("api_key", data.get("COINBASE_API_KEY", ""))
        secret = data.get("api_secret", data.get("COINBASE_API_SECRET", ""))
        if key and secret:
            # Check file permissions (should be 600 on Unix)
            try:
                mode = oct(p.stat().st_mode)[-3:]
                if mode not in ("600", "400"):
                    logger.warning(
                        "Secrets file %s has permissive permissions (%s). "
                        "Recommend chmod 600.", path, mode,
                    )
            except (OSError, ValueError):
                pass
            return Credentials(api_key=key, api_secret=secret, source="secrets_file")
    except Exception as e:
        logger.warning("Failed to load secrets file %s: %s", path, e)
    return None


def _validate_format(creds: Credentials) -> bool:
    """Basic format validation — key and secret are non-empty strings."""
    if not creds.api_key or len(creds.api_key) < 5:
        logger.error("API key too short or empty")
        return False
    if not creds.api_secret or len(creds.api_secret) < 5:
        logger.error("API secret too short or empty")
        return False
    return True


def load_credentials(secrets_path: str = "secrets/credentials.yaml") -> Optional[Credentials]:
    """
    Load credentials from available sources in priority order.

    Returns None if no valid credentials found.
    NEVER logs credential values.
    """
    # 1. Environment variables
    creds = _load_from_env()
    if creds and creds.is_configured():
        if _validate_format(creds):
            logger.info("Credentials loaded from: environment variables")
            return creds

    # 2. .env file
    creds = _load_from_dotenv()
    if creds and creds.is_configured():
        if _validate_format(creds):
            logger.info("Credentials loaded from: .env file")
            return creds

    # 3. Secrets file
    creds = _load_from_secrets_file(secrets_path)
    if creds and creds.is_configured():
        if _validate_format(creds):
            logger.info("Credentials loaded from: secrets file (%s)", secrets_path)
            return creds

    logger.warning("No valid credentials found in any source")
    return None
