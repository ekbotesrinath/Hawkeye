"""Tests for hawkeye_check_approvals tool."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from core.models import (
    ExecutionStatus,
    HarnessProfile,
    PipelineExecution,
    StageExecution,
    StepExecution,
)
from tools.executions import _format_approvals

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
) -> StepExecution:
    return StepExecution(
        step_id=step_id,
        name=name,
        identifier=step_id,
        status=status,
        step_type=step_type,
        start_ts=start_ts or _NOW_MS - 60_000,
        end_ts=end_ts or _NOW_MS,
    )


def _make_stage(
    stage_id: str = "stage1",
    name: str = "Build",
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    steps: list[StepExecution] | None = None,
) -> StageExecution:
    return StageExecution(
        stage_id=stage_id,
        name=name,
        identifier=stage_id,
        status=status,
        start_ts=_NOW_MS - 120_000,
        end_ts=_NOW_MS,
        stage_type="Approval",
        steps=steps or [],
    )


def _make_execution(
    execution_id: str = "exec1",
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    stages: list[StageExecution] | None = None,
) -> PipelineExecution:
    return PipelineExecution(
        plan_execution_id=execution_id,
        pipeline_identifier="cd_deploy_env",
        pipeline_name="Deploy Environment",
        status=status,
        start_ts=_NOW_MS - 600_000,
        end_ts=_NOW_MS,
        trigger_type="MANUAL",
        triggered_by="sekbote@ensono.com",
        branch="main",
        run_sequence=42,
        execution_url="https://app.harness.io/ng/account/acc123/module/cd/orgs/default/projects/proj/pipelines/cd_deploy_env/executions/exec1/pipeline",
        stages=stages or [],
    )


# ------------------------------------------------------------------
# Provider mock fixtures
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


# ------------------------------------------------------------------
# Unit tests for _format_approvals
# ------------------------------------------------------------------


class TestFormatApprovalsPending:
    """test_check_approvals_pending"""

    def test_pending_approval_shows_section(self) -> None:
        step = _make_step(
            step_id="approval1",
            name="Approve Prod Deploy",
            status=ExecutionStatus.APPROVAL_WAITING,
            step_type="HarnessApproval",
            start_ts=_NOW_MS - 300_000,
            end_ts=None,
        )
        stage = _make_stage(
            stage_id="approve_stage",
            name="Approval Gate",
            status=ExecutionStatus.APPROVAL_WAITING,
            steps=[step],
        )
        execution = _make_execution(
            status=ExecutionStatus.APPROVAL_WAITING,
            stages=[stage],
        )
        raw_node_map = {
            "approval1": {
                "stepType": "HarnessApproval",
                "status": "ApprovalWaiting",
                "startTs": _NOW_MS - 300_000,
                "stepParameters": {
                    "approvalMessage": "Please approve production deployment",
                    "approvers": {
                        "userGroups": ["NAD_Eng_Leads", "Admins"],
                    },
                },
            },
        }

        result = _format_approvals(execution, raw_node_map)

        assert "Pending Approvals" in result
        assert "Approve Prod Deploy" in result
        assert "Approval Gate" in result
        assert "Waiting for:" in result
        assert "Please approve production deployment" in result
        assert "NAD_Eng_Leads" in result
        assert "Admins" in result


class TestFormatApprovalsApproved:
    """test_check_approvals_approved"""

    def test_approved_shows_completed_section(self) -> None:
        step = _make_step(
            step_id="approval1",
            name="Approve Demo Deploy",
            status=ExecutionStatus.SUCCESS,
            step_type="HarnessApproval",
            start_ts=_NOW_MS - 600_000,
            end_ts=_NOW_MS - 300_000,
        )
        stage = _make_stage(
            name="Approval Gate",
            status=ExecutionStatus.SUCCESS,
            steps=[step],
        )
        execution = _make_execution(stages=[stage])
        raw_node_map = {
            "approval1": {
                "stepType": "HarnessApproval",
                "status": "Success",
                "startTs": _NOW_MS - 600_000,
                "endTs": _NOW_MS - 300_000,
                "stepParameters": {
                    "approvalMessage": "Approve demo",
                    "approvers": {"userGroups": ["Ensonos"]},
                },
            },
        }

        result = _format_approvals(execution, raw_node_map)

        assert "Completed Approvals" in result
        assert "Approve Demo Deploy" in result
        assert "Approved" in result


class TestFormatApprovalsRejected:
    """test_check_approvals_rejected"""

    def test_rejected_shows_rejected_section(self) -> None:
        step = _make_step(
            step_id="approval1",
            name="Approve Prod Deploy",
            status=ExecutionStatus.APPROVAL_REJECTED,
            step_type="HarnessApproval",
            start_ts=_NOW_MS - 600_000,
            end_ts=_NOW_MS - 300_000,
        )
        stage = _make_stage(
            name="Approval Gate",
            status=ExecutionStatus.APPROVAL_REJECTED,
            steps=[step],
        )
        execution = _make_execution(
            status=ExecutionStatus.APPROVAL_REJECTED,
            stages=[stage],
        )
        raw_node_map = {
            "approval1": {
                "stepType": "HarnessApproval",
                "status": "ApprovalRejected",
                "startTs": _NOW_MS - 600_000,
                "endTs": _NOW_MS - 300_000,
                "stepParameters": {
                    "approvalMessage": "Approve prod",
                    "approvers": {"userGroups": ["Admins"]},
                },
            },
        }

        result = _format_approvals(execution, raw_node_map)

        assert "Rejected Approvals" in result
        assert "Approve Prod Deploy" in result
        assert "Rejected" in result


class TestFormatApprovalsMixed:
    """test_check_approvals_mixed — 3 approval steps: pending, approved, rejected."""

    def test_all_three_sections_appear(self) -> None:
        steps_pending = [_make_step(
            step_id="appr_pending",
            name="Approve Prod",
            status=ExecutionStatus.APPROVAL_WAITING,
            step_type="HarnessApproval",
            start_ts=_NOW_MS - 120_000,
            end_ts=None,
        )]
        steps_approved = [_make_step(
            step_id="appr_approved",
            name="Approve Demo",
            status=ExecutionStatus.SUCCESS,
            step_type="HarnessApproval",
            start_ts=_NOW_MS - 600_000,
            end_ts=_NOW_MS - 500_000,
        )]
        steps_rejected = [_make_step(
            step_id="appr_rejected",
            name="Approve Perf",
            status=ExecutionStatus.APPROVAL_REJECTED,
            step_type="HarnessApproval",
            start_ts=_NOW_MS - 400_000,
            end_ts=_NOW_MS - 350_000,
        )]
        stages = [
            _make_stage(stage_id="s1", name="Demo Gate", status=ExecutionStatus.SUCCESS, steps=steps_approved),
            _make_stage(stage_id="s2", name="Perf Gate", status=ExecutionStatus.APPROVAL_REJECTED, steps=steps_rejected),
            _make_stage(stage_id="s3", name="Prod Gate", status=ExecutionStatus.APPROVAL_WAITING, steps=steps_pending),
        ]
        execution = _make_execution(
            status=ExecutionStatus.APPROVAL_WAITING,
            stages=stages,
        )
        raw_node_map = {
            "appr_pending": {
                "stepType": "HarnessApproval",
                "status": "ApprovalWaiting",
                "startTs": _NOW_MS - 120_000,
                "stepParameters": {
                    "approvalMessage": "Approve prod deploy",
                    "approvers": {"userGroups": ["Admins"]},
                },
            },
            "appr_approved": {
                "stepType": "HarnessApproval",
                "status": "Success",
                "startTs": _NOW_MS - 600_000,
                "endTs": _NOW_MS - 500_000,
                "stepParameters": {
                    "approvalMessage": "Approve demo",
                    "approvers": {"userGroups": ["NAD_Eng_Leads"]},
                },
            },
            "appr_rejected": {
                "stepType": "HarnessApproval",
                "status": "ApprovalRejected",
                "startTs": _NOW_MS - 400_000,
                "endTs": _NOW_MS - 350_000,
                "stepParameters": {
                    "approvalMessage": "Approve perf",
                    "approvers": {"userGroups": ["Ensonos"]},
                },
            },
        }

        result = _format_approvals(execution, raw_node_map)

        # All 3 sections must appear
        assert "Pending Approvals" in result
        assert "Completed Approvals" in result
        assert "Rejected Approvals" in result
        # Pending comes first
        pending_pos = result.index("Pending Approvals")
        completed_pos = result.index("Completed Approvals")
        rejected_pos = result.index("Rejected Approvals")
        assert pending_pos < completed_pos < rejected_pos


class TestFormatApprovalsNoApprovals:
    """test_check_approvals_no_approvals"""

    def test_no_approvals_returns_message(self) -> None:
        stage = _make_stage(
            name="Build",
            status=ExecutionStatus.SUCCESS,
            steps=[_make_step(step_type="Run")],
        )
        execution = _make_execution(stages=[stage])

        result = _format_approvals(execution, {})

        assert "No approval steps found" in result


class TestFormatApprovalsJiraType:
    """test_check_approvals_jira_type"""

    def test_jira_approval_detected(self) -> None:
        step = _make_step(
            step_id="jira_appr",
            name="Jira Approval",
            status=ExecutionStatus.APPROVAL_WAITING,
            step_type="JiraApproval",
            start_ts=_NOW_MS - 60_000,
            end_ts=None,
        )
        stage = _make_stage(
            name="Jira Gate",
            status=ExecutionStatus.APPROVAL_WAITING,
            steps=[step],
        )
        execution = _make_execution(
            status=ExecutionStatus.APPROVAL_WAITING,
            stages=[stage],
        )
        raw_node_map = {
            "jira_appr": {
                "stepType": "JiraApproval",
                "status": "ApprovalWaiting",
                "startTs": _NOW_MS - 60_000,
                "stepParameters": {
                    "approvalMessage": "Waiting for Jira ticket approval",
                    "approvers": {},
                },
            },
        }

        result = _format_approvals(execution, raw_node_map)

        assert "Pending Approvals" in result
        assert "Jira Approval" in result


class TestFormatApprovalsWaitingDuration:
    """test_check_approvals_waiting_duration"""

    def test_duration_formatted(self) -> None:
        # Set start 2h 15m ago
        start_ts = _NOW_MS - (2 * 3600 + 15 * 60) * 1000
        step = _make_step(
            step_id="appr1",
            name="Approve Deploy",
            status=ExecutionStatus.APPROVAL_WAITING,
            step_type="HarnessApproval",
            start_ts=start_ts,
            end_ts=None,
        )
        stage = _make_stage(
            name="Gate",
            status=ExecutionStatus.APPROVAL_WAITING,
            steps=[step],
        )
        execution = _make_execution(
            status=ExecutionStatus.APPROVAL_WAITING,
            stages=[stage],
        )
        raw_node_map = {
            "appr1": {
                "stepType": "HarnessApproval",
                "status": "ApprovalWaiting",
                "startTs": start_ts,
                "stepParameters": {
                    "approvalMessage": "",
                    "approvers": {"userGroups": ["Admins"]},
                },
            },
        }

        result = _format_approvals(execution, raw_node_map)

        assert "Waiting for:" in result
        # Should contain hours and minutes (2h 15m ...)
        assert "2h 15m" in result


# ------------------------------------------------------------------
# Integration tests (mocked provider via MCP tool)
# ------------------------------------------------------------------


class TestCheckApprovalsFromUrl:
    """test_check_approvals_from_url"""

    @pytest.mark.asyncio
    async def test_url_parsing(self, mock_provider, mock_audit) -> None:
        execution = _make_execution(
            execution_id="exec_from_url",
            status=ExecutionStatus.APPROVAL_WAITING,
            stages=[
                _make_stage(
                    name="Approval",
                    status=ExecutionStatus.APPROVAL_WAITING,
                    steps=[
                        _make_step(
                            step_id="appr1",
                            name="Approve",
                            status=ExecutionStatus.APPROVAL_WAITING,
                            step_type="HarnessApproval",
                        ),
                    ],
                ),
            ],
        )
        mock_provider.get_execution.return_value = execution
        mock_provider.get_raw_execution_graph.return_value = {
            "appr1": {
                "stepType": "HarnessApproval",
                "status": "ApprovalWaiting",
                "startTs": _NOW_MS - 60_000,
                "stepParameters": {
                    "approvalMessage": "Approve this",
                    "approvers": {"userGroups": ["Admins"]},
                },
            },
        }

        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_check_approvals"].fn
        url = "https://app.harness.io/ng/account/acc123/module/cd/orgs/default/projects/proj/pipelines/cd_deploy_env/executions/exec_from_url/pipeline"
        result = await tool_fn(url=url)

        assert "Pending Approvals" in result
        mock_provider.get_execution.assert_awaited_once_with("exec_from_url")


class TestCheckApprovalsIntegration:
    """Integration test — tool end-to-end with mocked provider."""

    @pytest.mark.asyncio
    async def test_no_id_or_url(self, mock_provider, mock_audit) -> None:
        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_check_approvals"].fn
        result = await tool_fn()
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_no_approvals_via_tool(self, mock_provider, mock_audit) -> None:
        execution = _make_execution(
            stages=[_make_stage(steps=[_make_step(step_type="Run")])],
        )
        mock_provider.get_execution.return_value = execution
        mock_provider.get_raw_execution_graph.return_value = {}

        from main import mcp

        tool_fn = mcp._tool_manager._tools["hawkeye_check_approvals"].fn
        result = await tool_fn(execution_id="exec1")
        assert "No approval steps found" in result
