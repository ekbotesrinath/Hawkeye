"""Environment tools — Harness environments, infrastructure definitions, services.

6 tools:
- hawkeye_list_environments
- hawkeye_get_environment
- hawkeye_list_infrastructures
- hawkeye_get_infrastructure
- hawkeye_list_services
- hawkeye_get_service

These surface the deployment-side configuration (environment variables and
overrides, infra connector references, service manifests) that failure
investigations frequently trace into.  All output is read-only YAML/metadata
and passes through the standard sanitizer.
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from core.models import EnvironmentSummary, InfrastructureSummary, ServiceSummary
from tools._helpers import (
    READ_ONLY_ANNOTATIONS,
    format_relative_time,
    fuzzy_resolve_identifier,
    get_provider,
    run_tool,
)


async def _resolve_environment_id(environment_id: str) -> str:
    """Resolve a possibly-fuzzy environment identifier to an exact one."""
    provider = await get_provider()
    environments = await provider.list_environments()
    candidates = [(e.identifier, e.name) for e in environments]
    return fuzzy_resolve_identifier(environment_id, candidates, "Environment")


def register_environment_tools(mcp: FastMCP) -> None:
    """Register all environment/infrastructure/service tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_list_environments(search: str | None = None) -> str:
        """List environments in the current Harness project.

        Optionally filter by search term. Returns a table with environment
        name, identifier, type (Production/PreProduction), and last update.
        """
        params = {"search": search}

        async def _run() -> str:
            provider = await get_provider()
            environments = await provider.list_environments(search=search)

            if not environments:
                msg = "No environments found"
                if search:
                    msg += f" matching '{search}'"
                return msg + " in the current project."

            return _format_environment_table(environments)

        return await run_tool("hawkeye_list_environments", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_get_environment(environment_id: str) -> str:
        """Get the YAML definition of a Harness environment.

        Includes environment-level variables and overrides. Supports fuzzy
        matching on the environment identifier or name. Secret references
        appear as <+secrets.getValue(...)> expressions — resolve them with
        hawkeye_check_secret.
        """
        params = {"environment_id": environment_id}

        async def _run() -> str:
            if not environment_id or not environment_id.strip():
                return "Error: Provide an environment identifier to fetch."

            resolved_id = await _resolve_environment_id(environment_id.strip())
            provider = await get_provider()
            env = await provider.get_environment(resolved_id)

            return _format_entity_detail("Environment", resolved_id, env, extra_meta=[
                ("Type", env.get("type", "")),
            ])

        return await run_tool("hawkeye_get_environment", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_list_infrastructures(environment_id: str) -> str:
        """List infrastructure definitions for a Harness environment.

        Supports fuzzy matching on the environment identifier or name.
        Returns a table with infrastructure name, identifier, type
        (KubernetesDirect, Pdc, …), and deployment type.
        """
        params = {"environment_id": environment_id}

        async def _run() -> str:
            if not environment_id or not environment_id.strip():
                return "Error: Provide an environment identifier."

            resolved_env = await _resolve_environment_id(environment_id.strip())
            provider = await get_provider()
            infrastructures = await provider.list_infrastructures(resolved_env)

            if not infrastructures:
                return (
                    f"No infrastructure definitions found in environment "
                    f"'{resolved_env}'."
                )

            return _format_infrastructure_table(infrastructures, resolved_env)

        return await run_tool("hawkeye_list_infrastructures", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_get_infrastructure(
        infrastructure_id: str, environment_id: str
    ) -> str:
        """Get the YAML definition of an infrastructure definition.

        Shows the connector reference, cluster/namespace, and deployment
        type an environment deploys to. Supports fuzzy matching on both
        the infrastructure and environment identifiers.
        """
        params = {
            "infrastructure_id": infrastructure_id,
            "environment_id": environment_id,
        }

        async def _run() -> str:
            if not infrastructure_id or not infrastructure_id.strip():
                return "Error: Provide an infrastructure identifier to fetch."
            if not environment_id or not environment_id.strip():
                return "Error: Provide the environment identifier that owns the infrastructure."

            resolved_env = await _resolve_environment_id(environment_id.strip())
            provider = await get_provider()
            infrastructures = await provider.list_infrastructures(resolved_env)
            candidates = [(i.identifier, i.name) for i in infrastructures]
            resolved_id = fuzzy_resolve_identifier(
                infrastructure_id.strip(),
                candidates,
                "Infrastructure",
                f"environment '{resolved_env}'",
            )
            infra = await provider.get_infrastructure(resolved_id, resolved_env)

            return _format_entity_detail("Infrastructure", resolved_id, infra, extra_meta=[
                ("Environment", resolved_env),
                ("Type", infra.get("type", "")),
                ("Deployment Type", infra.get("deploymentType", "")),
            ])

        return await run_tool("hawkeye_get_infrastructure", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_list_services(search: str | None = None) -> str:
        """List services in the current Harness project.

        Optionally filter by search term. Returns a table with service
        name, identifier, and last update.
        """
        params = {"search": search}

        async def _run() -> str:
            provider = await get_provider()
            services = await provider.list_services(search=search)

            if not services:
                msg = "No services found"
                if search:
                    msg += f" matching '{search}'"
                return msg + " in the current project."

            return _format_service_table(services)

        return await run_tool("hawkeye_list_services", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_get_service(service_id: str) -> str:
        """Get the YAML definition of a Harness service.

        Includes the service definition — manifests, artifact sources, and
        service variables. Supports fuzzy matching on the service
        identifier or name.
        """
        params = {"service_id": service_id}

        async def _run() -> str:
            if not service_id or not service_id.strip():
                return "Error: Provide a service identifier to fetch."

            provider = await get_provider()
            services = await provider.list_services()
            candidates = [(s.identifier, s.name) for s in services]
            resolved_id = fuzzy_resolve_identifier(
                service_id.strip(), candidates, "Service"
            )
            service = await provider.get_service(resolved_id)

            return _format_entity_detail("Service", resolved_id, service)

        return await run_tool("hawkeye_get_service", params, _run())


# ------------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------------


def _format_environment_table(environments: list[EnvironmentSummary]) -> str:
    """Format environments as a markdown table."""
    lines = [
        f"## Environments ({len(environments)} found)",
        "| Name | Identifier | Type | Store | Updated |",
        "|------|------------|------|-------|---------|",
    ]
    for e in environments:
        lines.append(
            f"| {e.name} | `{e.identifier}` | {e.env_type or '—'}"
            f" | {e.store_type} | {format_relative_time(e.last_modified)} |"
        )
    return "\n".join(lines)


def _format_infrastructure_table(
    infrastructures: list[InfrastructureSummary], environment_id: str
) -> str:
    """Format infrastructure definitions as a markdown table."""
    lines = [
        f"## Infrastructure Definitions ({len(infrastructures)} found,"
        f" environment `{environment_id}`)",
        "| Name | Identifier | Type | Deployment Type | Updated |",
        "|------|------------|------|-----------------|---------|",
    ]
    for i in infrastructures:
        lines.append(
            f"| {i.name} | `{i.identifier}` | {i.infra_type or '—'}"
            f" | {i.deployment_type or '—'} | {format_relative_time(i.last_modified)} |"
        )
    return "\n".join(lines)


def _format_service_table(services: list[ServiceSummary]) -> str:
    """Format services as a markdown table."""
    lines = [
        f"## Services ({len(services)} found)",
        "| Name | Identifier | Store | Updated |",
        "|------|------------|-------|---------|",
    ]
    for s in services:
        lines.append(
            f"| {s.name} | `{s.identifier}` | {s.store_type}"
            f" | {format_relative_time(s.last_modified)} |"
        )
    return "\n".join(lines)


def _format_entity_detail(
    label: str,
    identifier: str,
    entity: dict,  # type: ignore[type-arg]
    extra_meta: list[tuple[str, str]] | None = None,
) -> str:
    """Format an entity detail response with its YAML."""
    name = entity.get("name", identifier)
    description = entity.get("description", "")

    yaml_content = entity.get("yaml", "")
    if not yaml_content:
        yaml_content = json.dumps(entity, indent=2)

    lines = [f"## {label}: {name} (`{identifier}`)"]

    meta_parts = [
        f"**{key}:** {value}" for key, value in (extra_meta or []) if value
    ]
    if meta_parts:
        lines.append(" | ".join(meta_parts))
    if description:
        lines.append(f"\n{description}")
    lines.append(f"\n```yaml\n{yaml_content}\n```")
    return "\n".join(lines)
