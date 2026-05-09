"""Shared formatting utilities for Hawkeye MCP tools.

Centralises duration formatting, relative time, status icons, table generation,
and log truncation so every tool produces consistent output.
"""

from __future__ import annotations

import time

from core.models import ExecutionStatus, PipelineExecution


# ------------------------------------------------------------------
# Duration formatting
# ------------------------------------------------------------------


def format_duration(start_ts: int | None, end_ts: int | None) -> str:
    """Format a duration from Unix-ms timestamps into human-readable form.

    If *end_ts* is ``None`` the duration is measured from *start_ts* to now.
    Returns ``"—"`` when *start_ts* is missing or the result would be negative.
    """
    if start_ts is None:
        return "—"
    elapsed_ms = (
        int(time.time() * 1000) - start_ts if end_ts is None else end_ts - start_ts
    )
    if elapsed_ms < 0:
        return "—"
    total_seconds = elapsed_ms // 1000
    return _format_seconds(total_seconds)


def _format_seconds(total_seconds: int) -> str:
    """Format total seconds into ``12m 34s`` style."""
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


# ------------------------------------------------------------------
# Relative time
# ------------------------------------------------------------------


def format_relative_time(ts: int | None) -> str:
    """Format a Unix-ms timestamp as relative time: ``2h ago``, ``1d ago``."""
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
        return f"{diff_seconds // 60}m ago"
    if diff_seconds < 86400:
        return f"{diff_seconds // 3600}h ago"
    return f"{diff_seconds // 86400}d ago"


# ------------------------------------------------------------------
# Status icons
# ------------------------------------------------------------------

_STATUS_ICONS: dict[ExecutionStatus, str] = {
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


def format_status_icon(status: ExecutionStatus) -> str:
    """Map execution status to a display icon."""
    return _STATUS_ICONS.get(status, "❓")


# ------------------------------------------------------------------
# Execution table
# ------------------------------------------------------------------


def format_execution_table(executions: list[PipelineExecution]) -> str:
    """Format a list of executions as a markdown table."""
    if not executions:
        return "No executions to display."

    lines = [
        "| # | Pipeline | Status | Branch | Duration | Trigger | Link |",
        "|---|----------|--------|--------|----------|---------|------|",
    ]
    for ex in executions:
        icon = format_status_icon(ex.status)
        dur = format_duration(ex.start_ts, ex.end_ts)
        branch = ex.branch or "—"
        trigger = ex.triggered_by or ex.trigger_type
        link = f"[Link]({ex.execution_url})" if ex.execution_url else "—"
        lines.append(
            f"| #{ex.run_sequence} | {ex.pipeline_name} "
            f"| {icon} {ex.status.value} | {branch} | {dur} | {trigger} | {link} |"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------
# Log truncation
# ------------------------------------------------------------------


def truncate_logs(log_text: str, max_lines: int = 200) -> str:
    """Truncate log text to the last *max_lines* with a note about omission."""
    lines = log_text.splitlines()
    if len(lines) <= max_lines:
        return log_text
    truncated = lines[-max_lines:]
    return (
        f"... ({len(lines) - max_lines} lines truncated) ...\n"
        + "\n".join(truncated)
    )
