"""Deep link builder — construct Harness UI URLs from components.

1 tool:
- hawkeye_build_link
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from config.settings import get_active_profile
from tools._helpers import READ_ONLY_ANNOTATIONS, run_tool


def register_deeplink_tools(mcp: FastMCP) -> None:
    """Register deep-link tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_build_link(
        pipeline_id: str,
        execution_id: str | None = None,
        stage_id: str | None = None,
        step_id: str | None = None,
    ) -> str:
        """Build a Harness deep link URL from pipeline/execution/stage/step components.

        Returns a direct link to the pipeline, execution, or specific stage/step
        in the Harness UI. Useful for other agents or tools that need to construct
        clickable references without hard-coding URL patterns.
        """
        params = {
            "pipeline_id": pipeline_id,
            "execution_id": execution_id,
            "stage_id": stage_id,
            "step_id": step_id,
        }

        async def _run() -> str:
            profile = get_active_profile()
            if profile is None:
                return (
                    "Error: No Hawkeye profile configured. "
                    "Run `make connect` first."
                )

            base = profile.base_url.rstrip("/")
            path_parts = [
                f"{base}/ng/account/{profile.account_id}",
                "module/cd",
                f"orgs/{profile.org_identifier}",
                f"projects/{profile.project_identifier}",
                f"pipelines/{pipeline_id}",
            ]

            if execution_id:
                path_parts.append(f"executions/{execution_id}/pipeline")

            url = "/".join(path_parts)

            # Append stage/step as query parameters
            query_parts: list[str] = []
            if stage_id:
                query_parts.append(f"stage={stage_id}")
            if step_id:
                query_parts.append(f"step={step_id}")

            if query_parts:
                url += "?" + "&".join(query_parts)

            # Build a friendly description
            if execution_id and stage_id and step_id:
                desc = f"step `{step_id}` in stage `{stage_id}`"
            elif execution_id and stage_id:
                desc = f"stage `{stage_id}`"
            elif execution_id:
                desc = f"execution `{execution_id}`"
            else:
                desc = f"pipeline `{pipeline_id}`"

            return f"**Harness Link** ({desc}):\n{url}"

        return await run_tool("hawkeye_build_link", params, _run())
