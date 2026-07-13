"""Tests for main.py — MCP server initialization and tool registration."""

from __future__ import annotations

import logging

import pytest

from main import _TOOLSET_REGISTRY, create_server


class TestServerInit:
    """Test server initialization."""

    def test_mcp_server_exists(self) -> None:
        from main import mcp

        assert mcp is not None

    def test_mcp_server_name(self) -> None:
        from main import mcp

        assert mcp.name == "Hawkeye"

    def test_ping_tool_registered(self) -> None:
        from main import mcp

        tools = mcp._tool_manager._tools
        assert "ping" in tools

    def test_execution_tools_registered(self) -> None:
        from main import mcp

        tools = mcp._tool_manager._tools
        assert "hawkeye_get_execution" in tools
        assert "hawkeye_get_stage_detail" in tools
        assert "hawkeye_get_step_logs" in tools
        assert "hawkeye_get_execution_inputs" in tools
        assert "hawkeye_list_recent_executions" in tools
        assert "hawkeye_get_all_stages" in tools
        assert "hawkeye_get_child_execution" in tools
        assert "hawkeye_check_approvals" in tools

    def test_pipeline_tools_registered(self) -> None:
        from main import mcp

        tools = mcp._tool_manager._tools
        assert "hawkeye_get_pipeline_definition" in tools
        assert "hawkeye_list_pipelines" in tools
        assert "hawkeye_get_input_sets" in tools

    def test_account_tools_registered(self) -> None:
        from main import mcp

        tools = mcp._tool_manager._tools
        assert "hawkeye_connect_account" in tools
        assert "hawkeye_list_profiles" in tools

    def test_delegate_tools_registered(self) -> None:
        from main import mcp

        tools = mcp._tool_manager._tools
        assert "hawkeye_list_delegates" in tools
        assert "hawkeye_check_delegate" in tools

    def test_connector_tools_registered(self) -> None:
        from main import mcp

        tools = mcp._tool_manager._tools
        assert "hawkeye_list_connectors" in tools
        assert "hawkeye_check_connector" in tools

    def test_template_tools_registered(self) -> None:
        from main import mcp

        tools = mcp._tool_manager._tools
        assert "hawkeye_list_templates" in tools
        assert "hawkeye_get_template" in tools

    def test_secret_tools_registered(self) -> None:
        from main import mcp

        tools = mcp._tool_manager._tools
        assert "hawkeye_list_secrets" in tools
        assert "hawkeye_check_secret" in tools

    def test_environment_tools_registered(self) -> None:
        from main import mcp

        tools = mcp._tool_manager._tools
        assert "hawkeye_list_environments" in tools
        assert "hawkeye_get_environment" in tools
        assert "hawkeye_list_infrastructures" in tools
        assert "hawkeye_get_infrastructure" in tools
        assert "hawkeye_list_services" in tools
        assert "hawkeye_get_service" in tools

    def test_all_tools_registered(self) -> None:
        from main import mcp

        tools = mcp._tool_manager._tools
        # 8 execution + 3 pipeline + 2 template + 2 account + 4 investigation
        # + 1 deeplink + 2 terraform + 2 delegate + 2 connector + 2 secrets
        # + 6 environments + 1 ping
        assert len(tools) == 35

    @pytest.mark.asyncio
    async def test_ping_returns_status(self) -> None:
        from main import mcp

        tool_fn = mcp._tool_manager._tools["ping"].fn
        result = await tool_fn()
        assert "running" in result.lower()

    def test_tools_have_descriptions(self) -> None:
        from main import mcp

        for name, tool in mcp._tool_manager._tools.items():
            assert tool.description, f"Tool '{name}' has no description"

    @pytest.mark.asyncio
    async def test_no_profile_returns_helpful_error(self) -> None:
        """Verify get_provider raises a helpful error when no profile is set."""
        from unittest.mock import patch

        from tools._helpers import get_provider, reset_provider

        reset_provider()
        with (
            patch("tools._helpers.get_active_profile", return_value=None),
            pytest.raises(RuntimeError, match="No active Hawkeye profile"),
        ):
            await get_provider()


class TestToolsetFiltering:
    """Test HAWKEYE_TOOLSETS environment variable filtering."""

    def test_all_toolsets_registered_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With HAWKEYE_TOOLSETS not set, all tools are registered."""
        monkeypatch.delenv("HAWKEYE_TOOLSETS", raising=False)
        server = create_server()
        tools = server._tool_manager._tools
        # All 35 tools should be present
        assert len(tools) == 35
        assert "hawkeye_get_execution" in tools
        assert "hawkeye_diagnose" in tools
        assert "hawkeye_list_pipelines" in tools
        assert "hawkeye_list_templates" in tools
        assert "hawkeye_connect_account" in tools
        assert "hawkeye_build_link" in tools
        assert "hawkeye_list_delegates" in tools
        assert "hawkeye_list_connectors" in tools
        assert "hawkeye_check_secret" in tools
        assert "hawkeye_list_environments" in tools
        assert "ping" in tools

    def test_toolset_filtering(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only execution + investigation + account (always) + ping are registered."""
        monkeypatch.setenv("HAWKEYE_TOOLSETS", "execution,investigation")
        server = create_server()
        tools = server._tool_manager._tools

        # Execution tools present
        assert "hawkeye_get_execution" in tools
        assert "hawkeye_get_stage_detail" in tools

        # Investigation tools present
        assert "hawkeye_diagnose" in tools
        assert "hawkeye_investigate_failure" in tools

        # Account always present
        assert "hawkeye_connect_account" in tools
        assert "hawkeye_list_profiles" in tools

        # ping always present
        assert "ping" in tools

        # Pipeline, templates, deeplinks, terraform, delegates, connectors,
        # secrets, environments NOT present
        assert "hawkeye_list_pipelines" not in tools
        assert "hawkeye_list_templates" not in tools
        assert "hawkeye_get_template" not in tools
        assert "hawkeye_build_link" not in tools
        assert "hawkeye_parse_terraform_plan" not in tools
        assert "hawkeye_list_delegates" not in tools
        assert "hawkeye_list_connectors" not in tools
        assert "hawkeye_check_secret" not in tools
        assert "hawkeye_list_secrets" not in tools
        assert "hawkeye_list_environments" not in tools
        assert "hawkeye_get_service" not in tools

    def test_toolset_all_keyword(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HAWKEYE_TOOLSETS='all' registers everything."""
        monkeypatch.setenv("HAWKEYE_TOOLSETS", "all")
        server = create_server()
        tools = server._tool_manager._tools
        assert len(tools) == 35

    def test_toolset_invalid_name_ignored(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unknown toolset names are logged as warnings and skipped."""
        monkeypatch.setenv("HAWKEYE_TOOLSETS", "execution,nonexistent,investigation")
        with caplog.at_level(logging.WARNING, logger="main"):
            server = create_server()
        tools = server._tool_manager._tools

        # Valid toolsets registered
        assert "hawkeye_get_execution" in tools
        assert "hawkeye_diagnose" in tools

        # Warning logged for 'nonexistent'
        assert any("nonexistent" in r.message for r in caplog.records)

    def test_toolset_account_always_included(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Account toolset is always registered even if not explicitly listed."""
        monkeypatch.setenv("HAWKEYE_TOOLSETS", "execution")
        server = create_server()
        tools = server._tool_manager._tools
        assert "hawkeye_connect_account" in tools
        assert "hawkeye_list_profiles" in tools

    def test_toolset_templates_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HAWKEYE_TOOLSETS='templates' registers templates + account + ping only."""
        monkeypatch.setenv("HAWKEYE_TOOLSETS", "templates")
        server = create_server()
        tools = server._tool_manager._tools
        assert "hawkeye_list_templates" in tools
        assert "hawkeye_get_template" in tools
        assert "hawkeye_connect_account" in tools
        assert "ping" in tools
        assert "hawkeye_get_execution" not in tools
        assert "hawkeye_list_pipelines" not in tools

    def test_toolset_ping_always_registered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ping is always available regardless of toolset filter."""
        monkeypatch.setenv("HAWKEYE_TOOLSETS", "execution")
        server = create_server()
        tools = server._tool_manager._tools
        assert "ping" in tools

    def test_toolset_empty_string_means_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty HAWKEYE_TOOLSETS registers all toolsets."""
        monkeypatch.setenv("HAWKEYE_TOOLSETS", "")
        server = create_server()
        tools = server._tool_manager._tools
        assert len(tools) == 35

    def test_toolset_explicit_parameter_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Passing toolsets= to create_server() bypasses the env var."""
        monkeypatch.setenv("HAWKEYE_TOOLSETS", "all")
        server = create_server(toolsets={"execution"})
        tools = server._tool_manager._tools
        assert "hawkeye_get_execution" in tools
        assert "hawkeye_list_pipelines" not in tools
        assert "ping" in tools
