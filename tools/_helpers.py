"""Shared helpers for Hawkeye MCP tools.

Formatting utilities, provider resolution, and the audit/sanitize wrapper
used by every tool.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from config.settings import get_active_profile
from core.audit import log_tool_call
from core.models import ExecutionStatus
from core.sanitizer import sanitize
from providers.base import PipelineProvider
from providers.harness.client import HarnessAPIError, HarnessClient

logger = logging.getLogger(__name__)

# Cached provider instance
_provider: PipelineProvider | None = None
_provider_profile_name: str | None = None


async def get_provider() -> PipelineProvider:
    """Return the active provider, creating it if needed.

    Raises ``RuntimeError`` with a helpful message if no profile is configured.
    """
    global _provider, _provider_profile_name  # noqa: PLW0603

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

    _provider = HarnessClient(profile)
    _provider_profile_name = profile.name
    return _provider


def reset_provider() -> None:
    """Reset the cached provider (used when switching profiles)."""
    global _provider, _provider_profile_name  # noqa: PLW0603
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

    if isinstance(exc, HarnessAPIError):
        code = exc.status_code
        if code == 401:
            return "API token expired or invalid. Run `make connect` to update."
        if code == 403:
            return "Insufficient permissions for this operation."
        if code == 404:
            return "Execution/pipeline not found. Check the ID or URL."
        return f"Harness API error ({code}): {exc}"

    if isinstance(exc, httpx.ConnectError | httpx.TimeoutException | ConnectionError | OSError):
        return "Cannot reach Harness. Check connectivity and base URL."

    if isinstance(exc, RuntimeError) and "No active Hawkeye profile" in str(exc):
        return "No Hawkeye profile configured. Run `make connect` first."

    return str(exc)


# ------------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------------


def format_duration_ms(start_ts: int | None, end_ts: int | None) -> str:
    """Format a duration from Unix-ms timestamps into human-readable form.

    Returns "14s", "2m 10s", "1h 5m 22s", or "—" if timestamps are missing.
    """
    if start_ts is None:
        return "—"
    elapsed_ms = (
        int(time.time() * 1000) - start_ts if end_ts is None else end_ts - start_ts
    )

    if elapsed_ms < 0:
        return "—"

    total_seconds = elapsed_ms // 1000
    return format_seconds(total_seconds)


def format_seconds(total_seconds: int) -> str:
    """Format total seconds into human-readable form."""
    if total_seconds < 0:
        return "—"

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def format_date(ts: int | None) -> str:
    """Format a Unix-ms timestamp as an absolute UTC date: '2024-03-02'."""
    if ts is None:
        return "—"
    from datetime import UTC, datetime

    try:
        return datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "—"


def format_relative_time(ts: int | None) -> str:
    """Format a Unix-ms timestamp as relative time: '2h ago', '1d ago', etc."""
    if ts is None:
        return "—"

    now_ms = int(time.time() * 1000)
    diff_ms = now_ms - ts
    if diff_ms < 0:
        return "just now"

    diff_seconds = diff_ms // 1000

    if diff_seconds < 60:
        return "just now"
    if diff_seconds < 3600:
        mins = diff_seconds // 60
        return f"{mins}m ago"
    if diff_seconds < 86400:
        hours = diff_seconds // 3600
        return f"{hours}h ago"
    days = diff_seconds // 86400
    return f"{days}d ago"


def status_icon(status: ExecutionStatus) -> str:
    """Map execution status to a display icon."""
    icons = {
        ExecutionStatus.SUCCESS: "✅",
        ExecutionStatus.FAILED: "❌",
        ExecutionStatus.ERRORED: "❌",
        ExecutionStatus.ABORTED: "🚫",
        ExecutionStatus.EXPIRED: "⏰",
        ExecutionStatus.RUNNING: "🔄",
        ExecutionStatus.ASYNC_WAITING: "⏳",
        ExecutionStatus.TASK_WAITING: "⏳",
        ExecutionStatus.TIMED_WAITING: "⏳",
        ExecutionStatus.QUEUED: "📋",
        ExecutionStatus.PAUSED: "⏸️",
        ExecutionStatus.SKIPPED: "⏭️",
        ExecutionStatus.NOT_STARTED: "⏭️",
        ExecutionStatus.APPROVAL_WAITING: "🔐",
        ExecutionStatus.APPROVAL_REJECTED: "🚫",
        ExecutionStatus.INPUT_WAITING: "📝",
        ExecutionStatus.INTERVENTION_WAITING: "🔧",
        ExecutionStatus.IGNORE_FAILED: "⚠️",
        ExecutionStatus.SUSPENDED: "💤",
        ExecutionStatus.ABORTED_BY_FREEZE: "🧊",
        ExecutionStatus.WAIT_STEP_RUNNING: "⏳",
    }
    return icons.get(status, "❓")


def status_label(status: ExecutionStatus) -> str:
    """Map execution status to a short display label."""
    return f"{status_icon(status)} {status.value.upper()}"


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
