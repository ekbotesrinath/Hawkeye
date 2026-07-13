"""Hawkeye — Universal Pipeline Observability MCP Server.

Entry point for the MCP server.  Uses FastMCP from the official Python SDK.
All tools are registered from tools/*.py modules.

Toolset filtering:
    Set ``HAWKEYE_TOOLSETS`` env var to a comma-separated list of toolset
    names to limit which tools are registered.  When unset or empty, all
    toolsets are loaded (backward-compatible).  The ``account`` toolset and
    ``ping`` health check are always registered regardless of the filter.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from tools.account import register_account_tools
from tools.connectors import register_connector_tools
from tools.deeplinks import register_deeplink_tools
from tools.delegates import register_delegate_tools
from tools.environments import register_environment_tools
from tools.executions import register_execution_tools
from tools.investigate import register_investigation_tools
from tools.pipelines import register_pipeline_tools
from tools.secrets import register_secret_tools
from tools.templates import register_template_tools
from tools.terraform import register_terraform_tools

# Configure logging to stderr (stdout is reserved for MCP stdio transport)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Toolset registry — maps toolset names to their registration functions
# ---------------------------------------------------------------------------

_TOOLSET_REGISTRY: dict[str, Callable[[FastMCP], None]] = {
    "execution": register_execution_tools,
    "investigation": register_investigation_tools,
    "pipeline": register_pipeline_tools,
    "templates": register_template_tools,
    "account": register_account_tools,
    "deeplinks": register_deeplink_tools,
    "terraform": register_terraform_tools,
    "delegates": register_delegate_tools,
    "connectors": register_connector_tools,
    "secrets": register_secret_tools,
    "environments": register_environment_tools,
}

_ALWAYS_REGISTERED: set[str] = {"account"}


def _get_active_toolsets() -> set[str]:
    """Determine which toolsets to register based on HAWKEYE_TOOLSETS env var."""
    raw = os.environ.get("HAWKEYE_TOOLSETS", "").strip()
    if not raw or raw.lower() == "all":
        return set(_TOOLSET_REGISTRY.keys())

    requested = {t.strip().lower() for t in raw.split(",") if t.strip()}
    valid: set[str] = set()
    for name in requested:
        if name == "all":
            return set(_TOOLSET_REGISTRY.keys())
        if name in _TOOLSET_REGISTRY:
            valid.add(name)
        else:
            logger.warning("Unknown toolset '%s' in HAWKEYE_TOOLSETS, ignoring", name)

    # Always include mandatory toolsets
    valid |= _ALWAYS_REGISTERED
    return valid


def create_server(toolsets: set[str] | None = None) -> FastMCP:
    """Create and configure the Hawkeye MCP server.

    Parameters
    ----------
    toolsets:
        Explicit set of toolset names to register.  When *None*, the active
        toolsets are resolved from the ``HAWKEYE_TOOLSETS`` environment
        variable via :func:`_get_active_toolsets`.
    """
    server = FastMCP(
        "Hawkeye",
        instructions="Universal pipeline observability MCP server for SRE",
    )

    active = toolsets if toolsets is not None else _get_active_toolsets()
    for name, register_fn in _TOOLSET_REGISTRY.items():
        if name in active:
            register_fn(server)
            logger.info("Registered toolset: %s", name)
        else:
            logger.info("Skipped toolset: %s (not in HAWKEYE_TOOLSETS)", name)

    # ping is always registered regardless of toolset filter
    @server.tool()
    async def ping() -> str:
        """Health check — returns server status."""
        return "Hawkeye MCP server is running"

    return server


# Module-level server instance — used by MCP stdio transport
mcp = create_server()


def _handle_shutdown(signum: int, _frame: object) -> None:
    """Graceful shutdown on SIGINT/SIGTERM."""
    sig_name = signal.Signals(signum).name
    logger.info("Received %s, shutting down…", sig_name)
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)
    logger.info("Starting Hawkeye MCP server…")
    mcp.run()
