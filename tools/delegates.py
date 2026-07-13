"""Delegate health inspection tools.

2 tools:
- hawkeye_list_delegates
- hawkeye_check_delegate
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from mcp.server.fastmcp import FastMCP

from core.fuzzy import fuzzy_match, fuzzy_suggest
from tools._helpers import READ_ONLY_ANNOTATIONS, format_relative_time, get_provider, run_tool

logger = logging.getLogger(__name__)


def _health_icon(actively_connected: bool) -> str:
    """Return ✅ for connected delegates, ❌ for disconnected."""
    return "✅" if actively_connected else "❌"


def _format_absolute_time(ts: int | None) -> str:
    """Format a Unix-ms timestamp as an absolute ISO-like string."""
    if ts is None:
        return "—"
    dt = datetime.fromtimestamp(ts / 1000, tz=UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def register_delegate_tools(mcp: FastMCP) -> None:
    """Register all delegate tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_list_delegates() -> str:
        """List all delegate groups with health status.

        Returns a markdown table with delegate name, connection status,
        last heartbeat, type, and tags. Useful for checking overall
        delegate fleet health.
        """
        params: dict[str, str] = {}

        async def _run() -> str:
            provider = await get_provider()
            delegates = await provider.list_delegates()

            if not delegates:
                return "No delegates found in this Harness account/project."

            lines = [
                "## Delegate Groups\n",
                "| Name | Status | Connected | Last Heartbeat | Type | Instances | Tags |",
                "|------|--------|-----------|----------------|------|-----------|------|",
            ]
            for d in sorted(delegates, key=lambda x: x.name):
                icon = _health_icon(d.actively_connected)
                heartbeat = format_relative_time(d.last_heartbeat)
                tags = ", ".join(d.tags) if d.tags else "—"
                lines.append(
                    f"| {d.name} | {d.status} | {icon} | {heartbeat} "
                    f"| {d.delegate_type} | {d.instances_connected} | {tags} |"
                )

            connected = sum(1 for d in delegates if d.actively_connected)
            total = len(delegates)
            lines.append(f"\n**Summary:** {connected}/{total} delegates actively connected")
            return "\n".join(lines)

        return await run_tool("hawkeye_list_delegates", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_check_delegate(name: str) -> str:
        """Check health of a specific delegate by name (supports fuzzy matching).

        Provide the full or partial delegate name (e.g., "tanr" for
        "newad-eswd-tanr-dlgt"). Returns detailed health info including
        connection status, last heartbeat, type, and tags.
        """
        params = {"name": name}

        async def _run() -> str:
            if not name or not name.strip():
                return "Error: Provide a delegate name to check."

            provider = await get_provider()
            delegates = await provider.list_delegates()

            if not delegates:
                return "No delegates found in this Harness account/project."

            delegate_names = [d.name for d in delegates]
            match = fuzzy_match(name, delegate_names)

            if match is None:
                suggestions = fuzzy_suggest(name, delegate_names, max_suggestions=5)
                lines = [f"⚠️ No delegate matching `{name}` found.\n"]
                if suggestions:
                    lines.append("**Available delegates:**")
                    for s in suggestions:
                        lines.append(f"  • `{s.name}` (score: {s.score:.0%})")
                else:
                    lines.append("**Available delegates:**")
                    for d in sorted(delegates, key=lambda x: x.name):
                        icon = _health_icon(d.actively_connected)
                        lines.append(f"  • {icon} `{d.name}`")
                return "\n".join(lines)

            # Find the matched delegate
            matched = next(d for d in delegates if d.name == match.name)
            icon = _health_icon(matched.actively_connected)
            connected_str = "Yes" if matched.actively_connected else "No"
            heartbeat_rel = format_relative_time(matched.last_heartbeat)
            heartbeat_abs = _format_absolute_time(matched.last_heartbeat)
            tags = ", ".join(matched.tags) if matched.tags else "—"

            lines = [
                f"## Delegate: {matched.name}\n",
                f"**Status:** {icon} {matched.status} | "
                f"**Connected:** {connected_str} | "
                f"**Instances:** {matched.instances_connected}",
                f"**Last Heartbeat:** {heartbeat_rel} ({heartbeat_abs})",
                f"**Type:** {matched.delegate_type}",
                f"**Tags:** {tags}",
                f"**Identifier:** `{matched.identifier}`",
            ]

            if not matched.actively_connected:
                lines.append(
                    "\n⚠️ **Warning:** This delegate is not actively connected. "
                    "Pipeline steps targeting this delegate may fail or queue indefinitely."
                )

            return "\n".join(lines)

        return await run_tool("hawkeye_check_delegate", params, _run())
