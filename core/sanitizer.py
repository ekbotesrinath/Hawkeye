"""Prompt-injection scrubbing and secret masking for Hawkeye.

Replaces text that resembles prompt-injection patterns with a visible
marker (silent removal would falsify log evidence) and masks secrets
(API tokens, connection strings, passwords) found in log content.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# Visible marker left in place of removed injection content, so both the
# model and the human reading the report know the text was altered.
INJECTION_MARKER = "[hawkeye:removed-suspected-prompt-injection]"

# ---------------------------------------------------------------------------
# Prompt injection patterns
# ---------------------------------------------------------------------------
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"<\|im_start\|>.*?(<\|im_end\|>|$)", re.DOTALL | re.IGNORECASE),
    re.compile(r"<system>.*?(</system>|$)", re.DOTALL | re.IGNORECASE),
    re.compile(r"<\|system\|>.*?(<\|end\|>|$)", re.DOTALL | re.IGNORECASE),
    re.compile(
        r"(ignore|disregard|forget)\s+(all\s+)?(previous|prior|above)\s+"
        r"(instructions|context|prompts?)",
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
# Ordered: specific, well-known token formats first; the generic
# high-entropy fallback runs last.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]] = [
    # Private key blocks (PEM)
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)",
            re.DOTALL,
        ),
        "***MASKED_PRIVATE_KEY***",
    ),
    # Bearer tokens (covers JWTs presented as Authorization headers)
    (
        re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
        "Bearer ***MASKED_TOKEN***",
    ),
    # GitHub tokens: classic PATs, OAuth, server-to-server, refresh,
    # and fine-grained PATs
    (
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        "***MASKED_GITHUB_TOKEN***",
    ),
    (
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
        "***MASKED_GITHUB_TOKEN***",
    ),
    # AWS access key IDs
    (
        re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
        "***MASKED_AWS_KEY***",
    ),
    # Slack tokens
    (
        re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
        "***MASKED_SLACK_TOKEN***",
    ),
    # Bare JWTs (three base64url segments)
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        "***MASKED_JWT***",
    ),
    # Azure / generic connection strings
    (
        re.compile(
            r"(DefaultEndpointsProtocol|AccountKey|SharedAccessSignature|"
            r"Server=tcp:|Data Source=|Password=|pwd=)[^\s;\"']{8,}",
            re.IGNORECASE,
        ),
        "***MASKED_CONNECTION_STRING***",
    ),
    # Key/value context: api keys, tokens, secrets named as such
    (
        re.compile(
            r"(api[_-]?key|api[_-]?token|x-api-key|authorization|"
            r"secret[_-]?access[_-]?key|private[_-]?key|passwd|password)"
            r"[\"':\s=]+[A-Za-z0-9\-._~+/]{20,}",
            re.IGNORECASE,
        ),
        "***MASKED_API_KEY***",
    ),
]

# Generic high-entropy fallback: long alphanumeric/base64 runs.  Applied via
# a replacement function so common benign tokens (git SHAs, image digests,
# URL path segments) survive — see _mask_entropy_candidate.
_ENTROPY_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])")

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def _char_class_count(token: str) -> int:
    """Count character classes present: lowercase, uppercase, digit, +/=."""
    classes = 0
    if any(c.islower() for c in token):
        classes += 1
    if any(c.isupper() for c in token):
        classes += 1
    if any(c.isdigit() for c in token):
        classes += 1
    if any(c in "+/=" for c in token):
        classes += 1
    return classes


def _mask_entropy_candidate(match: re.Match[str]) -> str:
    """Mask a long token only when it plausibly is a secret.

    Skipped (returned unchanged):
    - pure hex of any length: git commit SHAs (40), image digests (64),
      trace IDs — overwhelmingly benign in CI logs
    - tokens containing ``/``: these are URL/FQN path segments far more
      often than base64 secrets.  Slash-containing secrets are still
      caught by the key/value-context pattern above.
    - fewer than 3 character classes: identifiers like
      ``hawkeye_release_precheck_report_pipeline`` or ``AAAA…`` padding
      lack the mixed-case-plus-digit shape of real key material
    """
    token = match.group(0)
    if "/" in token:
        return token
    if _HEX_RE.match(token):
        return token
    if _char_class_count(token) < 3:
        return token
    return "***MASKED_BASE64_SECRET***"


def sanitize(text: str) -> str:
    """Remove prompt-injection patterns and mask secrets in *text*."""
    result = strip_injections(text)
    result = mask_secrets(result)
    return result


def strip_injections(text: str) -> str:
    """Replace prompt-injection patterns in *text* with a visible marker."""
    result = text
    for pattern in _INJECTION_PATTERNS:
        result = pattern.sub(INJECTION_MARKER, result)
    return result


def mask_secrets(text: str) -> str:
    """Mask secrets found in *text*."""
    result = text
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    result = _ENTROPY_RE.sub(_mask_entropy_candidate, result)
    return result
