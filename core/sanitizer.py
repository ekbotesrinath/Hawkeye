"""Prompt-injection scrubbing and secret masking for Hawkeye.

Strips text that resembles prompt-injection patterns from API responses and
masks secrets (API tokens, connection strings, passwords) found in log content.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Prompt injection patterns
# ---------------------------------------------------------------------------
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"<\|im_start\|>.*?(<\|im_end\|>|$)", re.DOTALL | re.IGNORECASE),
    re.compile(r"<system>.*?(</system>|$)", re.DOTALL | re.IGNORECASE),
    re.compile(r"<\|system\|>.*?(<\|end\|>|$)", re.DOTALL | re.IGNORECASE),
    re.compile(
        r"(ignore|disregard|forget)\s+(all\s+)?(previous|prior|above)\s+(instructions|context|prompts?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(you\s+are\s+now|act\s+as|pretend\s+to\s+be|switch\s+to)\s+",
        re.IGNORECASE,
    ),
    re.compile(r"\[INST\].*?(\[/INST\]|$)", re.DOTALL | re.IGNORECASE),
    re.compile(r"###\s*(System|Human|Assistant)\s*:", re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# Secret patterns
# ---------------------------------------------------------------------------
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Azure / generic connection strings
    (
        re.compile(
            r"(DefaultEndpointsProtocol|AccountKey|SharedAccessSignature|"
            r"Server=tcp:|Data Source=|Password=|pwd=)[^\s;\"']{8,}",
            re.IGNORECASE,
        ),
        "***MASKED_CONNECTION_STRING***",
    ),
    # Bearer tokens
    (
        re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
        "Bearer ***MASKED_TOKEN***",
    ),
    # Generic API key / token patterns (hex or base64, >20 chars)
    (
        re.compile(
            r"(api[_-]?key|api[_-]?token|x-api-key|authorization)[\"':\s=]+[A-Za-z0-9\-._~+/]{20,}",
            re.IGNORECASE,
        ),
        "***MASKED_API_KEY***",
    ),
    # Base64-encoded long secrets (>=40 chars of base64)
    (
        re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"),
        "***MASKED_BASE64_SECRET***",
    ),
]


def sanitize(text: str) -> str:
    """Remove prompt-injection patterns and mask secrets in *text*."""
    result = strip_injections(text)
    result = mask_secrets(result)
    return result


def strip_injections(text: str) -> str:
    """Remove prompt-injection patterns from *text*."""
    result = text
    for pattern in _INJECTION_PATTERNS:
        result = pattern.sub("", result)
    return result


def mask_secrets(text: str) -> str:
    """Mask secrets found in *text*."""
    result = text
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result
