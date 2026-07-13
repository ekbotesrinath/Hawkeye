"""Tests for tools/deeplinks.py — deep link builder."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.models import HarnessProfile
from tools.deeplinks import register_deeplink_tools


def _make_profile() -> HarnessProfile:
    return HarnessProfile(
        name="test",
        account_id="acc123",
        org_identifier="default",
        project_identifier="proj",
        base_url="https://app.harness.io",
        is_active=True,
    )


@pytest.fixture()
def _patch_profile():
    """Patch get_active_profile to return a test profile."""
    with (
        patch("tools.deeplinks.get_active_profile", return_value=_make_profile()),
        patch("tools._helpers.sanitize", side_effect=lambda x: x),
    ):
        yield


@pytest.fixture()
def _patch_no_profile():
    """Patch get_active_profile to return None."""
    with (
        patch("tools.deeplinks.get_active_profile", return_value=None),
        patch("tools._helpers.sanitize", side_effect=lambda x: x),
    ):
        yield


class TestBuildLink:
    @pytest.mark.asyncio()
    @pytest.mark.usefixtures("_patch_profile")
    async def test_pipeline_only(self) -> None:
        """Build link with just pipeline_id."""
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test")
        register_deeplink_tools(mcp)

        tool_fn = mcp._tool_manager._tools["hawkeye_build_link"].fn
        result = await tool_fn(pipeline_id="ci")
        assert "ng/account/acc123" in result
        assert "pipelines/ci" in result
        assert "pipeline `ci`" in result
        assert "Harness Link" in result

    @pytest.mark.asyncio()
    @pytest.mark.usefixtures("_patch_profile")
    async def test_with_execution(self) -> None:
        """Build link with pipeline_id and execution_id."""
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test")
        register_deeplink_tools(mcp)

        tool_fn = mcp._tool_manager._tools["hawkeye_build_link"].fn
        result = await tool_fn(pipeline_id="my_pipeline", execution_id="exec456")
        assert "executions/exec456/pipeline" in result
        assert "execution `exec456`" in result

    @pytest.mark.asyncio()
    @pytest.mark.usefixtures("_patch_profile")
    async def test_with_stage_and_step(self) -> None:
        """Build link with stage and step query params."""
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test")
        register_deeplink_tools(mcp)

        tool_fn = mcp._tool_manager._tools["hawkeye_build_link"].fn
        result = await tool_fn(
            pipeline_id="my_pipeline",
            execution_id="exec456",
            stage_id="stage1",
            step_id="step1",
        )
        assert "?stage=stage1&step=step1" in result
        assert "step `step1`" in result

    @pytest.mark.asyncio()
    @pytest.mark.usefixtures("_patch_profile")
    async def test_stage_without_step(self) -> None:
        """Build link with stage but no step."""
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test")
        register_deeplink_tools(mcp)

        tool_fn = mcp._tool_manager._tools["hawkeye_build_link"].fn
        result = await tool_fn(
            pipeline_id="my_pipeline",
            execution_id="exec456",
            stage_id="stage1",
        )
        assert "?stage=stage1" in result
        assert "step=" not in result
        assert "stage `stage1`" in result

    @pytest.mark.asyncio()
    @pytest.mark.usefixtures("_patch_no_profile")
    async def test_no_profile_returns_error(self) -> None:
        """Returns error when no profile is configured."""
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test")
        register_deeplink_tools(mcp)

        tool_fn = mcp._tool_manager._tools["hawkeye_build_link"].fn
        result = await tool_fn(pipeline_id="my_pipeline")
        assert "No Hawkeye profile configured" in result
