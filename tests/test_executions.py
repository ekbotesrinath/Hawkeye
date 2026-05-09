"""Tests for tools/executions.py — the 5 core execution tools."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from core.models import (
    ExecutionStatus,
    FailureInfo,
    HarnessProfile,
    PipelineExecution,
    StageExecution,
    StepExecution,
)
from tools._helpers import format_duration_ms, format_relative_time, format_seconds, status_icon
from tools.executions import (
    _format_all_stages,
    _format_execution,
    _format_execution_table,
    _format_stage_detail,
)

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


def _make_step(
    step_id: str = "step1",
    name: str = "Build",
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    step_type: str = "Run",
    start_ts: int | None = None,
    end_ts: int | None = None,
    failure_info: FailureInfo | None = None,
    delegate_info: str | None = None,
) -> StepExecution:
    return StepExecution(
        step_id=step_id,
        name=name,
        identifier=step_id,
        status=status,
        step_type=step_type,
        start_ts=start_ts or _NOW_MS - 60_000,
        end_ts=end_ts or _NOW_MS,
        failure_info=failure_info,
        delegate_info=delegate_info,
    )


def _make_stage(
    stage_id: str = "stage1",
    name: str = "Build",
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    steps: list[StepExecution] | None = None,
    failure_info: FailureInfo | None = None,
) -> StageExecution:
    return StageExecution(
        stage_id=stage_id,
        name=name,
        identifier=stage_id,
        status=status,
        start_ts=_NOW_MS - 120_000,
        end_ts=_NOW_MS,
        stage_type="CI",
        failure_info=failure_info,
        steps=steps or [],
    )


def _make_execution(
    execution_id: str = "exec1",
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    stages: list[StageExecution] | None = None,
    branch: str | None = "main",
    run_sequence: int = 247,
) -> PipelineExecution:
    return PipelineExecution(
        plan_execution_id=execution_id,
        pipeline_identifier="ci_cd_pipeline",
        pipeline_name="ci_cd_AD",
        status=status,
        start_ts=_NOW_MS - 754_000,
        end_ts=_NOW_MS,
        trigger_type="MANUAL",
        triggered_by="sekbote@ensono.com",
        branch=branch,
        run_sequence=run_sequence,
        execution_url="https://app.harness.io/ng/account/acc123/module/cd/orgs/default/projects/proj/pipelines/ci_cd_pipeline/executions/exec1/pipeline",
        stages=stages or [],
    )


# ------------------------------------------------------------------
# Duration formatting
# ------------------------------------------------------------------


class TestDurationFormatting:
    def test_seconds_only(self) -> None:
        assert format_seconds(14) == "14s"

    def test_minutes_and_seconds(self) -> None:
        assert format_seconds(130) == "2m 10s"

    def test_hours_minutes_seconds(self) -> None:
        assert format_seconds(3922) == "1h 5m 22s"

    def test_zero_seconds(self) -> None:
        assert format_seconds(0) == "0s"

    def test_negative_returns_dash(self) -> None:
        assert format_seconds(-1) == "—"

    def test_format_duration_ms_none_start(self) -> None:
        assert format_duration_ms(None, _NOW_MS) == "—"

    def test_format_duration_ms_none_end_means_running(self) -> None:
        result = format_duration_ms(_NOW_MS - 60_000, None)
        # Should be roughly 60s (1m 0s)
        assert "s" in result

    def test_format_duration_ms_valid(self) -> None:
        result = format_duration_ms(_NOW_MS - 754_000, _NOW_MS)
        assert result == "12m 34s"


# ------------------------------------------------------------------
# Relative time formatting
# ------------------------------------------------------------------


class TestRelativeTimeFormatting:
    def test_just_now(self) -> None:
        assert format_relative_time(_NOW_MS) == "just now"

    def test_minutes_ago(self) -> None:
        result = format_relative_time(_NOW_MS - 300_000)  # 5 min ago
        assert result == "5m ago"

    def test_hours_ago(self) -> None:
        result = format_relative_time(_NOW_MS - 7_200_000)  # 2h ago
        assert result == "2h ago"

    def test_days_ago(self) -> None:
        result = format_relative_time(_NOW_MS - 86_400_000)  # 1d ago
        assert result == "1d ago"

    def test_none_returns_dash(self) -> None:
        assert format_relative_time(None) == "—"

    def test_future_returns_just_now(self) -> None:
        assert format_relative_time(_NOW_MS + 60_000) == "just now"


# ------------------------------------------------------------------
# Status icons
# ------------------------------------------------------------------


class TestStatusIcons:
    def test_success_icon(self) -> None:
        assert status_icon(ExecutionStatus.SUCCESS) == "✅"

    def test_failed_icon(self) -> None:
        assert status_icon(ExecutionStatus.FAILED) == "❌"

    def test_skipped_icon(self) -> None:
        assert status_icon(ExecutionStatus.SKIPPED) == "⏭️"

    def test_running_icon(self) -> None:
        assert status_icon(ExecutionStatus.RUNNING) == "🔄"

    def test_unknown_icon(self) -> None:
        assert status_icon(ExecutionStatus.UNKNOWN) == "❓"


# ------------------------------------------------------------------
# Execution formatting
# ------------------------------------------------------------------


class TestFormatExecution:
    def test_success_execution_basic(self) -> None:
        ex = _make_execution()
        result = _format_execution(ex)
        assert "ci_cd_AD" in result
        assert "Run #247" in result
        assert "SUCCESS" in result
        assert "sekbote@ensono.com" in result

    def test_failed_execution_shows_failure_info(self) -> None:
        fail_info = FailureInfo(
            message="Gradle build failed: compilation error in GatewayController.java",
            failure_types=["APPLICATION_FAILURE"],
        )
        failed_stage = _make_stage(
            name="Build_Gateway_Service",
            status=ExecutionStatus.FAILED,
            failure_info=fail_info,
        )
        ex = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[
                _make_stage(name="Setup", status=ExecutionStatus.SUCCESS),
                failed_stage,
                _make_stage(name="Deploy", status=ExecutionStatus.SKIPPED),
            ],
        )
        result = _format_execution(ex)
        assert "FAILED" in result
        assert "← FAILED" in result
        assert "APPLICATION_FAILURE" in result
        assert "Gradle build failed" in result

    def test_running_execution(self) -> None:
        ex = _make_execution(status=ExecutionStatus.RUNNING)
        result = _format_execution(ex)
        assert "RUNNING" in result

    def test_branch_in_output(self) -> None:
        ex = _make_execution(branch="release_26_2")
        result = _format_execution(ex)
        assert "release_26_2" in result

    def test_no_branch_omits_branch_info(self) -> None:
        ex = _make_execution(branch=None)
        result = _format_execution(ex)
        assert "Branch:" not in result

    def test_execution_url_in_output(self) -> None:
        ex = _make_execution()
        result = _format_execution(ex)
        assert "https://app.harness.io" in result

    def test_stages_count_in_header(self) -> None:
        ex = _make_execution(
            stages=[_make_stage(name="A"), _make_stage(name="B")],
        )
        result = _format_execution(ex)
        assert "2 total" in result

    def test_failed_count_in_header(self) -> None:
        ex = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[
                _make_stage(name="A", status=ExecutionStatus.SUCCESS),
                _make_stage(name="B", status=ExecutionStatus.FAILED),
            ],
        )
        result = _format_execution(ex)
        assert "1 failed" in result


# ------------------------------------------------------------------
# Stage detail formatting
# ------------------------------------------------------------------


class TestFormatStageDetail:
    def test_stage_with_steps(self) -> None:
        steps = [
            _make_step(name="Initialize", step_type="ShellScript"),
            _make_step(name="Build", step_type="Run"),
        ]
        stage = _make_stage(name="Build_Stage", steps=steps)
        ex = _make_execution(stages=[stage])
        result = _format_stage_detail(ex, stage)
        assert "Build_Stage" in result
        assert "Initialize" in result
        assert "ShellScript" in result
        assert "Build" in result

    def test_stage_not_found(self) -> None:
        from tools.executions import _find_stage

        ex = _make_execution(stages=[_make_stage(stage_id="stage1")])
        assert _find_stage(ex, "nonexistent") is None

    def test_stage_failure_info_shown(self) -> None:
        fail_info = FailureInfo(
            message="Build failed",
            failure_types=["APPLICATION_FAILURE"],
        )
        stage = _make_stage(
            name="Build",
            status=ExecutionStatus.FAILED,
            failure_info=fail_info,
        )
        ex = _make_execution(stages=[stage])
        result = _format_stage_detail(ex, stage)
        assert "Build failed" in result

    def test_no_steps_message(self) -> None:
        stage = _make_stage(name="Empty", steps=[])
        ex = _make_execution(stages=[stage])
        result = _format_stage_detail(ex, stage)
        assert "No step details available" in result

    def test_step_with_delegate(self) -> None:
        steps = [_make_step(name="Deploy", delegate_info="delegate-abc")]
        stage = _make_stage(name="Deploy_Stage", steps=steps)
        ex = _make_execution(stages=[stage])
        result = _format_stage_detail(ex, stage)
        assert "delegate-abc" in result


# ------------------------------------------------------------------
# Execution table formatting
# ------------------------------------------------------------------


class TestFormatExecutionTable:
    def test_table_header(self) -> None:
        executions = [_make_execution(run_sequence=247)]
        result = _format_execution_table("pipeline_1", executions, 10)
        assert "Recent Executions" in result
        assert "ci_cd_AD" in result

    def test_table_rows(self) -> None:
        executions = [
            _make_execution(run_sequence=247, status=ExecutionStatus.FAILED, branch="release"),
            _make_execution(run_sequence=246, status=ExecutionStatus.SUCCESS, branch="main"),
        ]
        result = _format_execution_table("pipeline_1", executions, 10)
        assert "247" in result
        assert "246" in result
        assert "release" in result
        assert "main" in result

    def test_empty_branch_shows_dash(self) -> None:
        executions = [_make_execution(branch=None)]
        result = _format_execution_table("p1", executions, 10)
        assert "—" in result


# ------------------------------------------------------------------
# Tool integration tests (with mocked provider)
# ------------------------------------------------------------------


@pytest.fixture()
def mock_provider():
    provider = AsyncMock()
    provider.list_pipelines.return_value = []

    async def _get_provider():
        return provider

    with (
        patch("tools.executions.get_provider", side_effect=_get_provider),
        patch("tools._helpers.get_provider", side_effect=_get_provider),
    ):
        yield provider


@pytest.fixture()
def mock_audit():
    with patch("tools._helpers.log_tool_call") as mock:
        yield mock


class TestHawkeyeGetExecution:
    @pytest.mark.asyncio
    async def test_with_execution_id(self, mock_provider, mock_audit) -> None:
        mock_provider.get_execution.return_value = _make_execution()
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_execution"].fn
        result = await tool_fn(execution_id="exec1")
        assert "ci_cd_AD" in result
        mock_provider.get_execution.assert_awaited_once_with("exec1")

    @pytest.mark.asyncio
    async def test_with_url(self, mock_provider, mock_audit) -> None:
        mock_provider.get_execution.return_value = _make_execution()
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_execution"].fn
        url = "https://app.harness.io/ng/account/acc123/module/cd/orgs/default/projects/proj/pipelines/pipe1/executions/exec_from_url/pipeline"
        result = await tool_fn(url=url)
        assert "ci_cd_AD" in result
        mock_provider.get_execution.assert_awaited_once_with("exec_from_url")

    @pytest.mark.asyncio
    async def test_with_url_containing_stage_step(self, mock_provider, mock_audit) -> None:
        mock_provider.get_execution.return_value = _make_execution()
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_execution"].fn
        url = "https://app.harness.io/ng/account/acc123/module/cd/orgs/default/projects/proj/pipelines/pipe1/executions/exec_url2/pipeline?stage=s1&step=st1"
        await tool_fn(url=url)
        mock_provider.get_execution.assert_awaited_once_with("exec_url2")

    @pytest.mark.asyncio
    async def test_no_id_or_url(self, mock_provider, mock_audit) -> None:
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_execution"].fn
        result = await tool_fn()
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_output_sanitized(self, mock_provider, mock_audit) -> None:
        ex = _make_execution()
        mock_provider.get_execution.return_value = ex
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_execution"].fn
        result = await tool_fn(execution_id="exec1")
        # Sanitize is called via run_tool; basic check it returned a string
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_audit_logged(self, mock_provider, mock_audit) -> None:
        mock_provider.get_execution.return_value = _make_execution()
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_execution"].fn
        await tool_fn(execution_id="exec1")
        mock_audit.assert_called_once()
        call_args = mock_audit.call_args
        assert call_args[0][0] == "hawkeye_get_execution"
        assert call_args[0][2] == "success"


class TestHawkeyeGetStageDetail:
    @pytest.mark.asyncio
    async def test_valid_stage(self, mock_provider, mock_audit) -> None:
        stage = _make_stage(stage_id="stg1", name="Build", steps=[_make_step()])
        mock_provider.get_execution.return_value = _make_execution(stages=[stage])
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_stage_detail"].fn
        result = await tool_fn(execution_id="exec1", stage_id="stg1")
        assert "Build" in result

    @pytest.mark.asyncio
    async def test_stage_not_found(self, mock_provider, mock_audit) -> None:
        mock_provider.get_execution.return_value = _make_execution(stages=[])
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_stage_detail"].fn
        result = await tool_fn(execution_id="exec1", stage_id="nonexistent")
        assert "not found" in result


class TestHawkeyeGetStepLogs:
    @pytest.mark.asyncio
    async def test_returns_step_details(self, mock_provider, mock_audit) -> None:
        mock_provider.get_step_logs.return_value = {
            "node": {
                "name": "Build",
                "stepType": "ShellScript",
                "status": "Success",
                "startTs": _NOW_MS - 60_000,
                "endTs": _NOW_MS,
                "outcomes": {"output": {"outputVariables": {"version": "1.0.0"}}},
            },
            "subgraph": {},
        }
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_step_logs"].fn
        result = await tool_fn(execution_id="e1", stage_id="s1", step_id="st1")
        assert "Step Details" in result
        assert "Build" in result

    @pytest.mark.asyncio
    async def test_step_not_found(self, mock_provider, mock_audit) -> None:
        mock_provider.get_step_logs.return_value = {
            "node": None,
            "subgraph": None,
            "available_nodes": [
                {
                    "identifier": "run_sonarqube",
                    "name": "Run SonarQube",
                    "step_type": "ShellScript",
                    "status": "Failed",
                    "base_fqn": "pipeline.stages.quality.spec.execution.steps.run_sonarqube",
                },
                {
                    "identifier": "get_version",
                    "name": "Get Version",
                    "step_type": "ShellScript",
                    "status": "Success",
                    "base_fqn": "pipeline.stages.versioning.spec.execution.steps.get_version",
                },
            ],
        }
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_step_logs"].fn
        result = await tool_fn(execution_id="e1", stage_id="s1", step_id="st1")
        assert "not found" in result
        assert "Available steps" in result
        assert "run_sonarqube" in result
        assert "get_version" in result
        assert "ShellScript" in result
        assert "\u274c" in result or "Failed" in result  # status icon or text
        assert "quality" in result  # stage hint from base_fqn

    @pytest.mark.asyncio
    async def test_step_not_found_no_available_nodes(self, mock_provider, mock_audit) -> None:
        """When graph is empty, just show not-found without available list."""
        mock_provider.get_step_logs.return_value = {
            "node": None,
            "subgraph": None,
            "available_nodes": [],
        }
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_step_logs"].fn
        result = await tool_fn(execution_id="e1", stage_id="s1", step_id="st1")
        assert "not found" in result
        assert "Available steps" not in result

    @pytest.mark.asyncio
    async def test_missing_step_id(self, mock_provider, mock_audit) -> None:
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_step_logs"].fn
        result = await tool_fn(execution_id="e1", stage_id="s1")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_secret_sanitization(self, mock_provider, mock_audit) -> None:
        mock_provider.get_step_logs.return_value = {
            "node": {
                "name": "Deploy",
                "stepType": "ShellScript",
                "status": "Success",
                "startTs": _NOW_MS - 60_000,
                "endTs": _NOW_MS,
            },
            "subgraph": {},
        }
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_step_logs"].fn
        result = await tool_fn(execution_id="e1", stage_id="s1", step_id="st1")
        # Sanitize is called via run_tool; basic check it returned a string
        assert isinstance(result, str)


class TestHawkeyeGetExecutionInputs:
    @pytest.mark.asyncio
    async def test_returns_yaml(self, mock_provider, mock_audit) -> None:
        mock_provider.get_execution_inputs.return_value = "pipeline:\n  variables:\n    env: prod"
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_execution_inputs"].fn
        result = await tool_fn(execution_id="exec1")
        assert "variables" in result
        assert "yaml" in result.lower()

    @pytest.mark.asyncio
    async def test_empty_inputs(self, mock_provider, mock_audit) -> None:
        mock_provider.get_execution_inputs.return_value = ""
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_execution_inputs"].fn
        result = await tool_fn(execution_id="exec1")
        assert "No input YAML" in result


class TestHawkeyeListRecentExecutions:
    @pytest.mark.asyncio
    async def test_table_format(self, mock_provider, mock_audit) -> None:
        mock_provider.list_recent_executions.return_value = [
            _make_execution(run_sequence=247, status=ExecutionStatus.FAILED),
            _make_execution(run_sequence=246, status=ExecutionStatus.SUCCESS),
        ]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_recent_executions"].fn
        result = await tool_fn(pipeline_id="pipe1")
        assert "Recent Executions" in result
        assert "247" in result
        assert "246" in result

    @pytest.mark.asyncio
    async def test_with_status_filter(self, mock_provider, mock_audit) -> None:
        mock_provider.list_recent_executions.return_value = [
            _make_execution(run_sequence=247, status=ExecutionStatus.FAILED),
        ]
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_recent_executions"].fn
        await tool_fn(pipeline_id="pipe1", status="Failed")
        mock_provider.list_recent_executions.assert_awaited_once()
        call_kwargs = mock_provider.list_recent_executions.call_args
        assert (
            call_kwargs[1].get("status") == ExecutionStatus.FAILED
            or call_kwargs[0][2] == ExecutionStatus.FAILED
        )

    @pytest.mark.asyncio
    async def test_no_executions(self, mock_provider, mock_audit) -> None:
        mock_provider.list_recent_executions.return_value = []
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_list_recent_executions"].fn
        result = await tool_fn(pipeline_id="pipe1")
        assert "No executions found" in result

    @pytest.mark.asyncio
    async def test_all_tools_audit_log(self, mock_provider, mock_audit) -> None:
        """Verify audit logging happens for execution tools."""
        mock_provider.get_execution.return_value = _make_execution()
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_execution"].fn
        await tool_fn(execution_id="exec1")
        assert mock_audit.called


# ------------------------------------------------------------------
# _format_all_stages tests
# ------------------------------------------------------------------


class TestFormatAllStages:
    def test_basic_success_stages(self) -> None:
        """Three successful stages render with step tables."""
        stages = [
            _make_stage(stage_id="s1", name="Setup", status=ExecutionStatus.SUCCESS, steps=[
                _make_step(name="Init", step_type="ShellScript"),
            ]),
            _make_stage(stage_id="s2", name="Build", status=ExecutionStatus.SUCCESS, steps=[
                _make_step(name="Compile", step_type="Run"),
                _make_step(name="Test", step_type="Run"),
            ]),
            _make_stage(stage_id="s3", name="Deploy", status=ExecutionStatus.SUCCESS, steps=[
                _make_step(name="Rollout", step_type="K8sRollingDeploy"),
            ]),
        ]
        ex = _make_execution(stages=stages)
        result = _format_all_stages(ex, {})
        assert "All Stages" in result
        assert "Setup" in result
        assert "Build" in result
        assert "Deploy" in result
        assert "Init" in result
        assert "Compile" in result
        assert "Rollout" in result
        assert "3 succeeded" in result

    def test_mixed_statuses(self) -> None:
        stages = [
            _make_stage(stage_id="s1", name="Setup", status=ExecutionStatus.SUCCESS),
            _make_stage(stage_id="s2", name="Build", status=ExecutionStatus.FAILED),
            _make_stage(stage_id="s3", name="Deploy", status=ExecutionStatus.SKIPPED),
        ]
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=stages)
        result = _format_all_stages(ex, {})
        assert "1 succeeded" in result
        assert "1 failed" in result
        assert "1 skipped" in result

    def test_skipped_collapse_more_than_3(self) -> None:
        """More than 3 consecutive skipped stages should be collapsed."""
        stages = [
            _make_stage(stage_id="s1", name="Setup", status=ExecutionStatus.SUCCESS),
        ] + [
            _make_stage(
                stage_id=f"skip{i}", name=f"Skipped_{i}",
                status=ExecutionStatus.SKIPPED,
            )
            for i in range(5)
        ]
        ex = _make_execution(stages=stages)
        result = _format_all_stages(ex, {})
        # Should show collapsed message, not 5 separate entries
        assert "5 stages" in result
        assert "⏭️ Skipped" in result

    def test_skipped_3_or_fewer_shown_individually(self) -> None:
        """3 or fewer consecutive skipped stages should be shown individually."""
        stages = [
            _make_stage(stage_id="s1", name="Setup", status=ExecutionStatus.SUCCESS),
            _make_stage(stage_id="skip1", name="QA", status=ExecutionStatus.SKIPPED),
            _make_stage(stage_id="skip2", name="Staging", status=ExecutionStatus.SKIPPED),
        ]
        ex = _make_execution(stages=stages)
        result = _format_all_stages(ex, {})
        assert "Stage: QA" in result
        assert "Stage: Staging" in result
        # Should NOT have collapsed message
        assert "stages)" not in result

    def test_failed_step_inline_details(self) -> None:
        """Failed steps should have inline details from detail_map."""
        steps = [
            _make_step(
                step_id="step_bad", name="Deploy", step_type="K8sRollingDeploy",
                status=ExecutionStatus.FAILED,
            ),
        ]
        stages = [
            _make_stage(stage_id="s1", name="Deploy", status=ExecutionStatus.FAILED, steps=steps),
        ]
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=stages)
        detail_map = {"s1/step_bad": "Step: Deploy (K8sRollingDeploy) — ❌ Failed (1m 0s)\nError:\n  Message: Timed out"}
        result = _format_all_stages(ex, detail_map)
        assert "Timed out" in result
        assert "1 failed" in result

    def test_empty_stages(self) -> None:
        ex = _make_execution(stages=[])
        result = _format_all_stages(ex, {})
        assert "All Stages" in result
        # Summary should still be there, just with zero counts
        assert "Summary:" in result

    def test_running_stage(self) -> None:
        stages = [
            _make_stage(stage_id="s1", name="Build", status=ExecutionStatus.RUNNING, steps=[
                _make_step(name="Compile", status=ExecutionStatus.RUNNING),
            ]),
        ]
        ex = _make_execution(status=ExecutionStatus.RUNNING, stages=stages)
        result = _format_all_stages(ex, {})
        assert "🔄" in result
        assert "1 running" in result

    def test_step_table_columns(self) -> None:
        """Step table must have Step, Status, Duration, Type columns."""
        stages = [
            _make_stage(stage_id="s1", name="Build", steps=[
                _make_step(name="Init", step_type="ShellScript"),
            ]),
        ]
        ex = _make_execution(stages=stages)
        result = _format_all_stages(ex, {})
        assert "| Step | Status | Duration | Type |" in result
        assert "ShellScript" in result

    def test_stage_type_label(self) -> None:
        """Stage output includes [CI] or [CD] type label."""
        stages = [
            _make_stage(stage_id="s1", name="Build", status=ExecutionStatus.SUCCESS),
        ]
        ex = _make_execution(stages=stages)
        result = _format_all_stages(ex, {})
        assert "[CI]" in result

    def test_details_unavailable_message(self) -> None:
        """When detail_map has "(details unavailable)" it should appear in output."""
        steps = [
            _make_step(
                step_id="step_fail", name="BadStep",
                status=ExecutionStatus.FAILED,
            ),
        ]
        stages = [
            _make_stage(stage_id="s1", name="FailStage", status=ExecutionStatus.FAILED, steps=steps),
        ]
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=stages)
        detail_map = {"s1/step_fail": "(details unavailable)"}
        result = _format_all_stages(ex, detail_map)
        assert "(details unavailable)" in result


# ------------------------------------------------------------------
# hawkeye_get_all_stages tool integration tests
# ------------------------------------------------------------------


class TestHawkeyeGetAllStages:
    @pytest.mark.asyncio
    async def test_returns_all_stages(self, mock_provider, mock_audit) -> None:
        stages = [
            _make_stage(stage_id="s1", name="Setup", status=ExecutionStatus.SUCCESS, steps=[
                _make_step(name="Init"),
            ]),
            _make_stage(stage_id="s2", name="Build", status=ExecutionStatus.SUCCESS, steps=[
                _make_step(name="Compile"),
            ]),
        ]
        mock_provider.get_execution.return_value = _make_execution(stages=stages)
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_all_stages"].fn
        result = await tool_fn(execution_id="exec1")
        assert "Setup" in result
        assert "Build" in result
        assert "Init" in result
        assert "Compile" in result

    @pytest.mark.asyncio
    async def test_with_url(self, mock_provider, mock_audit) -> None:
        mock_provider.get_execution.return_value = _make_execution(stages=[
            _make_stage(name="Build"),
        ])
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_all_stages"].fn
        url = "https://app.harness.io/ng/account/acc/module/cd/orgs/org/projects/proj/pipelines/pipe/executions/exec_url/pipeline"
        result = await tool_fn(url=url)
        assert "Build" in result
        mock_provider.get_execution.assert_awaited_once_with("exec_url")

    @pytest.mark.asyncio
    async def test_no_stages(self, mock_provider, mock_audit) -> None:
        mock_provider.get_execution.return_value = _make_execution(stages=[])
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_all_stages"].fn
        result = await tool_fn(execution_id="exec1")
        assert "No stages found" in result

    @pytest.mark.asyncio
    async def test_missing_params(self, mock_provider, mock_audit) -> None:
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_all_stages"].fn
        result = await tool_fn()
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_failed_step_detail_fetched(self, mock_provider, mock_audit) -> None:
        """Failed steps should trigger get_step_logs calls for detail."""
        failed_step = _make_step(
            step_id="bad_step", name="Deploy",
            status=ExecutionStatus.FAILED, step_type="K8sRollingDeploy",
        )
        stages = [
            _make_stage(stage_id="s1", name="Deploy", status=ExecutionStatus.FAILED, steps=[failed_step]),
        ]
        mock_provider.get_execution.return_value = _make_execution(
            status=ExecutionStatus.FAILED, stages=stages,
        )
        mock_provider.get_step_logs.return_value = {
            "node": {
                "name": "Deploy",
                "stepType": "K8sRollingDeploy",
                "status": "Failed",
                "startTs": _NOW_MS - 60_000,
                "endTs": _NOW_MS,
                "failureInfo": {"message": "Rollout timed out"},
            },
            "subgraph": {},
        }
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_all_stages"].fn
        result = await tool_fn(execution_id="exec1")
        assert "Deploy" in result
        mock_provider.get_step_logs.assert_awaited()

    @pytest.mark.asyncio
    async def test_failed_step_detail_fetch_error_resilience(self, mock_provider, mock_audit) -> None:
        """If get_step_logs raises, the tool should still return results."""
        failed_step = _make_step(
            step_id="bad_step", name="Deploy",
            status=ExecutionStatus.FAILED,
        )
        stages = [
            _make_stage(stage_id="s1", name="Deploy", status=ExecutionStatus.FAILED, steps=[failed_step]),
        ]
        mock_provider.get_execution.return_value = _make_execution(
            status=ExecutionStatus.FAILED, stages=stages,
        )
        mock_provider.get_step_logs.side_effect = RuntimeError("API error")
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_get_all_stages"].fn
        result = await tool_fn(execution_id="exec1")
        assert "Deploy" in result
        assert "(details unavailable)" in result
