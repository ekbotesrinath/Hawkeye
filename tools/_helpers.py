"""Shared helpers for Hawkeye MCP tools.

Formatting utilities, provider resolution, and the audit/sanitize wrapper
used by every tool.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from mcp.types import ToolAnnotations

from config.settings import get_active_profile
from core.audit import log_tool_call
from core.formatters import (
    format_date,
    format_duration,
    format_relative_time,
    format_seconds,
    format_status_icon,
    format_status_label,
)
from core.sanitizer import sanitize
from providers.base import PipelineProvider, ProviderError
from providers.harness.client import HarnessClient

__all__ = [
    "clamp_limit",
    "format_date",
    "format_duration_ms",
    "format_relative_time",
    "format_seconds",
    "fuzzy_resolve_identifier",
    "get_provider",
    "reset_provider",
    "resolve_pipeline_id",
    "run_tool",
    "status_icon",
    "status_label",
]

logger = logging.getLogger(__name__)

# Every Hawkeye tool is read-only — advertise that in the MCP protocol so
# clients can display and authorize them accordingly.
READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    openWorldHint=True,
)

# Cached provider instance
_provider: PipelineProvider | None = None
_provider_profile_name: str | None = None
_provider_lock = asyncio.Lock()
# Providers replaced by reset/switch, awaiting close on the next async call
_stale_providers: list[PipelineProvider] = []


async def _close_stale_providers() -> None:
    while _stale_providers:
        old = _stale_providers.pop()
        try:
            await old.close()  # type: ignore[attr-defined]
        except Exception:
            logger.debug("Failed to close stale provider", exc_info=True)


async def get_provider() -> PipelineProvider:
    """Return the active provider, creating it if needed.

    Thread-/task-safe: a lock prevents two parallel tool calls from racing
    to create duplicate clients.  Raises ``RuntimeError`` with a helpful
    message if no profile is configured.
    """
    global _provider, _provider_profile_name  # noqa: PLW0603

    async with _provider_lock:
        await _close_stale_providers()

        profile = get_active_profile()
        if profile is None:
            raise RuntimeError(
                "No active Hawkeye profile configured. "
                "Run 'make connect' to set up your Harness account, "
                "or use hawkeye_connect_account to switch profiles."
            )

        # Re-create client if profile changed
        if _provider is not None and _provider_profile_name == profile.name:
            return _provider

        if _provider is not None:
            _stale_providers.append(_provider)
            await _close_stale_providers()

        _provider = HarnessClient(profile)
        _provider_profile_name = profile.name
        return _provider


def reset_provider() -> None:
    """Reset the cached provider (used when switching profiles).

    The old client's connection pool is closed on the next
    :func:`get_provider` call (closing needs an event loop; this function
    stays sync for existing callers).
    """
    global _provider, _provider_profile_name  # noqa: PLW0603
    if _provider is not None:
        _stale_providers.append(_provider)
    _provider = None
    _provider_profile_name = None


async def run_tool(
    tool_name: str,
    parameters: dict[str, Any],
    coro: Any,
) -> str:
    """Execute a tool coroutine with audit logging and output sanitization.

    1. Audit-log the invocation (tool name, params, start time)
    2. Await the coroutine
    3. Sanitize the result
    4. Audit-log the outcome (status, duration)
    5. Return the sanitized string
    """
    start = time.monotonic()
    try:
        result = await coro
        duration_ms = (time.monotonic() - start) * 1000
        sanitized = sanitize(str(result))
        log_tool_call(tool_name, parameters, "success", duration_ms)
        return sanitized
    except Exception as exc:
        duration_ms = (time.monotonic() - start) * 1000
        log_tool_call(tool_name, parameters, "error", duration_ms)
        logger.exception("Tool %s failed", tool_name)
        return sanitize(f"Error: {_friendly_error(exc)}")


def _friendly_error(exc: Exception) -> str:
    """Translate known exceptions into user-friendly messages."""
    import httpx

    if isinstance(exc, ProviderError):
        code = exc.status_code
        if code == 401:
            return "API token expired or invalid. Run `make connect` to update."
        if code == 403:
            return "Insufficient permissions for this operation."
        if code == 404:
            return "Execution/pipeline not found. Check the ID or URL."
        return f"Provider API error ({code}): {exc}"

    if isinstance(exc, httpx.ConnectError | httpx.TimeoutException | ConnectionError | OSError):
        return "Cannot reach Harness. Check connectivity and base URL."

    if isinstance(exc, RuntimeError) and "No active Hawkeye profile" in str(exc):
        return "No Hawkeye profile configured. Run `make connect` first."

    return str(exc)


# ------------------------------------------------------------------
# Input validation
# ------------------------------------------------------------------


def clamp_limit(value: int, *, lo: int = 1, hi: int = 50, default: int = 10) -> int:
    """Clamp a caller-provided list limit into a safe range.

    Non-integer or absurd values from the model are coerced instead of
    being passed to the provider API verbatim.
    """
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


# ------------------------------------------------------------------
# Formatting helpers — canonical implementations live in core.formatters;
# these aliases keep the established tool-facing names stable.
# ------------------------------------------------------------------

format_duration_ms = format_duration
status_icon = format_status_icon
status_label = format_status_label


async def resolve_pipeline_id(pipeline_id: str) -> str:
    """Resolve a possibly-fuzzy pipeline identifier to an exact one.

    If *pipeline_id* exactly matches a known pipeline identifier, return it
    unchanged.  Otherwise, list all pipelines in the project and use fuzzy
    matching to find the best candidate.  Returns the original *pipeline_id*
    unchanged when no pipelines are available (e.g. listing not supported).
    Raises ``ValueError`` when pipelines exist but no reasonable match is found.
    """
    from core.fuzzy import fuzzy_match, fuzzy_suggest

    provider = await get_provider()

    # The project listing is paginated, so an exact identifier may not appear
    # in an unfiltered page.  Search server-side for the requested ID first —
    # an exact hit passes through without fuzzy matching.
    searched = await provider.list_pipelines(search=pipeline_id)
    searched_ids = [p.identifier for p in searched]
    if pipeline_id in searched_ids:
        return pipeline_id

    pipelines = await provider.list_pipelines()
    identifiers = [p.identifier for p in pipelines]
    # Merge search hits so fuzzy matching sees pipelines beyond the first page
    identifiers.extend(i for i in searched_ids if i not in identifiers)

    # No pipelines listed — pass through as-is
    if not identifiers:
        return pipeline_id

    # Fast path: exact match
    if pipeline_id in identifiers:
        return pipeline_id

    match = fuzzy_match(pipeline_id, identifiers)
    if match is not None:
        return match.name

    # No good match — build suggestion message
    suggestions = fuzzy_suggest(pipeline_id, identifiers, max_suggestions=3)
    if suggestions:
        names = ", ".join(f"`{s.name}`" for s in suggestions)
        raise ValueError(
            f"Pipeline '{pipeline_id}' not found. Did you mean: {names}?"
        )
    raise ValueError(f"Pipeline '{pipeline_id}' not found and no similar pipelines exist.")


def fuzzy_resolve_identifier(
    requested: str,
    candidates: list[tuple[str, str]],
    entity_label: str,
    scope_hint: str = "",
) -> str:
    """Resolve a possibly-fuzzy identifier against (identifier, name) pairs.

    Mirrors ``resolve_pipeline_id``: exact identifiers pass through, partial
    identifiers or display names are fuzzy-matched.  Returns *requested*
    unchanged when *candidates* is empty (e.g. listing unsupported).  Raises
    ``ValueError`` with suggestions when candidates exist but nothing matches.
    """
    from core.fuzzy import fuzzy_match, fuzzy_suggest

    if not candidates:
        return requested

    identifiers = [ident for ident, _name in candidates]
    if requested in identifiers:
        return requested

    match = fuzzy_match(requested, identifiers)
    if match is not None:
        return match.name

    # Fall back to display names, mapped back to identifiers
    name_to_id = {name: ident for ident, name in candidates if name}
    name_match = fuzzy_match(requested, list(name_to_id.keys()))
    if name_match is not None:
        return name_to_id[name_match.name]

    where = f" in {scope_hint}" if scope_hint else ""
    suggestions = fuzzy_suggest(requested, identifiers, max_suggestions=3)
    if suggestions:
        names = ", ".join(f"`{s.name}`" for s in suggestions)
        raise ValueError(
            f"{entity_label} '{requested}' not found{where}. Did you mean: {names}?"
        )
    raise ValueError(
        f"{entity_label} '{requested}' not found and no similar "
        f"{entity_label.lower()}s exist{where}."
    )
