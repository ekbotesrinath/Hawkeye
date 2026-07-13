"""Tests for hawkeye_watch_execution and hawkeye_get_flaky_steps."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from core.models import (
    ExecutionStatus,
    FailureInfo,
    PipelineExecution,
    StageExecution,
    StepExecution,
)
from tools.investigate import register_investigation_tools

_NOW_MS = int(time.time() * 1000)


def _make_step(
    name: str,
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    error: str = "",
) -> StepExecution:
    return StepExecution(
        step_id=f"step_{name}",
        name=name,
        identifier=name,
        status=status,
        step_type="Run",
        start_ts=_NOW_MS - 600_000,
        end_ts=_NOW_MS - 500_000,
        failure_info=FailureInfo(message=error) if error else None,
    )


def _make_execution(
    status: ExecutionStatus,
    steps: list[StepExecution] | None = None,
    stage_status: ExecutionStatus | None = None,
    branch: str = "main",
    run_sequence: int = 1,
    eid: str = "exec_1",
) -> PipelineExecution:
    stage = StageExecution(
        stage_id="stage_1",
        name="Build",
        identifier="build",
        status=stage_status or status,
        steps=steps or [],
        start_ts=_NOW_MS - 900_000,
        end_ts=_NOW_MS - 300_000,
    )
    return PipelineExecution(
        plan_execution_id=eid,
        pipeline_identifier="pipe1",
        pipeline_name="Pipeline One",
        status=status,
        start_ts=_NOW_MS - 900_000,
        end_ts=None if status == ExecutionStatus.RUNNING else _NOW_MS - 200_000,
        run_sequence=run_sequence,
        branch=branch,
        stages=[stage],
    )


def _get_tool(name: str):
    mcp = FastMCP("test")
    register_investigation_tools(mcp)
    return mcp._tool_manager._tools[name].fn


class TestWatchExecution:
    @pytest.mark.asyncio
    async def test_polls_until_success(self) -> None:
        running = _make_execution(ExecutionStatus.RUNNING)
        done = _make_execution(ExecutionStatus.SUCCESS)
        provider = AsyncMock()
        provider.get_execution = AsyncMock(side_effect=[running, running, done])

        with (
            patch("tools.investigate.get_provider", return_value=provider),
            patch("tools.investigate.asyncio.sleep", new_callable=AsyncMock) as sleep,
        ):
            tool_fn = _get_tool("hawkeye_watch_execution")
            result = await tool_fn(execution_id="exec_1", poll_seconds=10)

        assert "SUCCEEDED" in result
        assert "2 poll(s)" in result
        assert provider.get_execution.await_count == 3
        assert sleep.await_count == 2

    @pytest.mark.asyncio
    async def test_immediate_return_for_completed_execution(self) -> None:
        done = _make_execution(ExecutionStatus.SUCCESS)
        provider = AsyncMock()
        provider.get_execution = AsyncMock(return_value=done)

        with (
            patch("tools.investigate.get_provider", return_value=provider),
            patch("tools.investigate.asyncio.sleep", new_callable=AsyncMock) as sleep,
        ):
            tool_fn = _get_tool("hawkeye_watch_execution")
            result = await tool_fn(execution_id="exec_1")

        assert "SUCCEEDED" in result
        assert sleep.await_count == 0

    @pytest.mark.asyncio
    async def test_failure_triggers_full_investigation(self) -> None:
        failed_step = _make_step("Deploy", ExecutionStatus.FAILED, "progress deadline")
        failed = _make_execution(ExecutionStatus.FAILED, steps=[failed_step])
        provider = AsyncMock()
        provider.get_execution = AsyncMock(return_value=failed)
        provider.list_recent_executions = AsyncMock(return_value=[failed])
        provider.get_raw_execution_graph = AsyncMock(return_value={})
        provider.get_step_logs = AsyncMock(
            return_value={"node": None, "subgraph": None}
        )

        with (
            patch("tools.investigate.get_provider", return_value=provider),
            patch("tools.investigate.asyncio.sleep", new_callable=AsyncMock),
        ):
            tool_fn = _get_tool("hawkeye_watch_execution")
            result = await tool_fn(execution_id="exec_1")

        assert "Watch complete: FAILED" in result
        assert "Hawkeye Failure Investigation" in result
        assert "Root Cause" in result

    @pytest.mark.asyncio
    async def test_timeout_reports_current_state(self) -> None:
        running_stage = StageExecution(
            stage_id="s1",
            name="Deploy",
            identifier="deploy",
            status=ExecutionStatus.RUNNING,
            start_ts=_NOW_MS - 300_000,
        )
        running = PipelineExecution(
            plan_execution_id="exec_1",
            pipeline_identifier="pipe1",
            pipeline_name="Pipeline One",
            status=ExecutionStatus.RUNNING,
            start_ts=_NOW_MS - 900_000,
            run_sequence=5,
            stages=[running_stage],
        )
        provider = AsyncMock()
        provider.get_execution = AsyncMock(return_value=running)

        # Fake clock: each mocked sleep advances monotonic time, so the
        # 1-minute deadline is genuinely exceeded after two 45s polls.
        clock = {"t": 1000.0}

        async def fake_sleep(secs: float) -> None:
            clock["t"] += secs

        with (
            patch("tools.investigate.get_provider", return_value=provider),
            patch("tools.investigate.asyncio.sleep", side_effect=fake_sleep),
            patch("tools.investigate.time.monotonic", side_effect=lambda: clock["t"]),
        ):
            tool_fn = _get_tool("hawkeye_watch_execution")
            result = await tool_fn(
                execution_id="exec_1", poll_seconds=45, timeout_minutes=1
            )

        assert "Watch timed out" in result
        assert "still" in result
        assert "Deploy" in result  # current running stage surfaced

    @pytest.mark.asyncio
    async def test_requires_execution_id(self) -> None:
        provider = AsyncMock()
        with patch("tools.investigate.get_provider", return_value=provider):
            tool_fn = _get_tool("hawkeye_watch_execution")
            result = await tool_fn()
        assert "Error" in result


class TestFlakySteps:
    def _runs_with_flaky_step(self) -> list[PipelineExecution]:
        """5 runs: 'Test' fails in 2 of 5 across branches, 'Lint' always passes."""
        runs = []
        for i in range(5):
            test_failed = i in (1, 3)
            steps = [
                _make_step("Lint", ExecutionStatus.SUCCESS),
                _make_step(
                    "Test",
                    ExecutionStatus.FAILED if test_failed else ExecutionStatus.SUCCESS,
                    error="AssertionError: flaky" if test_failed else "",
                ),
            ]
            runs.append(
                _make_execution(
                    ExecutionStatus.FAILED if test_failed else ExecutionStatus.SUCCESS,
                    steps=steps,
                    stage_status=(
                        ExecutionStatus.FAILED if test_failed else ExecutionStatus.SUCCESS
                    ),
                    branch="main" if i % 2 == 0 else "develop",
                    run_sequence=i + 1,
                    eid=f"exec_{i}",
                )
            )
        return runs

    @pytest.mark.asyncio
    async def test_detects_flaky_step(self) -> None:
        runs = self._runs_with_flaky_step()
        provider = AsyncMock()
        provider.list_pipelines = AsyncMock(return_value=[])
        provider.list_recent_executions = AsyncMock(return_value=runs)
        provider.get_execution = AsyncMock(side_effect=runs)

        with (
            patch("tools.investigate.get_provider", return_value=provider),
            patch("tools._helpers.get_provider", return_value=provider),
        ):
            tool_fn = _get_tool("hawkeye_get_flaky_steps")
            result = await tool_fn(pipeline_id="pipe1")

        assert "Flaky steps" in result
        assert "**Test**" in result
        assert "40%" in result  # 2/5
        assert "AssertionError: flaky" in result
        # Lint never failed — must not appear as flaky
        assert "**Lint**" not in result

    @pytest.mark.asyncio
    async def test_branch_specific_failures_noted(self) -> None:
        """Step fails on both branches → 'regardless of branch'."""
        runs = self._runs_with_flaky_step()
        provider = AsyncMock()
        provider.list_pipelines = AsyncMock(return_value=[])
        provider.list_recent_executions = AsyncMock(return_value=runs)
        provider.get_execution = AsyncMock(side_effect=runs)

        with (
            patch("tools.investigate.get_provider", return_value=provider),
            patch("tools._helpers.get_provider", return_value=provider),
        ):
            tool_fn = _get_tool("hawkeye_get_flaky_steps")
            result = await tool_fn(pipeline_id="pipe1")

        # i=1 → develop, i=3 → develop... runs 1,3 both i%2==1 → develop only
        assert "only on `develop`" in result

    @pytest.mark.asyncio
    async def test_consistently_broken_step(self) -> None:
        runs = []
        for i in range(3):
            steps = [
                _make_step("Deploy", ExecutionStatus.FAILED, error="ImagePullBackOff"),
            ]
            runs.append(
                _make_execution(
                    ExecutionStatus.FAILED,
                    steps=steps,
                    run_sequence=i + 1,
                    eid=f"exec_{i}",
                )
            )
        provider = AsyncMock()
        provider.list_pipelines = AsyncMock(return_value=[])
        provider.list_recent_executions = AsyncMock(return_value=runs)
        provider.get_execution = AsyncMock(side_effect=runs)

        with (
            patch("tools.investigate.get_provider", return_value=provider),
            patch("tools._helpers.get_provider", return_value=provider),
        ):
            tool_fn = _get_tool("hawkeye_get_flaky_steps")
            result = await tool_fn(pipeline_id="pipe1")

        assert "Consistently broken" in result
        assert "**Deploy**" in result
        assert "ImagePullBackOff" in result

    @pytest.mark.asyncio
    async def test_no_failures_at_all(self) -> None:
        runs = [
            _make_execution(
                ExecutionStatus.SUCCESS,
                steps=[_make_step("Build", ExecutionStatus.SUCCESS)],
                run_sequence=i + 1,
                eid=f"exec_{i}",
            )
            for i in range(3)
        ]
        provider = AsyncMock()
        provider.list_pipelines = AsyncMock(return_value=[])
        provider.list_recent_executions = AsyncMock(return_value=runs)
        provider.get_execution = AsyncMock(side_effect=runs)

        with (
            patch("tools.investigate.get_provider", return_value=provider),
            patch("tools._helpers.get_provider", return_value=provider),
        ):
            tool_fn = _get_tool("hawkeye_get_flaky_steps")
            result = await tool_fn(pipeline_id="pipe1")

        assert "No step failures at all" in result

    @pytest.mark.asyncio
    async def test_no_executions(self) -> None:
        provider = AsyncMock()
        provider.list_pipelines = AsyncMock(return_value=[])
        provider.list_recent_executions = AsyncMock(return_value=[])

        with (
            patch("tools.investigate.get_provider", return_value=provider),
            patch("tools._helpers.get_provider", return_value=provider),
        ):
            tool_fn = _get_tool("hawkeye_get_flaky_steps")
            result = await tool_fn(pipeline_id="pipe1")

        assert "No executions found" in result
