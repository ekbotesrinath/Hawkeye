"""Secret tools — Harness secret metadata lookup (never values).

2 tools:
- hawkeye_list_secrets
- hawkeye_check_secret

Both tools return metadata only: the Harness v2 secrets read API never
returns secret values, and Hawkeye never requests them.  The primary
investigation flow is resolving a ``<+secrets.getValue("account.xyz")>``
reference found in pipeline YAML or step logs to the secret's type,
secret manager, and last-rotation timestamp.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from core.models import SecretInfo
from providers.base import ProviderError
from providers.harness.parser import split_scope_prefix as _split_scope_prefix
from tools._helpers import (
    READ_ONLY_ANNOTATIONS,
    format_date,
    format_relative_time,
    fuzzy_resolve_identifier,
    get_provider,
    run_tool,
)

_VALID_SCOPES = ("project", "org", "account")

# Canonical Harness secret types, keyed by lowercase for case-insensitive input
_SECRET_TYPES = {
    t.lower(): t
    for t in ("SecretText", "SecretFile", "SSHKey", "WinRmCredentials")
}

_PERMISSION_HINT = (
    "Error: Your API token lacks permission to view secrets in this scope "
    "(core_secret_view). Ask an account admin to grant the Secret Viewer role."
)


def register_secret_tools(mcp: FastMCP) -> None:
    """Register all secret tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_list_secrets(
        search: str | None = None,
        secret_type: str | None = None,
        scope: str = "project",
    ) -> str:
        """List secret metadata in the current Harness project, org, or account.

        Returns metadata only — never secret values. Optionally filter by
        search term or secret type (SecretText, SecretFile, SSHKey,
        WinRmCredentials). Set scope to "org" or "account" for secrets
        shared above the project level.
        """
        params = {"search": search, "secret_type": secret_type, "scope": scope}

        async def _run() -> str:
            scope_lower = scope.strip().lower()
            if scope_lower not in _VALID_SCOPES:
                return (
                    f"Error: Unknown scope '{scope}'. "
                    "Use one of: project, org, account."
                )

            resolved_type = None
            if secret_type:
                resolved_type = _SECRET_TYPES.get(secret_type.strip().lower())
                if resolved_type is None:
                    valid = ", ".join(sorted(_SECRET_TYPES.values()))
                    return (
                        f"Error: Unknown secret type '{secret_type}'. "
                        f"Use one of: {valid}."
                    )

            provider = await get_provider()
            try:
                secrets = await provider.list_secrets(search=search, scope=scope_lower)
            except ProviderError as exc:
                if exc.status_code == 403:
                    return _PERMISSION_HINT
                raise
            if resolved_type:
                secrets = [s for s in secrets if s.secret_type == resolved_type]

            if not secrets:
                msg = "No secrets found"
                if search:
                    msg += f" matching '{search}'"
                if resolved_type:
                    msg += f" of type '{resolved_type}'"
                return msg + f" in {scope_lower} scope."

            return _format_secret_table(secrets, scope_lower)

        return await run_tool("hawkeye_list_secrets", params, _run())

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def hawkeye_check_secret(
        secret_ref: str,
        scope: str = "project",
    ) -> str:
        """Look up metadata for a Harness secret — never its value.

        Accepts the exact reference form used in pipeline YAML, e.g.
        "account.global_git_hub_pat" from
        <+secrets.getValue("account.global_git_hub_pat")>, or a fuzzy
        name like "github pat". Shows type, secret manager, scope, and
        created/last-modified timestamps (the best proxy for when the
        credential was last rotated). Use during failure investigation to
        identify which stored credential an auth error points at.
        """
        params = {"secret_ref": secret_ref, "scope": scope}

        async def _run() -> str:
            if not secret_ref or not secret_ref.strip():
                return "Error: Provide a secret identifier or reference to check."

            # A scope prefix on the identifier overrides the scope argument
            prefix_scope, bare_id = _split_scope_prefix(secret_ref.strip())
            scope_lower = scope.strip().lower()
            if prefix_scope != "project":
                scope_lower = prefix_scope
            if scope_lower not in _VALID_SCOPES:
                return (
                    f"Error: Unknown scope '{scope}'. "
                    "Use one of: project, org, account."
                )

            provider = await get_provider()

            # Fast path: exact identifier (the common case when the ref
            # comes straight from pipeline YAML). Fall back to fuzzy
            # matching against the secret listing on 404.
            try:
                secret = await provider.get_secret(bare_id, scope=scope_lower)
            except ProviderError as exc:
                if exc.status_code == 403:
                    return _PERMISSION_HINT
                if exc.status_code != 404:
                    raise
                try:
                    secrets = await provider.list_secrets(scope=scope_lower)
                except ProviderError as list_exc:
                    if list_exc.status_code == 403:
                        return _PERMISSION_HINT
                    raise
                candidates = [(s.identifier, s.name) for s in secrets]
                resolved_id = fuzzy_resolve_identifier(
                    bare_id, candidates, "Secret", f"{scope_lower} scope"
                )
                if resolved_id == bare_id:
                    return (
                        f"Secret '{bare_id}' not found in {scope_lower} scope. "
                        "Check the identifier, or pass scope='org' / "
                        "scope='account' (or prefix the ref with 'org.' / "
                        "'account.')."
                    )
                secret = await provider.get_secret(resolved_id, scope=scope_lower)

            return _format_secret_detail(secret, scope_lower)

        return await run_tool("hawkeye_check_secret", params, _run())


# ------------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------------


def _format_secret_table(secrets: list[SecretInfo], scope: str) -> str:
    """Format secrets as a markdown table (metadata only)."""
    lines = [
        f"## Secrets ({len(secrets)} found, {scope} scope)"
        " — metadata only, values are never accessible",
        "| Name | Identifier | Type | Secret Manager | Last Modified |",
        "|------|------------|------|----------------|---------------|",
    ]

    for s in secrets:
        manager = s.secret_manager or "—"
        modified = format_relative_time(s.last_modified)
        lines.append(
            f"| {s.name} | `{s.identifier}` | {s.secret_type or '—'}"
            f" | {manager} | {modified} |"
        )

    return "\n".join(lines)


def _format_secret_detail(secret: SecretInfo, scope: str) -> str:
    """Format a single secret's metadata."""
    header = f"## Secret: {secret.name} (`{secret.identifier}`)"

    type_str = secret.secret_type or "Unknown"
    if secret.value_type:
        type_str += f" ({secret.value_type})"

    meta_parts = [
        f"**Type:** {type_str}",
        f"**Scope:** {scope}",
        f"**Secret Manager:** {secret.secret_manager or '—'}",
    ]

    time_parts = [
        f"**Created:** {format_date(secret.created)} ({format_relative_time(secret.created)})",
        f"**Last modified:** {format_date(secret.last_modified)} "
        f"({format_relative_time(secret.last_modified)})",
    ]

    lines = [header, " | ".join(meta_parts), " | ".join(time_parts)]

    if secret.draft:
        lines.append("⚠️ **Draft** — this secret has no value stored yet.")
    if secret.description:
        lines.append(f"**Description:** {secret.description}")
    if secret.tags:
        tag_str = ", ".join(f"{k}:{v}" if v else k for k, v in secret.tags.items())
        lines.append(f"**Tags:** {tag_str}")

    lines.append(
        "\nNote: Harness stores only metadata — the secret value and any "
        "provider-side expiry (e.g. a GitHub PAT expiration date) are not "
        "visible here. 'Last modified' is the best proxy for when the "
        "credential was last rotated."
    )
    return "\n".join(lines)
