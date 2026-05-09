"""Account tools — profile management and connectivity.

2 tools:
- hawkeye_connect_account
- hawkeye_list_profiles
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from config.settings import get_profile, list_profiles, set_active_profile
from providers.harness.client import HarnessClient
from tools._helpers import reset_provider, run_tool


def register_account_tools(mcp: FastMCP) -> None:
    """Register all account tools on the MCP server."""

    @mcp.tool()
    async def hawkeye_connect_account(
        profile_name: str,
    ) -> str:
        """Switch the active Harness profile and test connectivity.

        Does NOT accept API tokens as parameters — tokens must be configured
        via 'make connect' for security. This tool only switches between
        pre-configured profiles.
        """
        params = {"profile_name": profile_name}

        async def _run() -> str:
            profile = get_profile(profile_name)
            if profile is None:
                available = list_profiles()
                names = [p.name for p in available]
                msg = f"Profile '{profile_name}' not found."
                if names:
                    msg += f" Available profiles: {', '.join(names)}"
                else:
                    msg += " No profiles configured. Run 'make connect' to set up."
                return msg

            set_active_profile(profile_name)
            reset_provider()

            # Test connectivity
            client = HarnessClient(profile)
            try:
                connected = await client.test_connection()
            finally:
                await client.close()

            if connected:
                return (
                    f"## Connected: {profile_name}\n"
                    f"Account: `{profile.account_id}`\n"
                    f"Org: `{profile.org_identifier}` | Project: `{profile.project_identifier}`\n"
                    f"Status: ✅ Connection successful"
                )
            return (
                f"## Profile Switched: {profile_name}\n"
                f"Account: `{profile.account_id}`\n"
                f"⚠️ Connection test failed — check API token with 'make connect'"
            )

        return await run_tool("hawkeye_connect_account", params, _run())

    @mcp.tool()
    async def hawkeye_list_profiles() -> str:
        """List all configured Hawkeye profiles with active indicator.

        Shows which accounts are set up and which one is currently active.
        """
        params: dict[str, str] = {}

        async def _run() -> str:
            profiles = list_profiles()
            if not profiles:
                return (
                    "No profiles configured.\n"
                    "Run 'make connect' to set up your first Harness account."
                )

            lines = [
                f"## Hawkeye Profiles ({len(profiles)} configured)",
                "",
            ]

            for p in profiles:
                active = " ← active" if p.is_active else ""
                lines.append(f"- **{p.name}**{active}")
                lines.append(
                    f"  Account: `{p.account_id}` | Org: `{p.org_identifier}`"
                    f" | Project: `{p.project_identifier}`"
                )
                lines.append(f"  URL: {p.base_url}")
                lines.append("")

            return "\n".join(lines)

        return await run_tool("hawkeye_list_profiles", params, _run())
