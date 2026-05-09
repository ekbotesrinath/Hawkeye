"""Audit logging for Hawkeye tool invocations.

Logs every tool call to ``~/.hawkeye/logs/audit.log`` in JSON-Lines format.
Rotates at 10 MB, keeps 5 backups.  Secrets are masked before writing.
"""

from __future__ import annotations

import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from core.sanitizer import mask_secrets

_HAWKEYE_DIR = Path.home() / ".hawkeye"
_LOG_DIR = _HAWKEYE_DIR / "logs"
_AUDIT_LOG = _LOG_DIR / "audit.log"

_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 5

_logger: logging.Logger | None = None


def _ensure_logger() -> logging.Logger:
    """Lazily initialise the rotating audit logger."""
    global _logger  # noqa: PLW0603
    if _logger is not None:
        return _logger

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    _logger = logging.getLogger("hawkeye.audit")
    _logger.setLevel(logging.INFO)
    _logger.propagate = False

    handler = RotatingFileHandler(
        str(_AUDIT_LOG),
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    return _logger


def log_tool_call(
    tool_name: str,
    parameters: dict[str, Any],
    status: str,
    duration_ms: float,
) -> None:
    """Write a single audit entry.  Parameters are secret-masked."""
    logger = _ensure_logger()
    masked_params = json.loads(mask_secrets(json.dumps(parameters, default=str)))
    entry = {
        "ts": time.time(),
        "tool": tool_name,
        "params": masked_params,
        "status": status,
        "duration_ms": round(duration_ms, 2),
    }
    logger.info(json.dumps(entry, separators=(",", ":")))
