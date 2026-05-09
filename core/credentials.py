"""Keyring-based credential management for Hawkeye.

Stores Harness API tokens securely in the OS keychain via the ``keyring``
library.  Tokens are NEVER logged or printed.
"""

from __future__ import annotations

import logging

import keyring
from keyring.errors import KeyringError

logger = logging.getLogger(__name__)

SERVICE_NAME = "hawkeye"


def _key(profile_name: str) -> str:
    """Build the keyring key for a given profile."""
    return f"{profile_name}_api_token"


def store_token(profile_name: str, token: str) -> None:
    """Store an API token in the OS keychain for *profile_name*."""
    try:
        keyring.set_password(SERVICE_NAME, _key(profile_name), token)
        logger.info("Token stored for profile '%s'", profile_name)
    except KeyringError:
        logger.exception("Failed to store token for profile '%s'", profile_name)
        raise


def get_token(profile_name: str) -> str | None:
    """Retrieve the API token for *profile_name*, or ``None`` if absent."""
    try:
        return keyring.get_password(SERVICE_NAME, _key(profile_name))
    except KeyringError:
        logger.exception("Failed to retrieve token for profile '%s'", profile_name)
        raise


def delete_token(profile_name: str) -> None:
    """Delete the API token for *profile_name* from the keychain."""
    try:
        keyring.delete_password(SERVICE_NAME, _key(profile_name))
        logger.info("Token deleted for profile '%s'", profile_name)
    except KeyringError:
        logger.exception("Failed to delete token for profile '%s'", profile_name)
        raise


def has_token(profile_name: str) -> bool:
    """Return ``True`` if a token exists for *profile_name*."""
    try:
        return get_token(profile_name) is not None
    except KeyringError:
        return False
