"""Interactive first-time setup wizard for Hawkeye.

Guides the user through configuring their Harness connection:
- Harness base URL
- Account ID
- Org and Project identifiers
- API token (stored securely in OS keyring)
- Connectivity test
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from rich.console import Console
from rich.panel import Panel

console = Console()


def _prompt(label: str, default: str | None = None) -> str:
    """Prompt for a value with optional default."""
    if default:
        raw = input(f"  {label} [{default}]: ").strip()
        return raw if raw else default
    while True:
        raw = input(f"  {label}: ").strip()
        if raw:
            return raw
        console.print("  [red]This field is required.[/red]")


def _prompt_secret(label: str) -> str:
    """Prompt for a secret value (hidden input)."""
    while True:
        raw = getpass.getpass(f"  {label}: ").strip()
        if raw:
            return raw
        console.print("  [red]This field is required.[/red]")


async def _test_connection(
    base_url: str, account_id: str, org_id: str, project_id: str, token: str
) -> bool:
    """Test Harness API connectivity.

    Temporarily stores the token in keyring under a test profile, runs the
    connectivity check, then cleans up.
    """
    from core.credentials import delete_token, store_token
    from core.models import HarnessProfile
    from providers.harness.client import HarnessClient

    test_profile_name = "__setup_test__"
    profile = HarnessProfile(
        name=test_profile_name,
        account_id=account_id,
        org_identifier=org_id,
        project_identifier=project_id,
        base_url=base_url,
    )
    store_token(test_profile_name, token)
    client = HarnessClient(profile)
    try:
        return await client.test_connection()
    finally:
        await client.close()
        try:
            delete_token(test_profile_name)
        except Exception:
            pass


def run_setup() -> None:
    """Run the interactive setup wizard."""
    console.print(
        Panel(
            "[bold]Hawkeye — Pipeline Observability[/bold]\n"
            "First-time setup wizard",
            style="blue",
        )
    )
    console.print()

    # Step 1: Collect configuration
    console.print("[bold]Step 1:[/bold] Harness connection details\n")

    base_url = _prompt("Harness base URL", default="https://app.harness.io")
    account_id = _prompt("Account ID")
    org_id = _prompt("Organization Identifier", default="default")
    project_id = _prompt("Project Identifier")

    console.print()
    console.print("[bold]Step 2:[/bold] API token\n")
    console.print("  [dim]Your token is stored securely in the OS keyring.[/dim]")
    console.print("  [dim]It is never logged, printed, or sent to any MCP client.[/dim]\n")
    token = _prompt_secret("Harness API Token")

    # Step 2: Test connectivity
    console.print()
    console.print("[bold]Step 3:[/bold] Testing connectivity...\n")

    try:
        connected = asyncio.run(
            _test_connection(base_url, account_id, org_id, project_id, token)
        )
    except Exception as exc:
        console.print(f"  [red]Connection failed: {exc}[/red]")
        console.print("  Check your Account ID, API token, and network connectivity.")
        sys.exit(1)

    if not connected:
        console.print("  [yellow]Warning: Connection test returned unsuccessful.[/yellow]")
        console.print("  The profile will be saved, but check your API token.")
        proceed = input("  Continue anyway? [y/N]: ").strip().lower()
        if proceed != "y":
            console.print("  Setup cancelled.")
            sys.exit(1)
    else:
        console.print("  [green]Connection successful![/green]")

    # Step 3: Save profile
    console.print()
    console.print("[bold]Step 4:[/bold] Saving profile\n")

    profile_name = _prompt("Profile name", default="default")

    from config.settings import create_profile, set_active_profile
    from core.credentials import store_token

    try:
        create_profile(
            name=profile_name,
            account_id=account_id,
            org_identifier=org_id,
            project_identifier=project_id,
            base_url=base_url,
        )
    except ValueError as exc:
        console.print(f"  [yellow]{exc}[/yellow]")
        console.print("  Using existing profile.")

    store_token(profile_name, token)
    set_active_profile(profile_name)

    console.print(f"  Profile [bold]{profile_name}[/bold] saved and set as active.")

    # Done
    console.print()
    console.print(
        Panel(
            "[green bold]Setup complete![/green bold]\n\n"
            "Next steps:\n"
            "  1. Start the server:  [bold]make server[/bold]\n"
            "  2. Open VS Code Copilot Chat\n"
            "  3. Ask about your pipelines!",
            style="green",
        )
    )


def main() -> None:
    """Entry point with argument parsing."""
    parser = argparse.ArgumentParser(
        prog="hawkeye-setup",
        description="Interactive first-time setup wizard for Hawkeye",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Exit with usage info instead of running the wizard",
    )
    args = parser.parse_args()

    if args.non_interactive:
        parser.print_help()
        sys.exit(0)

    run_setup()


if __name__ == "__main__":
    main()
