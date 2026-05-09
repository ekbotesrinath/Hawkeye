"""Tests for tools/pipelines.py — the 3 pipeline tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.models import (
    ExecutionStatus,
    InputSet,
    PipelineSummary,
)
from tools.pipelines import _format_input_sets, _format_pipeline_table

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_pipeline_summary(
    identifier: str = "ci_cd_pipeline",
    name: str = "CI/CD Pipeline",
    modules: list[str] | None = None,
    last_status: ExecutionStatus | None = ExecutionStatus.SUCCESS,
) -> PipelineSummary:
    return PipelineSummary(
        identifier=identifier,
        name=name,
        description="A test pipeline",
        modules=modules or ["CI", "CD"],
        last_execution_status=last_status,
        last_execution_ts=1_700_000_000_000,
        store_type="INLINE",
        repo_name=None,
        created=1_600_000_000_000,
        updated=1_700_000_000_000,
    )


def _make_input_set(
    identifier: str = "input_set_1",
    name: str = "Default Input Set",
    pipeline_id: str = "ci_cd_pipeline",
    yaml_content: str = "variables:\n  env: prod",
) -> InputSet:
    return InputSet(
        identifier=identifier,
        name=name,
        pipeline_identifier=pipeline_id,
        input_set_yaml=yaml_content,
    )


@pytest.fixture()
def mock_provider():
    provider = AsyncMock()
    provider.list_pipelines.return_value = []

    async def _get_provider():
        return provider

    with (
        patch("tools.pipelines.get_provider", side_effect=_get_provider),
        patch("tools._helpers.get_provider", side_effect=_get_provider),
        patch("tools._helpers.log_tool_call"),
    ):
        yield provider


# ------------------------------------------------------------------
# Pipeline table formatting
# ------------------------------------------------------------------


class TestFormatPipelineTable:
    def test_basic_table(self) -> None:
        pipelines = [_make_pipeline_summary()]
        result = _format_pipeline_table(pipelines)
        assert "Pipelines (1 found)" in result
        assert "CI/CD Pipeline" in result
        assert "ci_cd_pipeline" in result

    def test_multiple_pipelines(self) -> None:
        pipelines = [
            _make_pipeline_summary(identifier="p1", name="Pipeline A"),
            _make_pipeline_summary(identifier="p2", name="Pipeline B"),
        ]
        result = _format_pipeline_table(pipelines)
        assert "2 found" in result
        assert "Pipeline A" in result
        assert "Pipeline B" in result

    def test_no_last_status_shows_dash(self) -> None:
        pipelines = [_make_pipeline_summary(last_status=None)]
        result = _format_pipeline_table(pipelines)
        assert "—" in result

    def test_modules_displayed(self) -> None:
        pipelines = [_make_pipeline_summary(modules=["CI", "CD"])]
        result = _format_pipeline_table(pipelines)
        assert "CI, CD" in result


# ------------------------------------------------------------------
# Input sets formatting
# ------------------------------------------------------------------


class TestFormatInputSets:
    def test_basic_input_sets(self) -> None:
        input_sets = [_make_input_set()]
        result = _format_input_sets("pipeline_1", input_sets)
        assert "Input Sets for `pipeline_1`" in result
        assert "Default Input Set" in result
        assert "variables" in result

    def test_multiple_input_sets(self) -> None:
        input_sets = [
            _make_input_set(identifier="is1", name="Dev"),
            _make_input_set(identifier="is2", name="Prod"),
        ]
        result = _format_input_sets("p1", input_sets)
        assert "2 found" in result
        assert "Dev" in result
        assert "Prod" in result


# ------------------------------------------------------------------
# Tool integration tests
# ------------------------------------------------------------------


class TestHawkeyeListPipelines:
    @pytest.mark.asyncio
    async def test_output_format(self, mock_provider) -> None:
        mock_provider.list_pipelines.return_value = [_make_pipeline_summary()]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_pipelines"].fn
        result = await tool_fn()
        assert "CI/CD Pipeline" in result

    @pytest.mark.asyncio
    async def test_with_search_filter(self, mock_provider) -> None:
        mock_provider.list_pipelines.return_value = [
            _make_pipeline_summary(name="Pipeline Alpha"),
        ]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_pipelines"].fn
        await tool_fn(search="Alpha")
        mock_provider.list_pipelines.assert_awaited_once_with(search="Alpha")

    @pytest.mark.asyncio
    async def test_no_pipelines_found(self, mock_provider) -> None:
        mock_provider.list_pipelines.return_value = []
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_pipelines"].fn
        result = await tool_fn()
        assert "No pipelines found" in result

    @pytest.mark.asyncio
    async def test_module_filter(self, mock_provider) -> None:
        mock_provider.list_pipelines.return_value = [
            _make_pipeline_summary(modules=["CI"]),
            _make_pipeline_summary(identifier="p2", name="CD Only", modules=["CD"]),
        ]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_pipelines"].fn
        result = await tool_fn(module="CI")
        assert "CI/CD Pipeline" in result
        assert "CD Only" not in result


class TestHawkeyeGetPipelineDefinition:
    @pytest.mark.asyncio
    async def test_returns_yaml(self, mock_provider) -> None:
        mock_provider.get_pipeline_definition.return_value = {
            "pipeline_yaml": "pipeline:\n  name: test"
        }
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_pipeline_definition"].fn
        result = await tool_fn(pipeline_id="pipe1")
        assert "pipeline:" in result
        assert "yaml" in result.lower()

    @pytest.mark.asyncio
    async def test_with_branch(self, mock_provider) -> None:
        mock_provider.get_pipeline_definition.return_value = {
            "pipeline_yaml": "pipeline:\n  name: test"
        }
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_pipeline_definition"].fn
        result = await tool_fn(pipeline_id="pipe1", branch="develop")
        mock_provider.get_pipeline_definition.assert_awaited_once_with("pipe1", branch="develop")
        assert "branch: develop" in result


class TestHawkeyeGetInputSets:
    @pytest.mark.asyncio
    async def test_listing(self, mock_provider) -> None:
        mock_provider.get_input_sets.return_value = [
            _make_input_set(identifier="is1", name="Dev Input Set"),
        ]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_input_sets"].fn
        result = await tool_fn(pipeline_id="pipe1")
        assert "Dev Input Set" in result
        assert "is1" in result

    @pytest.mark.asyncio
    async def test_no_input_sets(self, mock_provider) -> None:
        mock_provider.get_input_sets.return_value = []
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_input_sets"].fn
        result = await tool_fn(pipeline_id="pipe1")
        assert "No input sets found" in result
