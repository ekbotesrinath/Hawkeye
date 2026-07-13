"""Template tools — Harness template listing and YAML fetch.

2 tools:
- hawkeye_list_templates
- hawkeye_get_template
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from core.models import TemplateSummary
from tools._helpers import READ_ONLY_ANNOTATIONS, format_relative_time, get_provider, run_tool

_VALID_SCOPES = ("project", "org", "account")

# Canonical Harness template entity types, keyed by lowercase for
# case-insensitive user input ("step" -> "Step").
_TEMPLATE_TYPES = {
    t.lower(): t
    for t in (
        "Step",
        "Stage",
        "Pipeline",
        "StepGroup",
        "MonitoredService",
        "SecretManager",
        "ArtifactSource",
        "Deployment",
    )
}


def _split_scope_prefix(identifier: str) -> tuple[str, str]:
    """Split an ``account.`` / ``org.`` scope prefix from a template ID."""
    if identifier.startswith("account."):
        return "account", identifier[len("account."):]
    if identifier.startswith("org."):
        return "org", identifier[len("org."):]
    return "project", identifier


async def _resolve_template_id(template_id: str, scope: str) -> str:
    """Resolve a possibly-fuzzy template identifier to an exact one.

    Mirrors ``resolve_pipeline_id``: exact identifiers pass through, partial
    identifiers or display names are fuzzy-matched against the template list
    in the given scope.  Returns *template_id* unchanged when listing yields
    nothing (e.g. no templates or listing unsupported).  Raises ``ValueError``
    with suggestions when templates exist but nothing matches.
    """
    from core.fuzzy import fuzzy_match, fuzzy_suggest

    provider = await get_provider()
    templates = await provider.list_templates(scope=scope)
    if not templates:
        return template_id

    identifiers = [t.identifier for t in templates]
    if template_id in identifiers:
        return template_id

    match = fuzzy_match(template_id, identifiers)
    if match is not None:
        return match.name

    # Fall back to display names, mapped back to identifiers
    name_to_id = {t.name: t.identifier for t in templates}
    name_match = fuzzy_match(template_id, list(name_to_id.keys()))
    if name_match is not None:
        return name_to_id[name_match.name]

    suggestions = fuzzy_suggest(template_id, identifiers, max_suggestions=3)
    if suggestions:
        names = ", ".join(f"`{s.name}`" for s in suggestions)
        raise ValueError(
            f"Template '{template_id}' not found in {scope} scope. Did you mean: {names}?"
        )
    raise ValueError(
        f"Template '{template_id}' not found and no similar templates exist in {scope} scope."
    )


def register_template_tools(mcp: FastMCP) -> None:
    """Register all template tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_list_templates(
        search: str | None = None,
        template_type: str | None = None,
        scope: str = "project",
    ) -> str:
        """List templates in the current Harness project, org, or account.

        Optionally filter by search term or template type (Step, Stage,
        Pipeline, StepGroup, …). Set scope to "org" or "account" to list
        templates shared above the project level. Returns a table with
        template name, type, version, and stable flag.
        """
        params = {"search": search, "template_type": template_type, "scope": scope}

        async def _run() -> str:
            scope_lower = scope.strip().lower()
            if scope_lower not in _VALID_SCOPES:
                return (
                    f"Error: Unknown scope '{scope}'. "
                    "Use one of: project, org, account."
                )

            resolved_type = None
            if template_type:
                resolved_type = _TEMPLATE_TYPES.get(template_type.strip().lower())
                if resolved_type is None:
                    valid = ", ".join(sorted(_TEMPLATE_TYPES.values()))
                    return (
                        f"Error: Unknown template type '{template_type}'. "
                        f"Use one of: {valid}."
                    )

            provider = await get_provider()
            templates = await provider.list_templates(
                search=search, template_type=resolved_type, scope=scope_lower
            )

            if not templates:
                msg = "No templates found"
                if search:
                    msg += f" matching '{search}'"
                if resolved_type:
                    msg += f" of type '{resolved_type}'"
                return msg + f" in {scope_lower} scope."

            return _format_template_table(templates, scope_lower)

        return await run_tool("hawkeye_list_templates", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_get_template(
        template_id: str,
        version_label: str | None = None,
        scope: str = "project",
    ) -> str:
        """Get the YAML definition of a Harness template.

        Supports fuzzy matching on the template identifier or name. When
        version_label is omitted, the stable version is returned. Prefix the
        identifier with "account." or "org." (or set scope) for templates
        shared above the project level.
        """
        params = {
            "template_id": template_id,
            "version_label": version_label,
            "scope": scope,
        }

        async def _run() -> str:
            if not template_id or not template_id.strip():
                return "Error: Provide a template identifier to fetch."

            # A scope prefix on the identifier overrides the scope argument
            prefix_scope, bare_id = _split_scope_prefix(template_id.strip())
            scope_lower = scope.strip().lower()
            if prefix_scope != "project":
                scope_lower = prefix_scope
            if scope_lower not in _VALID_SCOPES:
                return (
                    f"Error: Unknown scope '{scope}'. "
                    "Use one of: project, org, account."
                )

            resolved_id = await _resolve_template_id(bare_id, scope_lower)
            provider = await get_provider()
            template = await provider.get_template(
                resolved_id, version_label=version_label, scope=scope_lower
            )

            return _format_template_detail(resolved_id, template, scope_lower)

        return await run_tool("hawkeye_get_template", params, _run())


# ------------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------------


def _format_template_table(templates: list[TemplateSummary], scope: str) -> str:
    """Format templates as a markdown table."""
    lines = [
        f"## Templates ({len(templates)} found, {scope} scope)",
        "| Name | Identifier | Type | Child Type | Version | Stable | Store | Updated |",
        "|------|------------|------|------------|---------|--------|-------|---------|",
    ]

    for t in templates:
        child = t.child_type or "—"
        version = t.version_label or "—"
        stable = "✅" if t.stable_template else "—"
        updated = format_relative_time(t.last_updated)
        lines.append(
            f"| {t.name} | `{t.identifier}` | {t.template_entity_type or '—'}"
            f" | {child} | {version} | {stable} | {t.store_type} | {updated} |"
        )

    return "\n".join(lines)


def _format_template_detail(template_id: str, template: dict, scope: str) -> str:  # type: ignore[type-arg]
    """Format a template detail response with its YAML."""
    name = template.get("name", template_id)
    version = template.get("versionLabel", "")
    entity_type = template.get("templateEntityType", "")
    child_type = template.get("childType", "")
    description = template.get("description", "")

    yaml_content = template.get("yaml", "")
    if not yaml_content:
        import json

        yaml_content = json.dumps(template, indent=2)

    header = f"## Template: {name} (`{template_id}`)"

    meta_parts: list[str] = []
    if entity_type:
        type_str = entity_type
        if child_type:
            type_str += f" / {child_type}"
        meta_parts.append(f"**Type:** {type_str}")
    if version:
        version_str = version
        if template.get("stableTemplate"):
            version_str += " (stable)"
        meta_parts.append(f"**Version:** {version_str}")
    meta_parts.append(f"**Scope:** {scope}")

    lines = [header, " | ".join(meta_parts)]
    if description:
        lines.append(f"\n{description}")
    lines.append(f"\n```yaml\n{yaml_content}\n```")
    return "\n".join(lines)
