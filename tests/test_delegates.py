"""Tests for tools/delegates.py — delegate health inspection tools."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from core.models import DelegateInfo, HarnessProfile

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

_NOW_MS = int(time.time() * 1000)


def _make_profile() -> HarnessProfile:
    return HarnessProfile(
        name="test",
        account_id="acc123",
        org_identifier="default",
        project_identifier="proj",
        base_url="https://app.harness.io",
        is_active=True,
    )


def _make_delegate(
    identifier: str = "dlgt1",
    name: str = "newad-eswd-tanr-dlgt",
    status: str = "CONNECTED",
    actively_connected: bool = True,
    last_heartbeat: int | None = None,
    delegate_type: str = "KUBERNETES",
    tags: list[str] | None = None,
    instances_connected: int = 2,
) -> DelegateInfo:
    return DelegateInfo(
        identifier=identifier,
        name=name,
        status=status,
        actively_connected=actively_connected,
        last_heartbeat=last_heartbeat if last_heartbeat is not None else _NOW_MS - 120_000,
        delegate_type=delegate_type,
        tags=tags or ["newad", "tanr"],
        instances_connected=instances_connected,
    )


def _mock_provider(delegates: list[DelegateInfo]) -> AsyncMock:
    provider = AsyncMock()
    provider.list_delegates = AsyncMock(return_value=delegates)
    return provider


# ------------------------------------------------------------------
# hawkeye_list_delegates
# ------------------------------------------------------------------


class TestListDelegates:
    @pytest.mark.asyncio
    async def test_list_delegates_formats_table(self) -> None:
        delegates = [
            _make_delegate(
                identifier="d1",
                name="newad-eswd-tanr-dlgt",
                status="CONNECTED",
                actively_connected=True,
                last_heartbeat=_NOW_MS - 120_000,
                tags=["newad", "tanr"],
                instances_connected=2,
            ),
            _make_delegate(
                identifier="d2",
                name="newad-eswd-tngo-dlgt",
                status="DISCONNECTED",
                actively_connected=False,
                last_heartbeat=_NOW_MS - 7200_000,
                tags=["newad", "tngo"],
                instances_connected=0,
            ),
            _make_delegate(
                identifier="d3",
                name="newad-eswd-taly-dlgt",
                status="UNKNOWN",
                actively_connected=False,
                last_heartbeat=None,
                tags=[],
                instances_connected=0,
            ),
        ]
        provider = _mock_provider(delegates)

        with patch("tools.delegates.get_provider", return_value=provider):
            from mcp.server.fastmcp import FastMCP

            from tools.delegates import register_delegate_tools

            mcp = FastMCP("test")
            register_delegate_tools(mcp)
            tool_fn = mcp._tool_manager._tools["hawkeye_list_delegates"].fn
            result = await tool_fn()

        # Should contain markdown table headers
        assert "| Name |" in result
        assert "| Status |" in result or "Status" in result
        # Connected delegate should have ✅
        assert "✅" in result
        # Disconnected delegate should have ❌
        assert "❌" in result
        # Delegate names should appear
        assert "newad-eswd-tanr-dlgt" in result
        assert "newad-eswd-tngo-dlgt" in result
        assert "newad-eswd-taly-dlgt" in result
        # Summary line
        assert "1/3" in result

    @pytest.mark.asyncio
    async def test_list_delegates_empty(self) -> None:
        provider = _mock_provider([])

        with patch("tools.delegates.get_provider", return_value=provider):
            from mcp.server.fastmcp import FastMCP

            from tools.delegates import register_delegate_tools

            mcp = FastMCP("test")
            register_delegate_tools(mcp)
            tool_fn = mcp._tool_manager._tools["hawkeye_list_delegates"].fn
            result = await tool_fn()

        assert "No delegates found" in result


# ------------------------------------------------------------------
# hawkeye_check_delegate
# ------------------------------------------------------------------


class TestCheckDelegate:
    @pytest.mark.asyncio
    async def test_check_delegate_exact_match(self) -> None:
        delegates = [
            _make_delegate(name="newad-eswd-tanr-dlgt", identifier="d1"),
            _make_delegate(name="newad-eswd-tngo-dlgt", identifier="d2"),
        ]
        provider = _mock_provider(delegates)

        with patch("tools.delegates.get_provider", return_value=provider):
            from mcp.server.fastmcp import FastMCP

            from tools.delegates import register_delegate_tools

            mcp = FastMCP("test")
            register_delegate_tools(mcp)
            tool_fn = mcp._tool_manager._tools["hawkeye_check_delegate"].fn
            result = await tool_fn(name="newad-eswd-tanr-dlgt")

        assert "newad-eswd-tanr-dlgt" in result
        assert "Status" in result
        assert "Connected" in result
        assert "Last Heartbeat" in result
        assert "Type" in result
        assert "Tags" in result
        assert "Identifier" in result

    @pytest.mark.asyncio
    async def test_check_delegate_fuzzy_match(self) -> None:
        delegates = [
            _make_delegate(name="newad-eswd-tanr-dlgt", identifier="d1"),
            _make_delegate(name="newad-eswd-tngo-dlgt", identifier="d2"),
            _make_delegate(name="newad-eswd-taly-dlgt", identifier="d3"),
        ]
        provider = _mock_provider(delegates)

        with patch("tools.delegates.get_provider", return_value=provider):
            from mcp.server.fastmcp import FastMCP

            from tools.delegates import register_delegate_tools

            mcp = FastMCP("test")
            register_delegate_tools(mcp)
            tool_fn = mcp._tool_manager._tools["hawkeye_check_delegate"].fn
            result = await tool_fn(name="tanr")

        # Should resolve "tanr" to "newad-eswd-tanr-dlgt"
        assert "newad-eswd-tanr-dlgt" in result
        assert "Status" in result

    @pytest.mark.asyncio
    async def test_check_delegate_no_match(self) -> None:
        delegates = [
            _make_delegate(name="newad-eswd-tanr-dlgt", identifier="d1"),
            _make_delegate(name="newad-eswd-tngo-dlgt", identifier="d2"),
        ]
        provider = _mock_provider(delegates)

        with patch("tools.delegates.get_provider", return_value=provider):
            from mcp.server.fastmcp import FastMCP

            from tools.delegates import register_delegate_tools

            mcp = FastMCP("test")
            register_delegate_tools(mcp)
            tool_fn = mcp._tool_manager._tools["hawkeye_check_delegate"].fn
            result = await tool_fn(name="nonexistent")

        assert "No delegate matching" in result
        # Should show available delegates
        assert "newad-eswd-tanr-dlgt" in result or "Available" in result

    @pytest.mark.asyncio
    async def test_check_delegate_disconnected_shows_warning(self) -> None:
        delegates = [
            _make_delegate(
                name="newad-eswd-tanr-dlgt",
                identifier="d1",
                actively_connected=False,
                status="DISCONNECTED",
                instances_connected=0,
            ),
        ]
        provider = _mock_provider(delegates)

        with patch("tools.delegates.get_provider", return_value=provider):
            from mcp.server.fastmcp import FastMCP

            from tools.delegates import register_delegate_tools

            mcp = FastMCP("test")
            register_delegate_tools(mcp)
            tool_fn = mcp._tool_manager._tools["hawkeye_check_delegate"].fn
            result = await tool_fn(name="newad-eswd-tanr-dlgt")

        assert "Warning" in result or "⚠️" in result
        assert "not actively connected" in result

    @pytest.mark.asyncio
    async def test_check_delegate_empty_name(self) -> None:
        provider = _mock_provider([])

        with patch("tools.delegates.get_provider", return_value=provider):
            from mcp.server.fastmcp import FastMCP

            from tools.delegates import register_delegate_tools

            mcp = FastMCP("test")
            register_delegate_tools(mcp)
            tool_fn = mcp._tool_manager._tools["hawkeye_check_delegate"].fn
            result = await tool_fn(name="")

        assert "Error" in result
