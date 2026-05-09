"""Pipeline tools — pipeline definitions, listings, and input sets.

3 tools:
- hawkeye_get_pipeline_definition
- hawkeye_list_pipelines
- hawkeye_get_input_sets
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from core.models import InputSet, PipelineSummary
from tools._helpers import (
    format_relative_time,
    get_provider,
    resolve_pipeline_id,
    run_tool,
    status_icon,
)


def register_pipeline_tools(mcp: FastMCP) -> None:
    """Register all pipeline tools on the MCP server."""

    @mcp.tool()
    async def hawkeye_get_pipeline_definition(
        pipeline_id: str,
        branch: str | None = None,
    ) -> str:
        """Get the YAML definition of a pipeline.

        Returns the pipeline configuration, not an execution. For remote pipelines
        stored in Git, specify the branch to fetch from.
        """
        params = {"pipeline_id": pipeline_id, "branch": branch}

        async def _run() -> str:
            resolved_id = await resolve_pipeline_id(pipeline_id)
            provider = await get_provider()
            definition = await provider.get_pipeline_definition(resolved_id, branch=branch)

            yaml_content = ""
            if isinstance(definition, dict):
                yaml_content = definition.get("pipeline_yaml", "")
                if not yaml_content:
                    yaml_content = definition.get("yaml", "")
                if not yaml_content:
                    import json
                    yaml_content = json.dumps(definition, indent=2)
            else:
                yaml_content = str(definition)

            header = f"## Pipeline Definition: `{pipeline_id}`"
            if branch:
                header += f" (branch: {branch})"
            return f"{header}\n\n```yaml\n{yaml_content}\n```"

        return await run_tool("hawkeye_get_pipeline_definition", params, _run())

    @mcp.tool()
    async def hawkeye_list_pipelines(
        search: str | None = None,
        module: str | None = None,
    ) -> str:
        """List all pipelines in the current project.

        Optionally filter by search term or module (CI, CD). Returns a table
        with pipeline name, last execution status, and modules.
        """
        params = {"search": search, "module": module}

        async def _run() -> str:
            provider = await get_provider()
            pipelines = await provider.list_pipelines(search=search)

            if module:
                pipelines = [
                    p for p in pipelines
                    if module.upper() in [m.upper() for m in p.modules]
                ]

            if not pipelines:
                msg = "No pipelines found"
                if search:
                    msg += f" matching '{search}'"
                if module:
                    msg += f" in module '{module}'"
                return msg + "."

            return _format_pipeline_table(pipelines)

        return await run_tool("hawkeye_list_pipelines", params, _run())

    @mcp.tool()
    async def hawkeye_get_input_sets(
        pipeline_id: str,
    ) -> str:
        """Get all available input sets for a pipeline.

        Returns the list of input sets with names and identifiers that can be
        used when triggering the pipeline.
        """
        params = {"pipeline_id": pipeline_id}

        async def _run() -> str:
            resolved_id = await resolve_pipeline_id(pipeline_id)
            provider = await get_provider()
            input_sets = await provider.get_input_sets(resolved_id)

            if not input_sets:
                return f"No input sets found for pipeline `{pipeline_id}`."

            return _format_input_sets(pipeline_id, input_sets)

        return await run_tool("hawkeye_get_input_sets", params, _run())


# ------------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------------


def _format_pipeline_table(pipelines: list[PipelineSummary]) -> str:
    """Format pipelines as a markdown table."""
    lines = [
        f"## Pipelines ({len(pipelines)} found)",
        "| Name | Identifier | Modules | Last Status | Store | Last Run |",
        "|------|------------|---------|-------------|-------|----------|",
    ]

    for p in pipelines:
        modules = ", ".join(p.modules) if p.modules else "—"
        if p.last_execution_status:
            last_status = f"{status_icon(p.last_execution_status)} {p.last_execution_status.value}"
        else:
            last_status = "—"
        store = p.store_type or "INLINE"
        last_run = format_relative_time(p.last_execution_ts)
        lines.append(
            f"| {p.name} | `{p.identifier}` | {modules}"
            f" | {last_status} | {store} | {last_run} |"
        )

    return "\n".join(lines)


def _format_input_sets(pipeline_id: str, input_sets: list[InputSet]) -> str:
    """Format input sets as a readable list."""
    lines = [
        f"## Input Sets for `{pipeline_id}` ({len(input_sets)} found)",
        "",
    ]

    for i, iset in enumerate(input_sets, 1):
        lines.append(f"### {i}. {iset.name} (`{iset.identifier}`)")
        if iset.input_set_yaml:
            lines.append(f"```yaml\n{iset.input_set_yaml}\n```")
        lines.append("")

    return "\n".join(lines)
