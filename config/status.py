"""Entry point for ``python -m config.status`` (``make status``).

Shows all configured profiles, marks the active one, and runs a live
connectivity check against the active profile.
"""

from __future__ import annotations

import asyncio
import sys

from rich.console import Console

console = Console()


def _print_profiles() -> str | None:
    """Print the profile table; return the active profile name (or None)."""
    from config.settings import list_profiles

    profiles = list_profiles()
    if not profiles:
        console.print("[yellow]No profiles configured.[/yellow]")
        console.print("Run [bold]make connect[/bold] to set up your first Harness account.")
        return None

    active_name: str | None = None
    console.print(f"[bold]Hawkeye profiles[/bold] ({len(profiles)} configured):\n")
    for p in profiles:
        marker = " [green](active)[/green]" if p.is_active else ""
        if p.is_active:
            active_name = p.name
        console.print(f"  [bold]{p.name}[/bold]{marker}")
        console.print(
            f"    account={p.account_id}  org={p.org_identifier}"
            f"  project={p.project_identifier}"
        )
        console.print(f"    url={p.base_url}")
    return active_name


async def _check_connectivity(profile_name: str) -> bool:
    from config.settings import get_profile
    from providers.harness.client import HarnessClient

    profile = get_profile(profile_name)
    if profile is None:
        return False
    client = HarnessClient(profile)
    try:
        return await client.test_connection()
    finally:
        await client.close()


def main() -> None:
    active_name = _print_profiles()
    if active_name is None:
        sys.exit(1)

    console.print(f"\nTesting connectivity for [bold]{active_name}[/bold]...")
    try:
        connected = asyncio.run(_check_connectivity(active_name))
    except Exception as exc:  # pragma: no cover - direct CLI feedback path
        console.print(f"[red]Connectivity check failed: {exc}[/red]")
        sys.exit(1)

    if connected:
        console.print("[green]OK: Connection successful[/green]")
    else:
        console.print(
            "[red]FAILED: Connection failed - check the API token with 'make connect'[/red]"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
