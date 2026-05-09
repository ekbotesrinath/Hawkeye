"""Connector status inspection tools.

2 tools:
- hawkeye_list_connectors
- hawkeye_check_connector
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from tools._helpers import format_relative_time, get_provider, run_tool

logger = logging.getLogger(__name__)


def _status_icon(status: str) -> str:
    """Return ✅ for SUCCESS, ❌ for FAILURE, ❓ for unknown/untested."""
    upper = status.upper()
    if upper == "SUCCESS":
        return "✅"
    if upper == "FAILURE":
        return "❌"
    return "❓"


def register_connector_tools(mcp: FastMCP) -> None:
    """Register all connector tools on the MCP server."""

    @mcp.tool()
    async def hawkeye_list_connectors(search: str = "") -> str:
        """List connectors with their connectivity status.

        Returns a markdown table with connector name, type, status,
        last tested time, and scope. Use the search parameter to
        filter by name.
        """
        params = {"search": search}

        async def _run() -> str:
            provider = await get_provider()
            connectors = await provider.list_connectors(
                search=search if search else None,
            )

            if not connectors:
                return "No connectors found."

            lines = [
                "## Connectors\n",
                "| Name | Type | Status | Last Tested | Scope |",
                "|------|------|--------|-------------|-------|",
            ]
            for c in sorted(connectors, key=lambda x: x.name):
                icon = _status_icon(c.status)
                tested = format_relative_time(c.last_tested)
                lines.append(
                    f"| {c.name} | {c.connector_type} | {icon} {c.status} "
                    f"| {tested} | {c.scope} |"
                )

            success = sum(1 for c in connectors if c.status.upper() == "SUCCESS")
            total = len(connectors)
            lines.append(f"\n**Summary:** {success}/{total} connectors healthy")
            return "\n".join(lines)

        return await run_tool("hawkeye_list_connectors", params, _run())

    @mcp.tool()
    async def hawkeye_check_connector(identifier: str) -> str:
        """Check connectivity of a specific connector.

        Runs a live connectivity test against the connector. Accepts
        scope-prefixed identifiers like 'account.GHConnector' or
        plain identifiers for project-scoped connectors.
        """
        params = {"identifier": identifier}

        async def _run() -> str:
            if not identifier or not identifier.strip():
                return "Error: Provide a connector identifier to check."

            provider = await get_provider()
            result = await provider.test_connector(identifier)

            status = str(result.get("status", "UNKNOWN"))
            icon = _status_icon(status)
            error_summary = str(result.get("errorSummary", "") or "")
            errors = result.get("errors", [])
            tested_at = result.get("testedAt")
            delegate_id = str(result.get("delegateId", "") or "")

            # Determine scope and bare identifier for display
            bare_id = identifier
            scope = "project"
            if identifier.startswith("account."):
                scope = "account"
                bare_id = identifier[len("account."):]
            elif identifier.startswith("org."):
                scope = "org"
                bare_id = identifier[len("org."):]

            tested_str = format_relative_time(tested_at)

            lines = [
                f"## Connector: {bare_id}\n",
                f"**Scope:** {scope} | **Identifier:** `{identifier}`\n",
                "### Connectivity Test\n",
                f"**Status:** {icon} {status}",
                f"**Tested:** {tested_str}",
                f"**Delegate:** {delegate_id or '—'}",
            ]

            if status.upper() == "FAILURE":
                lines.append("")
                lines.append("### ❌ Error Details\n")
                if error_summary:
                    lines.append(f"**Error:** {error_summary}")
                if errors and isinstance(errors, list):
                    for err in errors:
                        if isinstance(err, dict):
                            msg = err.get("message", str(err))
                            lines.append(f"- {msg}")
                        else:
                            lines.append(f"- {err}")

            return "\n".join(lines)

        return await run_tool("hawkeye_check_connector", params, _run())
