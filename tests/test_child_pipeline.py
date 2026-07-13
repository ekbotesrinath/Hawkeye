"""Tests for child pipeline following and Pipeline-type support.

Covers:
- extract_child_execution_id (parser)
- investigate_failure child pipeline following
- hawkeye_get_all_stages Pipeline-type indicator
- hawkeye_get_child_execution tool
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from core.models import (
    ExecutionStatus,
    FailureInfo,
    PipelineExecution,
    StageExecution,
    StepExecution,
)
from providers.harness.parser import extract_child_execution_id
from tools.executions import _is_pipeline_step_type
from tools.investigate import (
    _find_child_execution_id,
    _follow_child_pipelines,
    _format_investigation,
)

_NOW_MS = int(time.time() * 1000)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_step(
    step_id: str = "step1",
    name: str = "Build",
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    step_type: str = "Run",
    failure_info: FailureInfo | None = None,
) -> StepExecution:
    return StepExecution(
        step_id=step_id,
        name=name,
        identifier=step_id,
        status=status,
        step_type=step_type,
        start_ts=_NOW_MS - 60_000,
        end_ts=_NOW_MS,
        failure_info=failure_info,
    )


def _make_stage(
    stage_id: str = "stage1",
    name: str = "Build",
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    stage_type: str = "CI",
    steps: list[StepExecution] | None = None,
    failure_info: FailureInfo | None = None,
    identifier: str | None = None,
    child_execution_id: str | None = None,
) -> StageExecution:
    return StageExecution(
        stage_id=stage_id,
        name=name,
        identifier=identifier or stage_id,
        status=status,
        start_ts=_NOW_MS - 120_000,
        end_ts=_NOW_MS,
        stage_type=stage_type,
        failure_info=failure_info,
        steps=steps or [],
        child_execution_id=child_execution_id,
    )


def _make_execution(
    execution_id: str = "exec1",
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    stages: list[StageExecution] | None = None,
    pipeline_name: str = "build_js_monorepo",
    pipeline_id: str = "build_js_monorepo",
    run_sequence: int = 20751,
) -> PipelineExecution:
    return PipelineExecution(
        plan_execution_id=execution_id,
        pipeline_identifier=pipeline_id,
        pipeline_name=pipeline_name,
        status=status,
        start_ts=_NOW_MS - 600_000,
        end_ts=_NOW_MS,
        trigger_type="WEBHOOK",
        triggered_by="test-user",
        branch="main",
        run_sequence=run_sequence,
        execution_url=f"https://app.harness.io/ng/account/acc/module/ci/orgs/default/projects/proj/pipelines/{pipeline_id}/executions/{execution_id}/pipeline",
        stages=stages or [],
    )


# ==================================================================
# extract_child_execution_id
# ==================================================================


class TestExtractChildExecutionId:
    def test_returns_none_for_empty_data(self) -> None:
        assert extract_child_execution_id({}) is None

    def test_returns_none_for_none(self) -> None:
        assert extract_child_execution_id(None) is None  # type: ignore[arg-type]

    def test_extracts_from_outcomes_pipeline_execution_id(self) -> None:
        node = {
            "outcomes": {
                "output": {
                    "pipelineExecutionId": "child_exec_123",
                }
            }
        }
        assert extract_child_execution_id(node) == "child_exec_123"

    def test_extracts_from_outcomes_execution_url(self) -> None:
        node = {
            "outcomes": {
                "output": {
                    "executionUrl": (
                        "https://app.harness.io/ng/account/acc/module/ci"
                        "/orgs/default/projects/proj/pipelines/child_pipe"
                        "/executions/child_exec_456/pipeline"
                    ),
                }
            }
        }
        assert extract_child_execution_id(node) == "child_exec_456"

    def test_extracts_from_executable_responses_child(self) -> None:
        node = {
            "executableResponses": [
                {
                    "child": {
                        "childExecutionId": "child_exec_789",
                    }
                }
            ]
        }
        assert extract_child_execution_id(node) == "child_exec_789"

    def test_extracts_from_async_callback_ids(self) -> None:
        node = {
            "executableResponses": [
                {
                    "async": {
                        "callbackIds": ["child_exec_from_callback"],
                    }
                }
            ]
        }
        assert extract_child_execution_id(node) == "child_exec_from_callback"

    def test_returns_none_when_no_child_id_anywhere(self) -> None:
        node = {
            "name": "build_shared_libs",
            "stepType": "Pipeline",
            "status": "Failed",
            "outcomes": {"output": {"someOtherField": "value"}},
        }
        assert extract_child_execution_id(node) is None

    def test_url_parsing_with_query_params(self) -> None:
        node = {
            "outcomes": {
                "output": {
                    "executionUrl": (
                        "https://app.harness.io/ng/account/acc/module/ci"
                        "/orgs/default/projects/proj/pipelines/pipe"
                        "/executions/exec_with_params/pipeline?stage=Build"
                    ),
                }
            }
        }
        assert extract_child_execution_id(node) == "exec_with_params"

    def test_prefers_pipeline_execution_id_over_url(self) -> None:
        """Direct ID takes priority over URL parsing."""
        node = {
            "outcomes": {
                "output": {
                    "pipelineExecutionId": "direct_id",
                    "executionUrl": (
                        "https://app.harness.io/ng/account/acc/module/ci"
                        "/orgs/default/projects/proj/pipelines/pipe"
                        "/executions/url_id/pipeline"
                    ),
                }
            }
        }
        assert extract_child_execution_id(node) == "direct_id"

    def test_extracts_from_step_parameters(self) -> None:
        node = {
            "stepParameters": {
                "spec": {
                    "pipelineExecutionId": "params_exec_id",
                }
            }
        }
        assert extract_child_execution_id(node) == "params_exec_id"

    def test_skips_short_callback_ids(self) -> None:
        """Short callback IDs (≤10 chars) are not valid execution IDs."""
        node = {
            "executableResponses": [
                {
                    "async": {
                        "callbackIds": ["short"],
                    }
                }
            ]
        }
        assert extract_child_execution_id(node) is None


# ==================================================================
# _is_pipeline_step_type
# ==================================================================


class TestIsPipelineStepType:
    def test_pipeline_type(self) -> None:
        assert _is_pipeline_step_type("Pipeline") is True

    def test_pipeline_stage_type(self) -> None:
        assert _is_pipeline_step_type("PipelineStage") is True

    def test_case_insensitive(self) -> None:
        assert _is_pipeline_step_type("pipeline") is True
        assert _is_pipeline_step_type("PIPELINE") is True

    def test_pipeline_rollback(self) -> None:
        assert _is_pipeline_step_type("PipelineRollback") is True

    def test_non_pipeline_type(self) -> None:
        assert _is_pipeline_step_type("Run") is False
        assert _is_pipeline_step_type("ShellScript") is False
        assert _is_pipeline_step_type("") is False


# ==================================================================
# _find_child_execution_id (investigate helper)
# ==================================================================


class TestFindChildExecutionId:
    def test_finds_child_id_in_raw_graph(self) -> None:
        stage = _make_stage(
            stage_id="stg1",
            name="build_shared_libs",
            identifier="build_shared_libs",
            status=ExecutionStatus.FAILED,
            stage_type="Pipeline",
        )
        raw_node_map = {
            "uuid1": {
                "name": "build_shared_libs",
                "identifier": "build_shared_libs",
                "stepType": "Pipeline",
                "baseFqn": "pipeline.stages.build_shared_libs",
                "outcomes": {
                    "output": {
                        "pipelineExecutionId": "child_abc",
                    }
                },
            }
        }
        result = _find_child_execution_id(stage, raw_node_map)
        assert result == "child_abc"

    def test_returns_none_with_no_raw_node_map(self) -> None:
        stage = _make_stage(stage_type="Pipeline")
        assert _find_child_execution_id(stage, None) is None

    def test_returns_none_when_no_matching_node(self) -> None:
        stage = _make_stage(
            stage_id="stg1",
            identifier="build_shared_libs",
            stage_type="Pipeline",
        )
        raw_node_map = {
            "uuid1": {
                "name": "unrelated_stage",
                "identifier": "unrelated",
                "stepType": "Pipeline",
                "baseFqn": "pipeline.stages.unrelated",
            }
        }
        assert _find_child_execution_id(stage, raw_node_map) is None


# ==================================================================
# _follow_child_pipelines
# ==================================================================


class TestFollowChildPipelines:
    @pytest.mark.asyncio
    async def test_no_pipeline_stages_returns_empty(self) -> None:
        execution = _make_execution(
            stages=[
                _make_stage(status=ExecutionStatus.FAILED, stage_type="CI"),
            ]
        )
        result = await _follow_child_pipelines(execution, {}, AsyncMock())
        assert result == {}

    @pytest.mark.asyncio
    async def test_follows_failed_pipeline_stage(self) -> None:
        parent = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[
                _make_stage(
                    stage_id="stg1",
                    name="build_shared_libs",
                    identifier="build_shared_libs",
                    status=ExecutionStatus.FAILED,
                    stage_type="Pipeline",
                    failure_info=FailureInfo(message="exit status 1"),
                ),
            ],
        )

        child_step = _make_step(
            step_id="npm_build",
            name="npm_build",
            status=ExecutionStatus.FAILED,
            step_type="Run",
            failure_info=FailureInfo(message="npm ERR! Failed to compile"),
        )
        child_stage = _make_stage(
            stage_id="compile",
            name="compile_libs",
            status=ExecutionStatus.FAILED,
            stage_type="CI",
            steps=[child_step],
        )
        child_execution = _make_execution(
            execution_id="child_exec_1",
            status=ExecutionStatus.FAILED,
            stages=[child_stage],
            pipeline_name="build_shared_libs",
            run_sequence=42,
        )

        raw_node_map = {
            "uuid1": {
                "name": "build_shared_libs",
                "identifier": "build_shared_libs",
                "stepType": "Pipeline",
                "baseFqn": "pipeline.stages.build_shared_libs",
                "outcomes": {
                    "output": {
                        "pipelineExecutionId": "child_exec_1",
                    }
                },
            }
        }

        mock_provider = AsyncMock()
        mock_provider.get_execution = AsyncMock(return_value=child_execution)
        mock_provider.get_step_logs = AsyncMock(
            return_value={
                "node": {
                    "name": "npm_build",
                    "stepType": "Run",
                    "status": "Failed",
                    "failureInfo": {"message": "npm ERR! Failed to compile"},
                },
                "subgraph": None,
                "api_logs": "npm ERR! actual compiler output",
            }
        )

        findings = await _follow_child_pipelines(parent, raw_node_map, mock_provider)

        assert "stg1" in findings
        assert findings["stg1"]["child_execution"] == child_execution
        assert len(findings["stg1"]["child_failed_steps"]) == 1
        assert findings["stg1"]["child_failed_steps"][0][1] == "npm_build"
        detail = findings["stg1"]["child_step_details"]["compile/npm_build"]
        assert "Console Logs" in detail
        assert "npm ERR! actual compiler output" in detail

    @pytest.mark.asyncio
    async def test_handles_missing_child_id_gracefully(self) -> None:
        parent = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[
                _make_stage(
                    stage_id="stg1",
                    name="build_shared_libs",
                    identifier="build_shared_libs",
                    status=ExecutionStatus.FAILED,
                    stage_type="Pipeline",
                ),
            ],
        )
        raw_node_map = {
            "uuid1": {
                "name": "build_shared_libs",
                "identifier": "build_shared_libs",
                "stepType": "Pipeline",
                "baseFqn": "pipeline.stages.build_shared_libs",
                # No child execution ID
            }
        }

        mock_provider = AsyncMock()
        findings = await _follow_child_pipelines(parent, raw_node_map, mock_provider)
        assert findings == {}

    @pytest.mark.asyncio
    async def test_handles_provider_error_gracefully(self) -> None:
        parent = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[
                _make_stage(
                    stage_id="stg1",
                    name="build_shared_libs",
                    identifier="build_shared_libs",
                    status=ExecutionStatus.FAILED,
                    stage_type="Pipeline",
                ),
            ],
        )
        raw_node_map = {
            "uuid1": {
                "name": "build_shared_libs",
                "identifier": "build_shared_libs",
                "stepType": "Pipeline",
                "baseFqn": "pipeline.stages.build_shared_libs",
                "outcomes": {"output": {"pipelineExecutionId": "child_1"}},
            }
        }

        mock_provider = AsyncMock()
        mock_provider.get_execution = AsyncMock(side_effect=RuntimeError("API error"))

        findings = await _follow_child_pipelines(parent, raw_node_map, mock_provider)
        assert findings == {}

    @pytest.mark.asyncio
    async def test_includes_child_api_step_logs(self) -> None:
        """Child step console logs from the Harness API should be included."""
        parent = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[
                _make_stage(
                    stage_id="stg1",
                    name="build_sifi_adapter",
                    identifier="build_sifi_adapter",
                    status=ExecutionStatus.FAILED,
                    stage_type="Pipeline",
                    failure_info=FailureInfo(message="exit status 1"),
                ),
            ],
        )

        child_step = _make_step(
            step_id="sonar_scan",
            name="sonar_scan",
            status=ExecutionStatus.FAILED,
            step_type="Run",
            failure_info=FailureInfo(message="exit status 1"),
        )
        child_stage = _make_stage(
            stage_id="analysis",
            name="run_sonarqube_analysis",
            status=ExecutionStatus.FAILED,
            stage_type="CI",
            steps=[child_step],
        )
        child_execution = _make_execution(
            execution_id="child_exec_sonar",
            status=ExecutionStatus.FAILED,
            stages=[child_stage],
            pipeline_name="build_sifi_adapter",
            run_sequence=2246,
        )

        raw_node_map = {
            "uuid1": {
                "name": "build_sifi_adapter",
                "identifier": "build_sifi_adapter",
                "stepType": "Pipeline",
                "baseFqn": "pipeline.stages.build_sifi_adapter",
                "outcomes": {"output": {"pipelineExecutionId": "child_exec_sonar"}},
            }
        }

        mock_provider = AsyncMock()
        mock_provider.get_execution = AsyncMock(return_value=child_execution)
        mock_provider.get_step_logs = AsyncMock(
            return_value={
                "node": {
                    "name": "sonar_scan",
                    "stepType": "Run",
                    "status": "Failed",
                    "failureInfo": {"message": "exit status 1"},
                },
                "subgraph": None,
                "api_logs": "ERROR: SonarQube quality gate FAILED\nCoverage 42% < 80%",
            }
        )

        findings = await _follow_child_pipelines(parent, raw_node_map, mock_provider)

        assert "stg1" in findings
        detail = findings["stg1"]["child_step_details"]["analysis/sonar_scan"]
        assert "Console Logs" in detail
        assert "SonarQube quality gate FAILED" in detail


# ==================================================================
# _format_investigation with child findings
# ==================================================================


class TestFormatInvestigationWithChildFindings:
    def test_includes_child_pipeline_section(self) -> None:
        parent_stage = _make_stage(
            stage_id="stg1",
            name="build_shared_libs",
            status=ExecutionStatus.FAILED,
            stage_type="Pipeline",
            failure_info=FailureInfo(message="exit status 1"),
        )
        parent = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[parent_stage],
        )

        child_step = _make_step(
            step_id="npm_build",
            name="npm_build",
            status=ExecutionStatus.FAILED,
            step_type="Run",
            failure_info=FailureInfo(message="npm ERR! Failed to compile"),
        )
        child_stage = _make_stage(
            stage_id="compile",
            name="compile_libs",
            status=ExecutionStatus.FAILED,
            stage_type="CI",
            steps=[child_step],
        )
        child_execution = _make_execution(
            execution_id="child_exec_1",
            status=ExecutionStatus.FAILED,
            stages=[child_stage],
            pipeline_name="build_shared_libs",
            run_sequence=42,
        )

        child_findings = {
            "stg1": {
                "child_execution": child_execution,
                "child_failed_steps": [("compile", "npm_build", child_step)],
                "child_step_details": {
                    "compile/npm_build": "Step: npm_build (Run) — ❌ Failed"
                },
            }
        }

        result = _format_investigation(
            parent,
            [],  # No step-level failures in parent
            {},
            [],
            child_findings,
        )

        assert "Child Pipeline Details" in result
        assert "build_shared_libs" in result
        assert "Run #42" in result
        assert "npm_build" in result
        assert "npm ERR! Failed to compile" in result

    def test_no_child_findings_no_section(self) -> None:
        parent = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[
                _make_stage(status=ExecutionStatus.FAILED),
            ],
        )
        result = _format_investigation(parent, [], {}, [], None)
        assert "Child Pipeline Details" not in result

    def test_empty_child_findings_no_section(self) -> None:
        parent = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[
                _make_stage(status=ExecutionStatus.FAILED),
            ],
        )
        result = _format_investigation(parent, [], {}, [], {})
        assert "Child Pipeline Details" not in result


# ==================================================================
# hawkeye_get_all_stages Pipeline indicator
# ==================================================================


class TestParseChildExecutionIdFromLayout:
    """Layout nodes carry stepDetails.childPipelineExecutionDetails — the
    authoritative child execution reference for orchestrator pipelines."""

    @staticmethod
    def _orchestrator_response(node_extra: dict) -> dict:
        return {
            "pipelineExecutionSummary": {
                "planExecutionId": "parent1",
                "pipelineIdentifier": "orchestrator",
                "name": "orchestrator",
                "status": "Success",
                "startingNodeId": "n1",
                "layoutNodeMap": {
                    "n1": {
                        "nodeIdentifier": "deploy_child",
                        "name": "deploy_child",
                        "nodeType": "Pipeline",
                        "status": "Success",
                        "edgeLayoutList": {"nextIds": []},
                        **node_extra,
                    }
                },
            }
        }

    def test_child_execution_id_parsed(self) -> None:
        from providers.harness.parser import parse_execution_detail

        data = self._orchestrator_response({
            "stepDetails": {
                "childPipelineExecutionDetails": {
                    "planExecutionId": "child_plan_1",
                    "orgId": "platform_services",
                    "projectId": "Gail",
                }
            }
        })
        result = parse_execution_detail(data)
        assert result.stages[0].child_execution_id == "child_plan_1"

    def test_missing_step_details_gives_none(self) -> None:
        from providers.harness.parser import parse_execution_detail

        result = parse_execution_detail(self._orchestrator_response({}))
        assert result.stages[0].child_execution_id is None


class TestFollowChildPipelinesOrchestrator:
    """Orchestrator support: ALL executed Pipeline-type stages are followed,
    preferring the layout-node child execution ID over graph scanning."""

    @pytest.mark.asyncio
    async def test_follows_successful_pipeline_stage(self) -> None:
        parent = _make_execution(
            status=ExecutionStatus.SUCCESS,
            stages=[
                _make_stage(
                    stage_id="stg1",
                    name="deploy_sec_parser",
                    stage_type="Pipeline",
                    status=ExecutionStatus.SUCCESS,
                    child_execution_id="child_ok_1",
                ),
            ],
        )
        child_execution = _make_execution(
            execution_id="child_ok_1",
            status=ExecutionStatus.SUCCESS,
            stages=[_make_stage(stage_id="deploy", name="Deploy")],
            pipeline_name="cd_sec_parser",
        )
        mock_provider = AsyncMock()
        mock_provider.get_execution = AsyncMock(return_value=child_execution)

        findings = await _follow_child_pipelines(parent, {}, mock_provider)

        assert "stg1" in findings
        assert findings["stg1"]["child_execution"] == child_execution
        assert findings["stg1"]["child_failed_steps"] == []
        # No failed steps in the child — no step-log calls
        mock_provider.get_step_logs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_layout_child_id_preferred_over_graph_scan(self) -> None:
        parent = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[
                _make_stage(
                    stage_id="stg1",
                    name="deploy_child",
                    identifier="deploy_child",
                    stage_type="Pipeline",
                    status=ExecutionStatus.FAILED,
                    child_execution_id="layout_child_id",
                ),
            ],
        )
        # Graph carries a different (stale) reference — layout must win
        raw_node_map = {
            "uuid1": {
                "name": "deploy_child",
                "identifier": "deploy_child",
                "stepType": "Pipeline",
                "baseFqn": "pipeline.stages.deploy_child",
                "outcomes": {"output": {"pipelineExecutionId": "graph_child_id"}},
            }
        }
        child_execution = _make_execution(
            execution_id="layout_child_id", status=ExecutionStatus.FAILED
        )
        mock_provider = AsyncMock()
        mock_provider.get_execution = AsyncMock(return_value=child_execution)

        findings = await _follow_child_pipelines(parent, raw_node_map, mock_provider)

        mock_provider.get_execution.assert_awaited_once_with("layout_child_id")
        assert "stg1" in findings

    @pytest.mark.asyncio
    async def test_skipped_pipeline_stage_not_followed(self) -> None:
        parent = _make_execution(
            stages=[
                _make_stage(
                    stage_id="stg1",
                    stage_type="Pipeline",
                    status=ExecutionStatus.SKIPPED,
                ),
            ],
        )
        mock_provider = AsyncMock()
        findings = await _follow_child_pipelines(parent, {}, mock_provider)
        assert findings == {}
        mock_provider.get_execution.assert_not_awaited()

    def test_healthy_child_stages_rendered_in_report(self) -> None:
        child_execution = _make_execution(
            execution_id="child_ok_1",
            status=ExecutionStatus.SUCCESS,
            stages=[_make_stage(stage_id="deploy", name="Deploy SEC Parser")],
            pipeline_name="cd_sec_parser",
            run_sequence=88,
        )
        parent = _make_execution(
            status=ExecutionStatus.SUCCESS,
            stages=[
                _make_stage(
                    stage_id="stg1",
                    name="deploy_sec_parser",
                    stage_type="Pipeline",
                    child_execution_id="child_ok_1",
                ),
            ],
        )
        result = _format_investigation(
            parent,
            [],
            {},
            [],
            child_findings={
                "stg1": {
                    "child_execution": child_execution,
                    "child_failed_steps": [],
                    "child_step_details": {},
                }
            },
        )
        assert "Child Pipeline Details" in result
        assert "cd_sec_parser" in result
        assert "Run #88" in result
        assert "Deploy SEC Parser" in result


class TestChildIdSurfacedInFormatters:
    def test_format_execution_shows_child_id(self) -> None:
        from tools.executions import _format_execution

        execution = _make_execution(
            stages=[
                _make_stage(
                    stage_id="stg1",
                    name="deploy_child",
                    stage_type="Pipeline",
                    child_execution_id="child_abc",
                ),
            ],
        )
        result = _format_execution(execution)
        assert "child_abc" in result
        assert "hawkeye_get_child_execution" in result

    def test_format_all_stages_shows_child_id(self) -> None:
        from tools.executions import _format_all_stages

        execution = _make_execution(
            stages=[
                _make_stage(
                    stage_id="stg1",
                    name="deploy_child",
                    stage_type="Pipeline",
                    child_execution_id="child_abc",
                ),
            ],
        )
        result = _format_all_stages(execution, {})
        assert "↳ Child Pipeline" in result
        assert "child_abc" in result


class TestAllStagesPipelineIndicator:
    def test_format_all_stages_shows_pipeline_indicator(self) -> None:
        from tools.executions import _format_all_stages

        pipeline_stage = _make_stage(
            stage_id="stg1",
            name="build_shared_libs",
            status=ExecutionStatus.FAILED,
            stage_type="Pipeline",
            failure_info=FailureInfo(message="exit status 1"),
        )
        execution = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[pipeline_stage],
        )

        result = _format_all_stages(execution, {})
        assert "↳ Child Pipeline" in result
        assert "build_shared_libs" in result

    def test_non_pipeline_stage_no_indicator(self) -> None:
        from tools.executions import _format_all_stages

        ci_stage = _make_stage(
            stage_id="stg1",
            name="build",
            status=ExecutionStatus.SUCCESS,
            stage_type="CI",
        )
        execution = _make_execution(stages=[ci_stage])

        result = _format_all_stages(execution, {})
        assert "↳ Child Pipeline" not in result
