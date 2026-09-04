"""Account tools — profile management and connectivity.

2 tools:
- hawkeye_connect_account
- hawkeye_list_profiles
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from config.settings import get_profile, list_profiles, set_active_profile
from providers.harness.client import HarnessClient
from tools._helpers import (
    READ_ONLY_ANNOTATIONS,
    clear_scope_override,
    reset_provider,
    run_tool,
    set_scope_override,
)


def register_account_tools(mcp: FastMCP) -> None:
    """Register all account tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_connect_account(
        profile_name: str,
        org: str | None = None,
        project: str | None = None,
    ) -> str:
        """Switch the active Harness profile and test connectivity.

        Does NOT accept API tokens as parameters — tokens must be configured
        via 'make connect' for security. This tool only switches between
        pre-configured profiles.

        ``org`` and ``project`` aim that profile's credential at a different
        scope in the same account. A Harness token is issued at account scope,
        so one profile usually reaches every project in its account; without
        this, reading another project meant somebody creating another profile
        by hand, and any project nobody thought to add was invisible with
        nothing to say so. Pass both, or neither — one alone is ambiguous
        about what the other should be. Omitting them restores the profile's
        own coordinates.
        """
        params = {"profile_name": profile_name, "org": org, "project": project}

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

            if (org is None) != (project is None):
                return (
                    "Pass both 'org' and 'project', or neither. Supplying one "
                    "leaves the other ambiguous, and guessing it would read a "
                    "project you did not ask for."
                )

            set_active_profile(profile_name)
            if org is not None and project is not None:
                set_scope_override(org, project)
                profile = profile.model_copy(
                    update={"org_identifier": org, "project_identifier": project}
                )
            else:
                clear_scope_override()
            reset_provider()

            # Test connectivity
            client = HarnessClient(profile)
            try:
                connected = await client.test_connection()
            finally:
                await client.close()

            scoped = " (scoped)" if org is not None else ""
            if connected:
                return (
                    f"## Connected: {profile_name}{scoped}\n"
                    f"Account: `{profile.account_id}`\n"
                    f"Org: `{profile.org_identifier}` | Project: `{profile.project_identifier}`\n"
                    f"Status: ✅ Connection successful"
                )
            return (
                f"## Profile Switched: {profile_name}{scoped}\n"
                f"Account: `{profile.account_id}`\n"
                f"Org: `{profile.org_identifier}` | Project: `{profile.project_identifier}`\n"
                f"⚠️ Connection test failed — check the scope exists, or the "
                f"API token with 'make connect'"
            )

        return await run_tool("hawkeye_connect_account", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
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
