"""Multi-tenant profile management for Hawkeye.

Profiles are stored in ``~/.hawkeye/profiles.json``.  The active profile name
is tracked in ``~/.hawkeye/active_profile``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from core.models import HarnessProfile

logger = logging.getLogger(__name__)

_HAWKEYE_DIR = Path.home() / ".hawkeye"
_PROFILES_FILE = _HAWKEYE_DIR / "profiles.json"
_ACTIVE_FILE = _HAWKEYE_DIR / "active_profile"
_LOG_DIR = _HAWKEYE_DIR / "logs"


def _ensure_dirs() -> None:
    """Create ``~/.hawkeye/`` and ``~/.hawkeye/logs/`` on first use."""
    _HAWKEYE_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def _read_profiles() -> dict[str, dict]:  # type: ignore[type-arg]
    """Load the profiles mapping from disk."""
    if not _PROFILES_FILE.exists():
        return {}
    text = _PROFILES_FILE.read_text(encoding="utf-8")
    return json.loads(text) if text.strip() else {}


def _write_profiles(profiles: dict[str, dict]) -> None:  # type: ignore[type-arg]
    """Persist the profiles mapping to disk."""
    _ensure_dirs()
    _PROFILES_FILE.write_text(
        json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def create_profile(
    name: str,
    account_id: str,
    org_identifier: str,
    project_identifier: str,
    base_url: str = "https://app.harness.io",
) -> HarnessProfile:
    """Create and persist a new profile.  Raises ``ValueError`` on duplicate."""
    profiles = _read_profiles()
    if name in profiles:
        raise ValueError(f"Profile '{name}' already exists")

    profile = HarnessProfile(
        name=name,
        account_id=account_id,
        org_identifier=org_identifier,
        project_identifier=project_identifier,
        base_url=base_url,
        is_active=False,
    )
    profiles[name] = profile.model_dump()
    _write_profiles(profiles)
    logger.info("Created profile '%s'", name)
    return profile


def get_profile(name: str) -> HarnessProfile | None:
    """Return a single profile by name, or ``None``."""
    profiles = _read_profiles()
    data = profiles.get(name)
    if data is None:
        return None
    return HarnessProfile(**data)


def list_profiles() -> list[HarnessProfile]:
    """Return all stored profiles."""
    profiles = _read_profiles()
    active = _get_active_name()
    result: list[HarnessProfile] = []
    for data in profiles.values():
        p = HarnessProfile(**data)
        p.is_active = p.name == active
        result.append(p)
    return result


def set_active_profile(name: str) -> None:
    """Mark *name* as the active profile.  Raises ``ValueError`` if missing."""
    profiles = _read_profiles()
    if name not in profiles:
        raise ValueError(f"Profile '{name}' does not exist")
    _ensure_dirs()
    _ACTIVE_FILE.write_text(name, encoding="utf-8")
    logger.info("Active profile set to '%s'", name)


def get_active_profile() -> HarnessProfile | None:
    """Return the currently active profile, or ``None``."""
    name = _get_active_name()
    if name is None:
        return None
    return get_profile(name)


def delete_profile(name: str) -> None:
    """Delete a profile.  Clears active if it was the deleted one."""
    profiles = _read_profiles()
    if name not in profiles:
        raise ValueError(f"Profile '{name}' does not exist")
    del profiles[name]
    _write_profiles(profiles)
    if _get_active_name() == name:
        _clear_active()
    logger.info("Deleted profile '%s'", name)


# ------------------------------------------------------------------
# Internals
# ------------------------------------------------------------------


def _get_active_name() -> str | None:
    if not _ACTIVE_FILE.exists():
        return None
    text = _ACTIVE_FILE.read_text(encoding="utf-8").strip()
    return text or None


def _clear_active() -> None:
    if _ACTIVE_FILE.exists():
        _ACTIVE_FILE.unlink()
