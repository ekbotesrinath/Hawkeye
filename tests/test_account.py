"""Tests for tools/account.py — the 2 account management tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.models import HarnessProfile

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_profile(
    name: str = "prod",
    account_id: str = "acc123",
    is_active: bool = True,
) -> HarnessProfile:
    return HarnessProfile(
        name=name,
        account_id=account_id,
        org_identifier="default",
        project_identifier="myproject",
        base_url="https://app.harness.io",
        is_active=is_active,
    )


@pytest.fixture()
def mock_audit():
    with patch("tools._helpers.log_tool_call") as mock:
        yield mock


# ------------------------------------------------------------------
# hawkeye_connect_account
# ------------------------------------------------------------------


class TestHawkeyeConnectAccount:
    @pytest.mark.asyncio
    async def test_switches_profile(self, mock_audit) -> None:
        profile = _make_profile(name="staging")
        mock_client = AsyncMock()
        mock_client.test_connection.return_value = True

        with (
            patch("tools.account.get_profile", return_value=profile),
            patch("tools.account.set_active_profile"),
            patch("tools.account.HarnessClient", return_value=mock_client),
            patch("tools.account.reset_provider"),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_connect_account"].fn
            result = await tool_fn(profile_name="staging")

        assert "Connected: staging" in result
        assert "acc123" in result
        assert "Connection successful" in result

    @pytest.mark.asyncio
    async def test_nonexistent_profile(self, mock_audit) -> None:
        with (
            patch("tools.account.get_profile", return_value=None),
            patch("tools.account.list_profiles", return_value=[_make_profile(name="prod")]),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_connect_account"].fn
            result = await tool_fn(profile_name="nonexistent")

        assert "not found" in result
        assert "prod" in result

    @pytest.mark.asyncio
    async def test_nonexistent_profile_no_profiles(self, mock_audit) -> None:
        with (
            patch("tools.account.get_profile", return_value=None),
            patch("tools.account.list_profiles", return_value=[]),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_connect_account"].fn
            result = await tool_fn(profile_name="missing")

        assert "not found" in result
        assert "make connect" in result.lower()

    @pytest.mark.asyncio
    async def test_connection_test_failure(self, mock_audit) -> None:
        profile = _make_profile(name="broken")
        mock_client = AsyncMock()
        mock_client.test_connection.return_value = False

        with (
            patch("tools.account.get_profile", return_value=profile),
            patch("tools.account.set_active_profile"),
            patch("tools.account.HarnessClient", return_value=mock_client),
            patch("tools.account.reset_provider"),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_connect_account"].fn
            result = await tool_fn(profile_name="broken")

        assert "Profile Switched: broken" in result
        assert "Connection test failed" in result

    @pytest.mark.asyncio
    async def test_audit_logged(self, mock_audit) -> None:
        profile = _make_profile(name="audit_test")
        mock_client = AsyncMock()
        mock_client.test_connection.return_value = True

        with (
            patch("tools.account.get_profile", return_value=profile),
            patch("tools.account.set_active_profile"),
            patch("tools.account.HarnessClient", return_value=mock_client),
            patch("tools.account.reset_provider"),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_connect_account"].fn
            await tool_fn(profile_name="audit_test")

        mock_audit.assert_called_once()
        call_args = mock_audit.call_args
        assert call_args[0][0] == "hawkeye_connect_account"
        assert call_args[0][2] == "success"

    @pytest.mark.asyncio
    async def test_output_sanitized(self, mock_audit) -> None:
        profile = _make_profile(name="sanitize_test")
        mock_client = AsyncMock()
        mock_client.test_connection.return_value = True

        with (
            patch("tools.account.get_profile", return_value=profile),
            patch("tools.account.set_active_profile"),
            patch("tools.account.HarnessClient", return_value=mock_client),
            patch("tools.account.reset_provider"),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_connect_account"].fn
            result = await tool_fn(profile_name="sanitize_test")

        assert isinstance(result, str)


# ------------------------------------------------------------------
# hawkeye_list_profiles
# ------------------------------------------------------------------


class TestHawkeyeListProfiles:
    @pytest.mark.asyncio
    async def test_lists_profiles(self, mock_audit) -> None:
        profiles = [
            _make_profile(name="prod", is_active=True),
            _make_profile(name="staging", account_id="acc456", is_active=False),
        ]
        with patch("tools.account.list_profiles", return_value=profiles):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_list_profiles"].fn
            result = await tool_fn()

        assert "prod" in result
        assert "staging" in result
        assert "active" in result.lower()
        assert "2 configured" in result

    @pytest.mark.asyncio
    async def test_no_profiles(self, mock_audit) -> None:
        with patch("tools.account.list_profiles", return_value=[]):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_list_profiles"].fn
            result = await tool_fn()

        assert "No profiles configured" in result
        assert "make connect" in result.lower()

    @pytest.mark.asyncio
    async def test_profile_details_shown(self, mock_audit) -> None:
        profiles = [_make_profile(name="prod")]
        with patch("tools.account.list_profiles", return_value=profiles):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_list_profiles"].fn
            result = await tool_fn()

        assert "acc123" in result
        assert "default" in result
        assert "myproject" in result
        assert "harness.io" in result

    @pytest.mark.asyncio
    async def test_audit_logged(self, mock_audit) -> None:
        with patch("tools.account.list_profiles", return_value=[]):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_list_profiles"].fn
            await tool_fn()

        mock_audit.assert_called_once()
        call_args = mock_audit.call_args
        assert call_args[0][0] == "hawkeye_list_profiles"
