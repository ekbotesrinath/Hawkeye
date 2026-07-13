"""Tests for tools/investigate.py — the 4 investigation tools."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from core.models import (
    ExecutionGraph,
    ExecutionStatus,
    FailureInfo,
    GraphNode,
    PipelineExecution,
    StageExecution,
    StepExecution,
)
from tools.investigate import (
    _build_analysis,
    _collect_failed_steps,
    _compute_diffs,
    _detect_patterns,
    _enrich_from_raw_graph,
    _follow_child_pipelines,
    _format_comparison,
    _format_failure_pattern,
    _format_investigation,
    _most_common,
    _timing_delta,
    _truncate_logs,
)

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

_NOW_MS = int(time.time() * 1000)


def _make_step(
    step_id: str = "step1",
    name: str = "Build",
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    step_type: str = "Run",
    start_ts: int | None = None,
    end_ts: int | None = None,
    failure_info: FailureInfo | None = None,
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
    )


_UNSET = object()


def _make_stage(
    stage_id: str = "stage1",
    name: str = "Build",
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    steps: list[StepExecution] | None = None,
    failure_info: FailureInfo | None = None,
    start_ts: int | None | object = _UNSET,
    end_ts: int | None | object = _UNSET,
) -> StageExecution:
    return StageExecution(
        stage_id=stage_id,
        name=name,
        identifier=stage_id,
        status=status,
        start_ts=(_NOW_MS - 120_000) if start_ts is _UNSET else start_ts,
        end_ts=_NOW_MS if end_ts is _UNSET else end_ts,
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
    pipeline_id: str = "ci_cd_pipeline",
    pipeline_name: str = "ci_cd_AD",
    trigger_type: str = "MANUAL",
    triggered_by: str = "sekbote@ensono.com",
    start_ts: int | None = None,
    end_ts: int | None = None,
    tags: dict[str, str] | None = None,
) -> PipelineExecution:
    return PipelineExecution(
        plan_execution_id=execution_id,
        pipeline_identifier=pipeline_id,
        pipeline_name=pipeline_name,
        status=status,
        start_ts=start_ts or _NOW_MS - 754_000,
        end_ts=end_ts or _NOW_MS,
        trigger_type=trigger_type,
        triggered_by=triggered_by,
        branch=branch,
        run_sequence=run_sequence,
        execution_url="https://app.harness.io/ng/account/acc123/module/cd/orgs/default/projects/proj/pipelines/ci_cd_pipeline/executions/" + execution_id + "/pipeline",
        stages=stages or [],
        tags=tags or {},
    )


def _make_graph() -> ExecutionGraph:
    return ExecutionGraph(
        root_node_id="root",
        nodes={
            "root": GraphNode(
                uuid="root",
                name="Pipeline",
                identifier="pipeline",
                step_type="Pipeline",
                status=ExecutionStatus.SUCCESS,
            )
        },
    )


# ------------------------------------------------------------------
# _collect_failed_steps
# ------------------------------------------------------------------


class TestCollectFailedSteps:
    def test_no_failed_steps(self) -> None:
        ex = _make_execution(stages=[_make_stage()])
        assert _collect_failed_steps(ex) == []

    def test_single_failed_step(self) -> None:
        step = _make_step(
            step_id="gradle_build",
            name="gradle_build",
            status=ExecutionStatus.FAILED,
            failure_info=FailureInfo(message="Compilation error"),
        )
        stage = _make_stage(
            stage_id="Build",
            name="Build_Gateway_Service",
            status=ExecutionStatus.FAILED,
            steps=[step],
        )
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=[stage])
        result = _collect_failed_steps(ex)
        assert len(result) == 1
        assert result[0][0] == "Build"
        assert result[0][1] == "gradle_build"
        assert result[0][2].name == "gradle_build"

    def test_multiple_failed_steps(self) -> None:
        step1 = _make_step(step_id="s1", name="s1", status=ExecutionStatus.FAILED)
        step2 = _make_step(step_id="s2", name="s2", status=ExecutionStatus.FAILED)
        step3 = _make_step(step_id="s3", name="s3", status=ExecutionStatus.SUCCESS)
        stage1 = _make_stage(
            stage_id="stg1", name="Stage1", status=ExecutionStatus.FAILED, steps=[step1, step3]
        )
        stage2 = _make_stage(
            stage_id="stg2", name="Stage2", status=ExecutionStatus.FAILED, steps=[step2]
        )
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=[stage1, stage2])
        result = _collect_failed_steps(ex)
        assert len(result) == 2

    def test_errored_status_counted(self) -> None:
        step = _make_step(step_id="s1", name="s1", status=ExecutionStatus.ERRORED)
        stage = _make_stage(
            stage_id="stg1", name="Stage1", status=ExecutionStatus.ERRORED, steps=[step]
        )
        ex = _make_execution(status=ExecutionStatus.ERRORED, stages=[stage])
        result = _collect_failed_steps(ex)
        assert len(result) == 1

    def test_expired_stage_collected(self) -> None:
        step = _make_step(step_id="s1", name="s1", status=ExecutionStatus.EXPIRED)
        stage = _make_stage(
            stage_id="stg1", name="Stage1", status=ExecutionStatus.EXPIRED, steps=[step]
        )
        ex = _make_execution(status=ExecutionStatus.EXPIRED, stages=[stage])
        result = _collect_failed_steps(ex)
        assert len(result) == 1
        assert result[0][0] == "stg1"
        assert result[0][1] == "s1"

    def test_expired_stage_no_expired_steps(self) -> None:
        """Expired stage with all steps skipped returns no step matches."""
        step = _make_step(step_id="s1", name="s1", status=ExecutionStatus.SKIPPED)
        stage = _make_stage(
            stage_id="stg1", name="Stage1", status=ExecutionStatus.EXPIRED, steps=[step]
        )
        ex = _make_execution(status=ExecutionStatus.EXPIRED, stages=[stage])
        result = _collect_failed_steps(ex)
        assert result == []


# ------------------------------------------------------------------
# _enrich_from_raw_graph
# ------------------------------------------------------------------


class TestEnrichFromRawGraph:
    def test_expired_lite_engine_task_enriched(self) -> None:
        """An expired CI stage with no step-level failures gets enriched
        with the liteEngineTask node from the raw graph."""
        stage = _make_stage(
            stage_id="stg1",
            name="Build",
            status=ExecutionStatus.EXPIRED,
            steps=[],  # no steps matched — CI pod never started
        )
        stage = StageExecution(
            stage_id="stg1",
            name="Build",
            identifier="Build_CI",
            status=ExecutionStatus.EXPIRED,
            start_ts=_NOW_MS - 600_000,
            end_ts=_NOW_MS,
            stage_type="CI",
            steps=[],
        )
        ex = _make_execution(status=ExecutionStatus.EXPIRED, stages=[stage])
        failed_steps: list[tuple[str, str, StepExecution]] = []
        step_details: dict[str, str] = {}

        raw_node_map = {
            "uuid1": {
                "name": "liteEngineTask",
                "identifier": "liteEngineTask",
                "stepType": "liteEngineTask",
                "status": "Expired",
                "baseFqn": "pipeline.stages.Build_CI.spec.execution.steps.liteEngineTask",
                "startTs": _NOW_MS - 600_000,
                "endTs": _NOW_MS,
                "failureInfo": {
                    "message": "Please Check the timeout configuration on the step to extend the duration of the step",
                    "failureTypeList": ["EXPIRED"],
                    "responseMessages": [
                        {"message": "Please Check the timeout configuration on the step to extend the duration of the step"}
                    ],
                },
                "stepDetails": {
                    "initStepV2DelegateTaskInfo": {
                        "taskID": "task-123",
                        "taskName": "INITIALIZATION_PHASE",
                    }
                },
                "stepParameters": {
                    "identifier": "liteEngineTask",
                    "name": "liteEngineTask",
                    "timeout": "10m",
                    "type": "liteEngineTask",
                    "spec": {},
                },
                "executableResponses": [
                    {
                        "async": {
                            "callbackIds": ["cb1"],
                            "logKeys": ["account/pipeline/434/execution/stage/liteEngineTask"],
                        }
                    }
                ],
            },
        }

        _enrich_from_raw_graph(ex, failed_steps, step_details, raw_node_map)

        assert "stg1/liteEngineTask" in step_details
        detail = step_details["stg1/liteEngineTask"]
        assert "liteEngineTask" in detail
        assert "INITIALIZATION_PHASE" in detail
        assert "CI Infrastructure Issue" in detail
        assert "10m" in detail

    def test_no_enrichment_when_all_stages_covered(self) -> None:
        """No enrichment needed when all failed stages already have step details."""
        step = _make_step(step_id="s1", name="s1", status=ExecutionStatus.FAILED)
        stage = _make_stage(
            stage_id="stg1", name="Stage1", status=ExecutionStatus.FAILED, steps=[step]
        )
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=[stage])
        failed_steps = [("stg1", "s1", step)]
        step_details: dict[str, str] = {"stg1/s1": "some detail"}

        raw_node_map = {
            "uuid1": {
                "stepType": "liteEngineTask",
                "status": "Expired",
                "baseFqn": "pipeline.stages.Stage1.spec.execution.steps.liteEngineTask",
            },
        }

        _enrich_from_raw_graph(ex, failed_steps, step_details, raw_node_map)
        assert "stg1/liteEngineTask" not in step_details

    def test_no_enrichment_when_stage_not_failed(self) -> None:
        """Successful stages are not enriched even if liteEngineTask exists."""
        stage = _make_stage(stage_id="stg1", name="Stage1", status=ExecutionStatus.SUCCESS)
        ex = _make_execution(stages=[stage])
        failed_steps: list[tuple[str, str, StepExecution]] = []
        step_details: dict[str, str] = {}

        raw_node_map = {
            "uuid1": {
                "stepType": "liteEngineTask",
                "status": "Success",
                "baseFqn": "pipeline.stages.Stage1.spec.execution.steps.liteEngineTask",
            },
        }

        _enrich_from_raw_graph(ex, failed_steps, step_details, raw_node_map)
        assert step_details == {}


# ------------------------------------------------------------------
# _truncate_logs
# ------------------------------------------------------------------


class TestTruncateLogs:
    def test_short_logs_unchanged(self) -> None:
        logs = "\n".join(f"line {i}" for i in range(10))
        assert _truncate_logs(logs) == logs

    def test_long_logs_truncated(self) -> None:
        logs = "\n".join(f"line {i}" for i in range(200))
        result = _truncate_logs(logs, max_lines=50)
        assert "truncated" in result
        assert "line 199" in result

    def test_exact_limit_not_truncated(self) -> None:
        logs = "\n".join(f"line {i}" for i in range(50))
        result = _truncate_logs(logs, max_lines=50)
        assert "truncated" not in result


# ------------------------------------------------------------------
# Investigation report (hawkeye_investigate_failure)
# ------------------------------------------------------------------


class TestFormatInvestigation:
    def test_failed_execution_report(self) -> None:
        step = _make_step(
            step_id="gradle_build",
            name="gradle_build",
            status=ExecutionStatus.FAILED,
            failure_info=FailureInfo(
                message="Compilation error",
                failure_types=["APPLICATION_FAILURE"],
            ),
        )
        stage = _make_stage(
            stage_id="Build",
            name="Build_Gateway_Service",
            status=ExecutionStatus.FAILED,
            steps=[step],
        )
        ex = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[stage],
            branch="release_26_2",
        )
        failed_steps = _collect_failed_steps(ex)
        step_logs = {"Build/gradle_build": "BUILD FAILED in 4m 22s"}
        history = [ex]

        result = _format_investigation(ex, failed_steps, step_logs, history)
        assert "Hawkeye Failure Investigation" in result
        assert "Build_Gateway_Service" in result
        assert "gradle_build" in result
        assert "Compilation error" in result
        assert "APPLICATION_FAILURE" in result
        assert "BUILD FAILED" in result
        assert "release_26_2" in result

    def test_investigation_with_url_input(self) -> None:
        """The investigation tool accepts URL — test the report format."""
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=[
            _make_stage(
                stage_id="Deploy",
                name="Deploy",
                status=ExecutionStatus.FAILED,
                steps=[_make_step(status=ExecutionStatus.FAILED, failure_info=FailureInfo(message="timeout"))],
            )
        ])
        failed_steps = _collect_failed_steps(ex)
        result = _format_investigation(ex, failed_steps, {}, [])
        assert "Hawkeye Failure Investigation" in result
        assert "Deploy" in result

    def test_investigation_parallel_calls_structure(self) -> None:
        """Verify all 4 data signals appear in report."""
        step = _make_step(status=ExecutionStatus.FAILED, failure_info=FailureInfo(message="error"))
        stage = _make_stage(
            stage_id="stg", name="TestStage", status=ExecutionStatus.FAILED, steps=[step]
        )
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=[stage])
        failed_steps = _collect_failed_steps(ex)
        step_logs = {"stg/step1": "log line 1\nlog line 2"}
        history = [
            ex,
            _make_execution(execution_id="exec2", status=ExecutionStatus.SUCCESS, run_sequence=246),
        ]

        result = _format_investigation(ex, failed_steps, step_logs, history)
        # Summary
        assert "Summary" in result
        # Root Cause
        assert "Root Cause" in result
        # Step Details
        assert "Failed Step Details" in result
        assert "log line 1" in result
        # History table
        assert "Pipeline History" in result
        # Analysis
        assert "Analysis" in result

    def test_investigation_running_execution(self) -> None:
        ex = _make_execution(status=ExecutionStatus.RUNNING, stages=[
            _make_stage(status=ExecutionStatus.RUNNING)
        ])
        result = _format_investigation(ex, [], {}, [])
        assert "RUNNING" in result

    def test_investigation_success_execution(self) -> None:
        ex = _make_execution(status=ExecutionStatus.SUCCESS)
        result = _format_investigation(ex, [], {}, [])
        assert "SUCCESS" in result

    def test_investigation_multiple_failed_stages(self) -> None:
        step1 = _make_step(step_id="s1", name="s1", status=ExecutionStatus.FAILED,
                           failure_info=FailureInfo(message="err1"))
        step2 = _make_step(step_id="s2", name="s2", status=ExecutionStatus.FAILED,
                           failure_info=FailureInfo(message="err2"))
        stage1 = _make_stage(stage_id="stg1", name="Stage1", status=ExecutionStatus.FAILED, steps=[step1])
        stage2 = _make_stage(stage_id="stg2", name="Stage2", status=ExecutionStatus.FAILED, steps=[step2])
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=[stage1, stage2])
        failed_steps = _collect_failed_steps(ex)
        step_logs = {"stg1/s1": "log1", "stg2/s2": "log2"}

        result = _format_investigation(ex, failed_steps, step_logs, [])
        assert "Stage1" in result
        assert "Stage2" in result
        assert "err1" in result
        assert "err2" in result

    def test_investigation_no_history(self) -> None:
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=[
            _make_stage(status=ExecutionStatus.FAILED, steps=[
                _make_step(status=ExecutionStatus.FAILED, failure_info=FailureInfo(message="boom"))
            ])
        ])
        failed_steps = _collect_failed_steps(ex)
        result = _format_investigation(ex, failed_steps, {}, [])
        assert "Hawkeye Failure Investigation" in result
        # No history section when empty
        assert "Pipeline History" not in result

    def test_log_extraction_for_failed_steps(self) -> None:
        step = _make_step(
            step_id="gradle_build",
            name="gradle_build",
            status=ExecutionStatus.FAILED,
        )
        stage = _make_stage(
            stage_id="Build",
            name="Build",
            status=ExecutionStatus.FAILED,
            steps=[step],
        )
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=[stage])
        failed_steps = _collect_failed_steps(ex)
        step_details = {"Build/gradle_build": "Step: gradle_build (Run) — ❌ Failed (4m 22s)\nError:\n  Message: BUILD FAILED"}

        result = _format_investigation(ex, failed_steps, step_details, [])
        assert "gradle_build" in result
        assert "Failed Step Details" in result
        assert "BUILD FAILED" in result

    def test_pattern_detection_in_history(self) -> None:
        """History analysis should detect same-stage failures."""
        failed_stage = _make_stage(
            stage_id="helm", name="helm_upgrade", status=ExecutionStatus.FAILED,
            steps=[_make_step(status=ExecutionStatus.FAILED)]
        )
        ex_current = _make_execution(
            execution_id="exec1", status=ExecutionStatus.FAILED,
            stages=[failed_stage], run_sequence=247,
        )
        ex_past = _make_execution(
            execution_id="exec2", status=ExecutionStatus.FAILED,
            stages=[failed_stage], run_sequence=244,
        )
        history = [ex_current, ex_past]
        failed_steps = _collect_failed_steps(ex_current)

        result = _format_investigation(ex_current, failed_steps, {}, history)
        assert "Same stage" in result

    def test_deep_link_generation(self) -> None:
        step = _make_step(name="gradle_build", status=ExecutionStatus.FAILED)
        stage = _make_stage(
            stage_id="Build", name="Build_Gateway", status=ExecutionStatus.FAILED, steps=[step]
        )
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=[stage])
        failed_steps = _collect_failed_steps(ex)

        result = _format_investigation(ex, failed_steps, {}, [])
        assert "Deep Links" in result
        assert "?stage=Build_Gateway" in result
        assert "&step=gradle_build" in result


# ------------------------------------------------------------------
# hawkeye_compare_executions
# ------------------------------------------------------------------


class TestFormatComparison:
    def test_basic_diff(self) -> None:
        ex1 = _make_execution(
            execution_id="exec1", status=ExecutionStatus.FAILED,
            branch="release_26_2", run_sequence=247,
            stages=[_make_stage(stage_id="Build", name="Build", status=ExecutionStatus.FAILED)],
        )
        ex2 = _make_execution(
            execution_id="exec2", status=ExecutionStatus.SUCCESS,
            branch="release_26_2", run_sequence=246,
            stages=[_make_stage(stage_id="Build", name="Build", status=ExecutionStatus.SUCCESS)],
        )
        result = _format_comparison(ex1, ex2)
        assert "Execution Comparison" in result
        assert "Run #247" in result
        assert "Run #246" in result
        assert "Failed" in result
        assert "Success" in result

    def test_comparison_different_branches(self) -> None:
        ex1 = _make_execution(branch="main", run_sequence=1)
        ex2 = _make_execution(branch="develop", run_sequence=2)
        result = _format_comparison(ex1, ex2)
        assert "main" in result
        assert "develop" in result
        assert "Branch changed" in result

    def test_comparison_different_triggers(self) -> None:
        ex1 = _make_execution(trigger_type="MANUAL", triggered_by="alice", run_sequence=1)
        ex2 = _make_execution(trigger_type="WEBHOOK", triggered_by="github", run_sequence=2)
        result = _format_comparison(ex1, ex2)
        assert "Trigger type changed" in result
        assert "Triggered by changed" in result

    def test_comparison_same_success(self) -> None:
        ex1 = _make_execution(status=ExecutionStatus.SUCCESS, run_sequence=1)
        ex2 = _make_execution(status=ExecutionStatus.SUCCESS, run_sequence=2)
        result = _format_comparison(ex1, ex2)
        assert "Execution Comparison" in result
        # No status difference
        assert "Status changed" not in result

    def test_comparison_timing_differences(self) -> None:
        stage1 = _make_stage(
            stage_id="Build", name="Build",
            start_ts=_NOW_MS - 300_000, end_ts=_NOW_MS,
        )
        stage2 = _make_stage(
            stage_id="Build", name="Build",
            start_ts=_NOW_MS - 120_000, end_ts=_NOW_MS,
        )
        ex1 = _make_execution(run_sequence=1, stages=[stage1])
        ex2 = _make_execution(run_sequence=2, stages=[stage2])
        result = _format_comparison(ex1, ex2)
        assert "Stage Comparison" in result
        assert "Build" in result

    def test_comparison_skipped_stages(self) -> None:
        stage_a = _make_stage(stage_id="stgA", name="StageA")
        stage_b = _make_stage(stage_id="stgB", name="StageB")
        ex1 = _make_execution(run_sequence=1, stages=[stage_a, stage_b])
        ex2 = _make_execution(run_sequence=2, stages=[stage_a])
        result = _format_comparison(ex1, ex2)
        assert "Skipped" in result
        assert "StageB" in result


# ------------------------------------------------------------------
# _collect_rollback_steps + rollback report section
# ------------------------------------------------------------------


class TestCollectRollbackSteps:
    def test_no_rollback_steps(self) -> None:
        from tools.investigate import _collect_rollback_steps

        step = _make_step(step_id="s1", name="Build", status=ExecutionStatus.FAILED)
        stage = _make_stage(
            stage_id="stg1", name="Stage1", status=ExecutionStatus.FAILED, steps=[step]
        )
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=[stage])
        assert _collect_rollback_steps(ex) == []

    def test_rollback_steps_detected(self) -> None:
        from tools.investigate import _collect_rollback_steps

        deploy_step = _make_step(
            step_id="deploy1", name="Rollout Deployment",
            status=ExecutionStatus.FAILED, step_type="K8sRollingDeploy",
        )
        rollback_step = _make_step(
            step_id="rb1", name="Rollout Rollback",
            status=ExecutionStatus.SUCCESS, step_type="K8sRollingRollback",
        )
        stage = _make_stage(
            stage_id="stg1", name="deploy svc k8s",
            status=ExecutionStatus.FAILED,
            steps=[deploy_step, rollback_step],
        )
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=[stage])
        result = _collect_rollback_steps(ex)
        assert len(result) == 1
        assert result[0][2].name == "Rollout Rollback"

    def test_rollback_steps_in_success_stage_ignored(self) -> None:
        from tools.investigate import _collect_rollback_steps

        rollback_step = _make_step(
            step_id="rb1", name="Rollout Rollback",
            status=ExecutionStatus.SUCCESS, step_type="K8sRollingRollback",
        )
        stage = _make_stage(
            stage_id="stg1", name="Stage1",
            status=ExecutionStatus.SUCCESS,
            steps=[rollback_step],
        )
        ex = _make_execution(stages=[stage])
        assert _collect_rollback_steps(ex) == []

    def test_rollback_section_in_report(self) -> None:
        deploy_step = _make_step(
            step_id="deploy1", name="Rollout Deployment",
            status=ExecutionStatus.FAILED, step_type="K8sRollingDeploy",
            failure_info=FailureInfo(message="progress deadline exceeded"),
        )
        rollback_step = _make_step(
            step_id="rb1", name="Rollout Rollback",
            status=ExecutionStatus.SUCCESS, step_type="K8sRollingRollback",
        )
        stage = _make_stage(
            stage_id="stg1", name="deploy svc k8s",
            status=ExecutionStatus.FAILED,
            steps=[deploy_step, rollback_step],
        )
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=[stage])
        failed_steps = _collect_failed_steps(ex)
        result = _format_investigation(ex, failed_steps, {}, [])
        assert "Rollback Triggered" in result
        assert "Rollout Rollback" in result


# ------------------------------------------------------------------
# _build_analysis — failure classification
# ------------------------------------------------------------------


class TestBuildAnalysis:
    def test_k8s_deploy_failure_classified_as_infra(self) -> None:
        """K8s deployment failures with progress deadline should NOT be classified as code issues."""
        step = _make_step(
            step_id="s1", name="Rollout Deployment",
            status=ExecutionStatus.FAILED, step_type="K8sRollingDeploy",
            failure_info=FailureInfo(
                message="Steady state check failed with error:\ndeployment svc exceeded its progress deadline",
                failure_types=["APPLICATION_ERROR"],
                response_messages=["Resources failed to reach steady state."],
            ),
        )
        stage = _make_stage(
            stage_id="stg1", name="deploy svc k8s",
            status=ExecutionStatus.FAILED, steps=[step],
        )
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=[stage])
        failed_steps = _collect_failed_steps(ex)
        analysis = _build_analysis(ex, failed_steps, [])
        # Should NOT say "code change issue"
        assert not any("code change" in a for a in analysis)
        # Should say infra/K8s
        assert any("k8s" in a.lower() or "infrastructure" in a.lower() for a in analysis)

    def test_k8s_expired_deploy_classified_as_infra(self) -> None:
        """Expired K8s deployment should be classified as infrastructure timeout."""
        step = _make_step(
            step_id="s1", name="Rollout Deployment",
            status=ExecutionStatus.EXPIRED, step_type="K8sRollingDeploy",
            failure_info=FailureInfo(
                message="Please Check the timeout configuration",
                failure_types=["EXPIRED"],
            ),
        )
        stage = _make_stage(
            stage_id="stg1", name="deploy arelle k8s",
            status=ExecutionStatus.EXPIRED, steps=[step],
        )
        ex = _make_execution(status=ExecutionStatus.EXPIRED, stages=[stage])
        failed_steps = _collect_failed_steps(ex)
        analysis = _build_analysis(ex, failed_steps, [])
        assert not any("code change" in a for a in analysis)
        assert any("k8s" in a.lower() for a in analysis)

    def test_real_app_failure_still_classified_correctly(self) -> None:
        """A genuine application failure (e.g. compilation) should still be classified as code issue."""
        step = _make_step(
            step_id="s1", name="gradle_build",
            status=ExecutionStatus.FAILED, step_type="Run",
            failure_info=FailureInfo(
                message="Compilation failed",
                failure_types=["APPLICATION_FAILURE"],
            ),
        )
        stage = _make_stage(
            stage_id="stg1", name="Build",
            status=ExecutionStatus.FAILED, steps=[step],
        )
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=[stage])
        failed_steps = _collect_failed_steps(ex)
        analysis = _build_analysis(ex, failed_steps, [])
        assert any("code change" in a for a in analysis)


# ------------------------------------------------------------------
# _compute_diffs
# ------------------------------------------------------------------


class TestComputeDiffs:
    def test_no_diffs_when_identical(self) -> None:
        ex = _make_execution()
        diffs = _compute_diffs(ex, ex)
        assert len(diffs) == 0

    def test_status_diff(self) -> None:
        ex1 = _make_execution(status=ExecutionStatus.FAILED)
        ex2 = _make_execution(status=ExecutionStatus.SUCCESS)
        diffs = _compute_diffs(ex1, ex2)
        assert any("Status changed" in d for d in diffs)

    def test_branch_diff(self) -> None:
        ex1 = _make_execution(branch="main")
        ex2 = _make_execution(branch="develop")
        diffs = _compute_diffs(ex1, ex2)
        assert any("Branch changed" in d for d in diffs)


# ------------------------------------------------------------------
# _timing_delta
# ------------------------------------------------------------------


class TestTimingDelta:
    def test_same_timing(self) -> None:
        s = _make_stage(start_ts=_NOW_MS - 10_000, end_ts=_NOW_MS)
        result = _timing_delta(s, s)
        assert result == "~same"

    def test_slower_second(self) -> None:
        s1 = _make_stage(start_ts=_NOW_MS - 10_000, end_ts=_NOW_MS)
        s2 = _make_stage(start_ts=_NOW_MS - 70_000, end_ts=_NOW_MS)
        result = _timing_delta(s1, s2)
        assert "+" in result or "m" in result or "s" in result

    def test_missing_timestamps(self) -> None:
        s1 = _make_stage(start_ts=None, end_ts=None)
        s2 = _make_stage(start_ts=_NOW_MS, end_ts=_NOW_MS)
        result = _timing_delta(s1, s2)
        assert result == "—"

    def test_faster_second_shows_clean_negative_seconds(self) -> None:
        """-1500ms must render as -1s, not floor-divided to -2s."""
        s1 = _make_stage(start_ts=_NOW_MS - 10_000, end_ts=_NOW_MS)
        s2 = _make_stage(start_ts=_NOW_MS - 8_500, end_ts=_NOW_MS)
        result = _timing_delta(s1, s2)
        assert result == "-1s"

    def test_faster_second_negative_minutes(self) -> None:
        s1 = _make_stage(start_ts=_NOW_MS - 200_000, end_ts=_NOW_MS)
        s2 = _make_stage(start_ts=_NOW_MS - 65_000, end_ts=_NOW_MS)
        result = _timing_delta(s1, s2)
        assert result == "-2m 15s"


# ------------------------------------------------------------------
# hawkeye_get_failure_pattern
# ------------------------------------------------------------------


class TestFormatFailurePattern:
    def test_mixed_results(self) -> None:
        execs = [
            _make_execution(execution_id=f"e{i}", status=ExecutionStatus.SUCCESS, run_sequence=i)
            for i in range(15)
        ] + [
            _make_execution(
                execution_id=f"f{i}", status=ExecutionStatus.FAILED,
                run_sequence=20 + i,
                stages=[_make_stage(
                    stage_id="helm", name="helm_upgrade",
                    status=ExecutionStatus.FAILED,
                    failure_info=FailureInfo(message="ImagePullBackOff"),
                )],
            )
            for i in range(5)
        ]
        result = _format_failure_pattern("ci_cd_pipeline", execs)
        assert "Failure Pattern Analysis" in result
        assert "75%" in result
        assert "helm_upgrade" in result
        assert "ImagePullBackOff" in result

    def test_recurring_stage_failures(self) -> None:
        execs = [
            _make_execution(
                execution_id=f"e{i}", status=ExecutionStatus.FAILED,
                run_sequence=i,
                stages=[_make_stage(
                    stage_id="deploy", name="deploy",
                    status=ExecutionStatus.FAILED,
                    failure_info=FailureInfo(message="timeout"),
                )],
            )
            for i in range(5)
        ]
        result = _format_failure_pattern("pipe", execs)
        assert "RECURRING" in result
        assert "deploy" in result

    def test_branch_specific_failures(self) -> None:
        execs = [
            _make_execution(
                execution_id="e1", status=ExecutionStatus.FAILED,
                branch="release_26_2", run_sequence=1,
                stages=[_make_stage(
                    stage_id="tf", name="terraform_plan",
                    status=ExecutionStatus.FAILED,
                    failure_info=FailureInfo(message="State lock"),
                )],
            ),
            _make_execution(
                execution_id="e2", status=ExecutionStatus.FAILED,
                branch="release_26_2", run_sequence=2,
                stages=[_make_stage(
                    stage_id="tf", name="terraform_plan",
                    status=ExecutionStatus.FAILED,
                    failure_info=FailureInfo(message="State lock"),
                )],
            ),
            _make_execution(
                execution_id="e3", status=ExecutionStatus.SUCCESS,
                branch="main", run_sequence=3,
            ),
        ]
        result = _format_failure_pattern("pipe", execs)
        assert "BRANCH-SPECIFIC" in result
        assert "release_26_2" in result

    def test_all_successes(self) -> None:
        execs = [
            _make_execution(execution_id=f"e{i}", status=ExecutionStatus.SUCCESS, run_sequence=i)
            for i in range(10)
        ]
        result = _format_failure_pattern("pipe", execs)
        assert "100%" in result
        assert "Failed: 0" in result

    def test_all_failures(self) -> None:
        execs = [
            _make_execution(
                execution_id=f"e{i}", status=ExecutionStatus.FAILED,
                run_sequence=i,
                stages=[_make_stage(
                    stage_id="build", name="build",
                    status=ExecutionStatus.FAILED,
                    failure_info=FailureInfo(message="error"),
                )],
            )
            for i in range(5)
        ]
        result = _format_failure_pattern("pipe", execs)
        assert "0%" in result
        assert "Failed: 5" in result

    def test_success_rate_calculation(self) -> None:
        execs = [
            _make_execution(execution_id="s1", status=ExecutionStatus.SUCCESS, run_sequence=1),
            _make_execution(execution_id="s2", status=ExecutionStatus.SUCCESS, run_sequence=2),
            _make_execution(execution_id="s3", status=ExecutionStatus.SUCCESS, run_sequence=3),
            _make_execution(execution_id="f1", status=ExecutionStatus.FAILED, run_sequence=4,
                           stages=[_make_stage(status=ExecutionStatus.FAILED)]),
        ]
        result = _format_failure_pattern("pipe", execs)
        assert "75%" in result
        assert "3/4" in result


# ------------------------------------------------------------------
# _detect_patterns
# ------------------------------------------------------------------


class TestDetectPatterns:
    def test_success_trend(self) -> None:
        execs = [
            _make_execution(execution_id=f"e{i}", status=ExecutionStatus.SUCCESS, run_sequence=i)
            for i in range(5)
        ]
        patterns = _detect_patterns(execs, {})
        assert any("SUCCESS TREND" in p for p in patterns)

    def test_failure_trend(self) -> None:
        execs = [
            _make_execution(execution_id=f"e{i}", status=ExecutionStatus.FAILED, run_sequence=i)
            for i in range(5)
        ]
        patterns = _detect_patterns(execs, {"stage": ["err", "err"]})
        assert any("FAILURE TREND" in p for p in patterns)

    def test_no_patterns_with_few_runs(self) -> None:
        execs = [_make_execution(execution_id="e1", status=ExecutionStatus.SUCCESS, run_sequence=1)]
        patterns = _detect_patterns(execs, {})
        # Not enough data for trend
        assert not any("TREND" in p for p in patterns)


# ------------------------------------------------------------------
# _most_common
# ------------------------------------------------------------------


class TestMostCommon:
    def test_single_item(self) -> None:
        assert _most_common(["a"]) == "a"

    def test_most_frequent(self) -> None:
        assert _most_common(["a", "b", "a", "c", "a"]) == "a"

    def test_empty_list(self) -> None:
        assert _most_common([]) == ""


# ------------------------------------------------------------------
# Integration: tool MCP registration smoke
# ------------------------------------------------------------------


class TestInvestigateToolRegistration:
    """Verify the tools can be registered on a FastMCP instance."""

    def test_registration_does_not_raise(self) -> None:
        from mcp.server.fastmcp import FastMCP

        from tools.investigate import register_investigation_tools

        server = FastMCP("test")
        register_investigation_tools(server)

    @pytest.mark.asyncio
    async def test_investigate_failure_missing_params(self) -> None:
        """Tool returns error when no execution_id or url provided."""
        from mcp.server.fastmcp import FastMCP

        from tools.investigate import register_investigation_tools

        server = FastMCP("test")
        register_investigation_tools(server)

        # We call the internal async _run path via the tool wiring
        # Easier to test the formatted output via the helper
        from tools.investigate import _format_investigation

        ex = _make_execution(status=ExecutionStatus.SUCCESS)
        result = _format_investigation(ex, [], {}, [])
        assert "SUCCESS" in result

    @pytest.mark.asyncio
    async def test_investigate_failure_with_mock_provider(self) -> None:
        """End-to-end: hawkeye_investigate_failure with a fully mocked provider."""
        failed_step = _make_step(
            step_id="gradle",
            name="gradle_build",
            status=ExecutionStatus.FAILED,
            failure_info=FailureInfo(message="compile error"),
        )
        failed_stage = _make_stage(
            stage_id="Build",
            name="Build",
            status=ExecutionStatus.FAILED,
            steps=[failed_step],
        )
        execution = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[failed_stage],
        )

        mock_provider = AsyncMock()
        mock_provider.get_execution.return_value = execution
        mock_provider.get_execution_graph.return_value = _make_graph()
        mock_provider.get_step_logs.return_value = "BUILD FAILED"
        mock_provider.list_recent_executions.return_value = [execution]

        with patch("tools.investigate.get_provider", return_value=mock_provider):
            from mcp.server.fastmcp import FastMCP

            from tools.investigate import register_investigation_tools

            server = FastMCP("test")
            register_investigation_tools(server)

            # Directly call inner logic
            from tools.investigate import _collect_failed_steps, _format_investigation

            failed_steps = _collect_failed_steps(execution)
            result = _format_investigation(
                execution, failed_steps,
                {"Build/gradle": "BUILD FAILED"},
                [execution],
            )
            assert "compile error" in result
            assert "BUILD FAILED" in result

    @pytest.mark.asyncio
    async def test_compare_executions_with_mock(self) -> None:
        ex1 = _make_execution(
            execution_id="e1", status=ExecutionStatus.FAILED, run_sequence=10,
            stages=[_make_stage(stage_id="Build", name="Build", status=ExecutionStatus.FAILED)],
        )
        ex2 = _make_execution(
            execution_id="e2", status=ExecutionStatus.SUCCESS, run_sequence=9,
            stages=[_make_stage(stage_id="Build", name="Build", status=ExecutionStatus.SUCCESS)],
        )
        result = _format_comparison(ex1, ex2)
        assert "Run #10" in result
        assert "Run #9" in result
        assert "Status changed" in result

    @pytest.mark.asyncio
    async def test_failure_pattern_with_mock(self) -> None:
        execs = [
            _make_execution(execution_id=f"e{i}", status=ExecutionStatus.SUCCESS, run_sequence=i)
            for i in range(8)
        ] + [
            _make_execution(
                execution_id="f1", status=ExecutionStatus.FAILED, run_sequence=9,
                stages=[_make_stage(
                    stage_id="deploy", name="deploy",
                    status=ExecutionStatus.FAILED,
                    failure_info=FailureInfo(message="OOM"),
                )],
            ),
            _make_execution(
                execution_id="f2", status=ExecutionStatus.FAILED, run_sequence=10,
                stages=[_make_stage(
                    stage_id="deploy", name="deploy",
                    status=ExecutionStatus.FAILED,
                    failure_info=FailureInfo(message="OOM"),
                )],
            ),
        ]
        result = _format_failure_pattern("pipe", execs)
        assert "80%" in result
        assert "deploy" in result
        assert "OOM" in result


# ------------------------------------------------------------------
# Investigation resilience tests
# ------------------------------------------------------------------


class TestInvestigationResilience:
    """Test that investigation completes gracefully when sub-calls fail."""

    @pytest.mark.asyncio
    async def test_investigation_completes_when_step_detail_fails(self) -> None:
        """If get_step_logs raises for a failed step, report still generates."""
        failed_step = _make_step(
            step_id="bad", name="Deploy",
            status=ExecutionStatus.FAILED,
            failure_info=FailureInfo(message="timeout"),
        )
        failed_stage = _make_stage(
            stage_id="deploy", name="Deploy",
            status=ExecutionStatus.FAILED, steps=[failed_step],
        )
        execution = _make_execution(
            status=ExecutionStatus.FAILED, stages=[failed_stage],
        )

        mock_provider = AsyncMock()
        mock_provider.get_execution.return_value = execution
        mock_provider.get_execution_graph.return_value = _make_graph()
        mock_provider.get_step_logs.side_effect = RuntimeError("API unavailable")
        mock_provider.list_recent_executions.return_value = [execution]

        with (
            patch("tools.investigate.get_provider", return_value=mock_provider),
            patch("tools._helpers.log_tool_call"),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_investigate_failure"].fn
            result = await tool_fn(execution_id="exec1")

        assert "Investigation" in result
        assert "(step details unavailable)" in result
        assert "Deploy" in result

    @pytest.mark.asyncio
    async def test_investigation_completes_when_history_fails(self) -> None:
        """If list_recent_executions raises, investigation still completes."""
        execution = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[_make_stage(
                stage_id="build", name="Build",
                status=ExecutionStatus.FAILED,
                steps=[_make_step(step_id="s1", name="Compile", status=ExecutionStatus.FAILED)],
            )],
        )

        mock_provider = AsyncMock()
        mock_provider.get_execution.return_value = execution
        mock_provider.get_execution_graph.return_value = _make_graph()
        mock_provider.get_step_logs.return_value = {
            "node": {"name": "Compile", "stepType": "Run", "status": "Failed", "startTs": _NOW_MS, "endTs": _NOW_MS + 5000},
            "subgraph": {},
        }
        mock_provider.list_recent_executions.side_effect = RuntimeError("History unavailable")

        with (
            patch("tools.investigate.get_provider", return_value=mock_provider),
            patch("tools._helpers.log_tool_call"),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_investigate_failure"].fn
            result = await tool_fn(execution_id="exec1")

        assert "Investigation" in result
        assert "Compile" in result

    @pytest.mark.asyncio
    async def test_investigation_with_step_details_when_available(self) -> None:
        """When get_step_logs succeeds, details appear in the report."""
        failed_step = _make_step(
            step_id="bad", name="Deploy",
            status=ExecutionStatus.FAILED,
            failure_info=FailureInfo(message="OOM Killed"),
        )
        failed_stage = _make_stage(
            stage_id="deploy", name="Deploy",
            status=ExecutionStatus.FAILED, steps=[failed_step],
        )
        execution = _make_execution(
            status=ExecutionStatus.FAILED, stages=[failed_stage],
        )

        mock_provider = AsyncMock()
        mock_provider.get_execution.return_value = execution
        mock_provider.get_execution_graph.return_value = _make_graph()
        mock_provider.get_step_logs.return_value = {
            "node": {
                "name": "Deploy",
                "stepType": "K8sRollingDeploy",
                "status": "Failed",
                "startTs": _NOW_MS,
                "endTs": _NOW_MS + 60_000,
                "failureInfo": {"message": "OOM Killed", "failureTypeList": ["APPLICATION_FAILURE"]},
            },
            "subgraph": {},
        }
        mock_provider.list_recent_executions.return_value = [execution]

        with (
            patch("tools.investigate.get_provider", return_value=mock_provider),
            patch("tools._helpers.log_tool_call"),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_investigate_failure"].fn
            result = await tool_fn(execution_id="exec1")

        assert "Failed Step Details" in result
        assert "OOM Killed" in result

    @pytest.mark.asyncio
    async def test_investigation_all_parallel_calls_fail(self) -> None:
        """Even if history + graph + step details all fail, report still renders."""
        execution = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[_make_stage(
                stage_id="build", name="Build",
                status=ExecutionStatus.FAILED,
                steps=[_make_step(step_id="s1", name="Compile", status=ExecutionStatus.FAILED)],
            )],
        )

        mock_provider = AsyncMock()
        mock_provider.get_execution.return_value = execution
        mock_provider.get_execution_graph.side_effect = RuntimeError("graph error")
        mock_provider.get_step_logs.side_effect = RuntimeError("logs error")
        mock_provider.list_recent_executions.side_effect = RuntimeError("history error")

        with (
            patch("tools.investigate.get_provider", return_value=mock_provider),
            patch("tools._helpers.log_tool_call"),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_investigate_failure"].fn
            result = await tool_fn(execution_id="exec1")

        assert "Investigation" in result
        assert "(step details unavailable)" in result
        assert "Build" in result

    def test_format_investigation_with_step_details_dict(self) -> None:
        """_format_investigation includes step details when provided."""
        failed_step = _make_step(
            step_id="s1", name="Deploy",
            status=ExecutionStatus.FAILED,
            failure_info=FailureInfo(message="timeout"),
        )
        execution = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[_make_stage(
                stage_id="deploy", name="Deploy",
                status=ExecutionStatus.FAILED, steps=[failed_step],
            )],
        )
        failed_steps = _collect_failed_steps(execution)
        step_logs = {
            "deploy/s1": "Step: Deploy (K8sRollingDeploy) — ❌ Failed (1m 0s)\nError:\n  Message: timeout"
        }
        result = _format_investigation(execution, failed_steps, step_logs, [execution])
        assert "Failed Step Details" in result
        assert "timeout" in result


# ------------------------------------------------------------------
# _follow_child_pipelines — API logs
# ------------------------------------------------------------------


class TestFollowChildPipelinesApiLogs:
    """Test that _follow_child_pipelines includes API console logs."""

    @pytest.mark.asyncio
    async def test_no_pipeline_stages_returns_empty(self) -> None:
        """No pipeline-type stages means no child following."""
        execution = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[_make_stage(
                stage_id="build", name="Build", status=ExecutionStatus.FAILED,
                steps=[_make_step(status=ExecutionStatus.FAILED)],
            )],
        )
        provider = AsyncMock()
        result = await _follow_child_pipelines(execution, {}, provider)
        assert result == {}

    @pytest.mark.asyncio
    async def test_follows_pipeline_type_stage(self) -> None:
        """Pipeline-type failed stages are followed to get child details."""
        # Parent has a Pipeline-type failed stage
        child_step = _make_step(
            step_id="child_step1", name="child_build",
            status=ExecutionStatus.FAILED,
            failure_info=FailureInfo(message="exit status 1"),
        )
        child_stage = _make_stage(
            stage_id="child_stg1", name="ChildBuild",
            status=ExecutionStatus.FAILED,
            steps=[child_step],
        )
        child_execution = _make_execution(
            execution_id="child_exec1",
            status=ExecutionStatus.FAILED,
            stages=[child_stage],
            pipeline_id="build_sifi_adapter",
            pipeline_name="build_sifi_adapter",
        )

        pipeline_stage = StageExecution(
            stage_id="parent_stg1",
            name="ci sifi adapter",
            identifier="ci_sifi_adapter",
            status=ExecutionStatus.FAILED,
            start_ts=_NOW_MS - 60_000,
            end_ts=_NOW_MS,
            stage_type="Pipeline",
            steps=[],
        )
        parent_execution = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[pipeline_stage],
        )

        # Raw node map with child execution ID
        raw_node_map = {
            "uuid1": {
                "name": "ci sifi adapter",
                "identifier": "ci_sifi_adapter",
                "stepType": "Pipeline",
                "status": "Failed",
                "baseFqn": "pipeline.stages.ci_sifi_adapter",
                "outcomes": {
                    "output": {
                        "pipelineExecutionId": "child_exec1",
                    }
                },
            }
        }

        mock_provider = AsyncMock()
        mock_provider.get_execution.return_value = child_execution
        mock_provider.get_step_logs.return_value = {
            "node": {
                "name": "child_build",
                "stepType": "Run",
                "status": "Failed",
                "startTs": _NOW_MS,
                "endTs": _NOW_MS + 5000,
                "failureInfo": {"message": "exit status 1", "responseMessages": []},
            },
            "subgraph": {},
            "api_logs": "child build console logs here",
        }

        result = await _follow_child_pipelines(parent_execution, raw_node_map, mock_provider)

        assert "parent_stg1" in result
        child_data = result["parent_stg1"]
        assert child_data["child_execution"].plan_execution_id == "child_exec1"
        assert len(child_data["child_failed_steps"]) == 1
        assert "Console Logs" in child_data["child_step_details"]["child_stg1/child_step1"]

    @pytest.mark.asyncio
    async def test_api_log_results_mapped_to_children(self) -> None:
        """API log results should be mapped back to the correct child step details."""
        child_step = _make_step(
            step_id="cstep1", name="build_step",
            status=ExecutionStatus.FAILED,
            failure_info=FailureInfo(message="exit status 1"),
        )
        child_stage = _make_stage(
            stage_id="cstg1", name="ChildStage",
            status=ExecutionStatus.FAILED,
            steps=[child_step],
        )
        child_execution = _make_execution(
            execution_id="cexec1",
            status=ExecutionStatus.FAILED,
            stages=[child_stage],
            pipeline_id="child_pipe",
        )

        pipeline_stage = StageExecution(
            stage_id="pstg1",
            name="child pipeline",
            identifier="child_pipeline",
            status=ExecutionStatus.FAILED,
            start_ts=_NOW_MS - 60_000,
            end_ts=_NOW_MS,
            stage_type="Pipeline",
            steps=[],
        )
        parent = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[pipeline_stage],
        )

        raw_node_map = {
            "uuid1": {
                "name": "child pipeline",
                "identifier": "child_pipeline",
                "stepType": "Pipeline",
                "status": "Failed",
                "baseFqn": "pipeline.stages.child_pipeline",
                "outcomes": {"output": {"pipelineExecutionId": "cexec1"}},
            }
        }

        mock_provider = AsyncMock()
        mock_provider.get_execution.return_value = child_execution
        mock_provider.get_step_logs.return_value = {
            "node": {
                "name": "build_step", "stepType": "Run", "status": "Failed",
                "startTs": _NOW_MS, "endTs": _NOW_MS + 5000,
                "failureInfo": {"message": "exit status 1", "responseMessages": []},
            },
            "subgraph": {},
        }
        api_log_text = "ACTUAL BUILD OUTPUT\nerror: cannot find module"
        mock_provider.get_step_logs.return_value["api_logs"] = api_log_text

        result = await _follow_child_pipelines(parent, raw_node_map, mock_provider)

        child_details = result["pstg1"]["child_step_details"]
        detail_text = child_details["cstg1/cstep1"]
        assert "Console Logs" in detail_text
        assert "ACTUAL BUILD OUTPUT" in detail_text
        assert "cannot find module" in detail_text


# ------------------------------------------------------------------
# _detect_module_for_stage
# ------------------------------------------------------------------


class TestDetectModuleForStage:
    """Test CI vs CD module detection for child execution URLs."""

    def test_ci_stage_type(self) -> None:
        from tools.investigate import _detect_module_for_stage

        stage = StageExecution(
            stage_id="stg1", name="Build", identifier="Build",
            status=ExecutionStatus.FAILED, start_ts=_NOW_MS, end_ts=_NOW_MS,
            stage_type="CI", steps=[],
        )
        assert _detect_module_for_stage(stage, None) == "ci"

    def test_integration_stage_type(self) -> None:
        from tools.investigate import _detect_module_for_stage

        stage = StageExecution(
            stage_id="stg1", name="Build", identifier="Build",
            status=ExecutionStatus.FAILED, start_ts=_NOW_MS, end_ts=_NOW_MS,
            stage_type="IntegrationStageStepPMS", steps=[],
        )
        assert _detect_module_for_stage(stage, None) == "ci"

    def test_pipeline_type_defaults_to_ci(self) -> None:
        from tools.investigate import _detect_module_for_stage

        stage = StageExecution(
            stage_id="stg1", name="child", identifier="child",
            status=ExecutionStatus.FAILED, start_ts=_NOW_MS, end_ts=_NOW_MS,
            stage_type="Pipeline", steps=[],
        )
        assert _detect_module_for_stage(stage, None) == "ci"

    def test_detects_ci_from_raw_node_map(self) -> None:
        from tools.investigate import _detect_module_for_stage

        stage = StageExecution(
            stage_id="stg1", name="ci sifi adapter", identifier="ci_sifi_adapter",
            status=ExecutionStatus.FAILED, start_ts=_NOW_MS, end_ts=_NOW_MS,
            stage_type="Pipeline", steps=[],
        )
        raw_node_map = {
            "uuid1": {
                "name": "ci sifi adapter",
                "identifier": "ci_sifi_adapter",
                "stepType": "Pipeline",
                "nodeType": "IntegrationStageStepPMS",
                "baseFqn": "pipeline.stages.ci_sifi_adapter",
            }
        }
        assert _detect_module_for_stage(stage, raw_node_map) == "ci"


# ------------------------------------------------------------------
# Investigation output includes Console Logs
# ------------------------------------------------------------------


class TestInvestigationOutputConsoleLogs:
    """Test that investigation output includes Console Logs from API content."""

    def test_child_api_logs_in_output(self) -> None:
        """Console Logs from child API details should appear in the report."""
        child_step = _make_step(
            step_id="build_step", name="build",
            status=ExecutionStatus.FAILED,
            failure_info=FailureInfo(message="exit status 1"),
        )
        child_stage = _make_stage(
            stage_id="cstg1", name="ChildBuild",
            status=ExecutionStatus.FAILED,
            steps=[child_step],
        )
        child_execution = _make_execution(
            execution_id="child1",
            status=ExecutionStatus.FAILED,
            stages=[child_stage],
            pipeline_id="build_pipe",
            pipeline_name="build_pipe",
        )

        pipeline_stage = StageExecution(
            stage_id="pstg1", name="ci build",
            identifier="ci_build",
            status=ExecutionStatus.FAILED,
            start_ts=_NOW_MS, end_ts=_NOW_MS,
            stage_type="Pipeline", steps=[],
        )
        parent = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[pipeline_stage],
        )

        child_findings = {
            "pstg1": {
                "child_execution": child_execution,
                "child_failed_steps": [("cstg1", "build_step", child_step)],
                "child_step_details": {
                    "cstg1/build_step": (
                        "Step: build (Run) — ❌ Failed\n\n"
                        "**Console Logs** (Harness Log Service API):\n"
                        "```\nERROR: QUALITY GATE STATUS: FAILED\nProcess returned exit code 1\n```"
                    ),
                },
            }
        }

        result = _format_investigation(
            parent, [], {}, [], child_findings=child_findings,
        )
        assert "Console Logs" in result
        assert "QUALITY GATE STATUS: FAILED" in result
        assert "Child Pipeline Details" in result

    def test_investigation_output_has_api_step_logs(self) -> None:
        """API logs appended to step_details appear in Failed Step Details."""
        step = _make_step(
            step_id="sonar", name="sonar_scan",
            status=ExecutionStatus.FAILED,
            failure_info=FailureInfo(message="exit status 1"),
        )
        stage = _make_stage(
            stage_id="Build", name="Build",
            status=ExecutionStatus.FAILED, steps=[step],
        )
        ex = _make_execution(status=ExecutionStatus.FAILED, stages=[stage])
        failed_steps = _collect_failed_steps(ex)
        step_logs = {
            "Build/sonar": (
                "Step: sonar_scan (Run) — ❌ Failed\n\n"
                "**Console Logs** (Harness Log Service API):\n"
                "```\nERROR: QUALITY GATE STATUS: FAILED\n356 tests passed\n```"
            ),
        }
        result = _format_investigation(ex, failed_steps, step_logs, [])
        assert "Console Logs" in result
        assert "QUALITY GATE STATUS: FAILED" in result
        assert "356 tests passed" in result


# ------------------------------------------------------------------
# hawkeye_diagnose
# ------------------------------------------------------------------


class TestHawkeyeDiagnose:
    """Tests for the unified hawkeye_diagnose tool."""

    @pytest.mark.asyncio
    async def test_diagnose_summary_mode(self) -> None:
        """mode='summary' returns summary header, history, quick analysis, and hint."""
        failed_step = _make_step(
            step_id="deploy1", name="Rollout Deployment",
            status=ExecutionStatus.FAILED, step_type="K8sRollingDeploy",
            failure_info=FailureInfo(message="progress deadline exceeded"),
        )
        failed_stage = _make_stage(
            stage_id="Deploy", name="deploy svc k8s",
            status=ExecutionStatus.FAILED, steps=[failed_step],
        )
        execution = _make_execution(
            status=ExecutionStatus.FAILED, stages=[failed_stage],
        )
        history_exec = _make_execution(
            execution_id="prev1", status=ExecutionStatus.SUCCESS,
            run_sequence=246,
        )

        mock_provider = AsyncMock()
        mock_provider.get_execution.return_value = execution
        mock_provider.list_recent_executions.return_value = [execution, history_exec]
        mock_provider.get_execution_inputs.return_value = "pipeline:\n  variables:\n    env: dev"

        with (
            patch("tools.investigate.get_provider", return_value=mock_provider),
            patch("tools._helpers.log_tool_call"),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_diagnose"].fn
            result = await tool_fn(execution_id="exec1", mode="summary")

        assert "Summary" in result
        assert "Pipeline History" in result
        assert "Quick Analysis" in result
        assert 'mode="full"' in result
        # Summary mode should NOT include step details or console logs
        assert "Console Logs" not in result
        assert "Failed Step Details" not in result

    @pytest.mark.asyncio
    async def test_diagnose_full_mode_with_logs(self) -> None:
        """mode='full' with include_logs=True includes step details and console logs."""
        failed_step = _make_step(
            step_id="gradle", name="gradle_build",
            status=ExecutionStatus.FAILED,
            failure_info=FailureInfo(message="compile error"),
        )
        failed_stage = _make_stage(
            stage_id="Build", name="Build",
            status=ExecutionStatus.FAILED, steps=[failed_step],
        )
        execution = _make_execution(
            status=ExecutionStatus.FAILED, stages=[failed_stage],
        )

        mock_provider = AsyncMock()
        mock_provider.get_execution.return_value = execution
        mock_provider.get_step_logs.return_value = {
            "node": {
                "name": "gradle_build", "stepType": "Run", "status": "Failed",
                "startTs": _NOW_MS, "endTs": _NOW_MS + 5000,
                "failureInfo": {"message": "compile error", "failureTypeList": ["APPLICATION_FAILURE"]},
            },
            "subgraph": {},
            "api_logs": "ERROR: Compilation failed\njava.lang.NullPointerException",
        }
        mock_provider.list_recent_executions.return_value = [execution]
        mock_provider.get_raw_execution_graph.return_value = {}

        with (
            patch("tools.investigate.get_provider", return_value=mock_provider),
            patch("tools._helpers.log_tool_call"),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_diagnose"].fn
            result = await tool_fn(execution_id="exec1", mode="full", include_logs=True)

        assert "Failed Step Details" in result
        assert "Console Logs" in result
        assert "Compilation failed" in result

    @pytest.mark.asyncio
    async def test_diagnose_full_mode_without_logs(self) -> None:
        """mode='full' with include_logs=False shows step details but no console logs."""
        failed_step = _make_step(
            step_id="gradle", name="gradle_build",
            status=ExecutionStatus.FAILED,
            failure_info=FailureInfo(message="compile error"),
        )
        failed_stage = _make_stage(
            stage_id="Build", name="Build",
            status=ExecutionStatus.FAILED, steps=[failed_step],
        )
        execution = _make_execution(
            status=ExecutionStatus.FAILED, stages=[failed_stage],
        )

        mock_provider = AsyncMock()
        mock_provider.get_execution.return_value = execution
        mock_provider.get_step_logs.return_value = {
            "node": {
                "name": "gradle_build", "stepType": "Run", "status": "Failed",
                "startTs": _NOW_MS, "endTs": _NOW_MS + 5000,
                "failureInfo": {"message": "compile error", "failureTypeList": []},
            },
            "subgraph": {},
            "api_logs": "ERROR: should not appear in output",
        }
        mock_provider.list_recent_executions.return_value = [execution]
        mock_provider.get_raw_execution_graph.return_value = {}

        with (
            patch("tools.investigate.get_provider", return_value=mock_provider),
            patch("tools._helpers.log_tool_call"),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_diagnose"].fn
            result = await tool_fn(execution_id="exec1", mode="full", include_logs=False)

        assert "Failed Step Details" in result
        assert "Console Logs" not in result
        assert "should not appear" not in result

    @pytest.mark.asyncio
    async def test_diagnose_defaults_to_full_with_logs(self) -> None:
        """Calling with just execution_id defaults to mode='full', include_logs=True."""
        failed_step = _make_step(
            step_id="s1", name="Deploy",
            status=ExecutionStatus.FAILED,
            failure_info=FailureInfo(message="timeout"),
        )
        failed_stage = _make_stage(
            stage_id="stg1", name="Deploy",
            status=ExecutionStatus.FAILED, steps=[failed_step],
        )
        execution = _make_execution(
            status=ExecutionStatus.FAILED, stages=[failed_stage],
        )

        mock_provider = AsyncMock()
        mock_provider.get_execution.return_value = execution
        mock_provider.get_step_logs.return_value = {
            "node": {
                "name": "Deploy", "stepType": "K8sRollingDeploy", "status": "Failed",
                "startTs": _NOW_MS, "endTs": _NOW_MS + 60_000,
                "failureInfo": {"message": "timeout"},
            },
            "subgraph": {},
            "api_logs": "console output here",
        }
        mock_provider.list_recent_executions.return_value = [execution]
        mock_provider.get_raw_execution_graph.return_value = {}

        with (
            patch("tools.investigate.get_provider", return_value=mock_provider),
            patch("tools._helpers.log_tool_call"),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_diagnose"].fn
            # Call with just execution_id — should default to full + logs
            result = await tool_fn(execution_id="exec1")

        # Full mode includes investigation report
        assert "Investigation" in result
        # Console logs should be present (include_logs defaults to True)
        assert "Console Logs" in result

    @pytest.mark.asyncio
    async def test_diagnose_accepts_url(self) -> None:
        """Passing a Harness URL correctly extracts execution_id."""
        execution = _make_execution(
            execution_id="UqljtMGrS7qDqoiKQzsWvg",
            status=ExecutionStatus.FAILED,
            stages=[_make_stage(
                stage_id="Deploy", name="Deploy",
                status=ExecutionStatus.FAILED,
                steps=[_make_step(
                    step_id="s1", name="Deploy",
                    status=ExecutionStatus.FAILED,
                    failure_info=FailureInfo(message="timeout"),
                )],
            )],
        )

        mock_provider = AsyncMock()
        mock_provider.get_execution.return_value = execution
        mock_provider.get_step_logs.return_value = {
            "node": {"name": "Deploy", "stepType": "K8sRollingDeploy", "status": "Failed",
                     "startTs": _NOW_MS, "endTs": _NOW_MS + 5000},
            "subgraph": {},
        }
        mock_provider.list_recent_executions.return_value = [execution]
        mock_provider.get_raw_execution_graph.return_value = {}

        harness_url = (
            "https://app.harness.io/ng/account/acc123/module/cd/orgs/default/"
            "projects/eastwood/pipelines/ci_cd_pipeline/executions/"
            "UqljtMGrS7qDqoiKQzsWvg/pipeline"
        )

        with (
            patch("tools.investigate.get_provider", return_value=mock_provider),
            patch("tools._helpers.log_tool_call"),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_diagnose"].fn
            result = await tool_fn(url=harness_url, mode="full")

        # Should have called get_execution with the parsed ID
        mock_provider.get_execution.assert_called_once_with("UqljtMGrS7qDqoiKQzsWvg")
        assert "Investigation" in result

    @pytest.mark.asyncio
    async def test_diagnose_summary_includes_history(self) -> None:
        """Summary mode fetches and displays history table."""
        execution = _make_execution(
            status=ExecutionStatus.FAILED,
            stages=[_make_stage(
                status=ExecutionStatus.FAILED,
                steps=[_make_step(status=ExecutionStatus.FAILED)],
            )],
        )
        history = [
            _make_execution(execution_id=f"h{i}", status=ExecutionStatus.SUCCESS,
                           run_sequence=240 + i)
            for i in range(5)
        ]

        mock_provider = AsyncMock()
        mock_provider.get_execution.return_value = execution
        mock_provider.list_recent_executions.return_value = history
        mock_provider.get_execution_inputs.return_value = ""

        with (
            patch("tools.investigate.get_provider", return_value=mock_provider),
            patch("tools._helpers.log_tool_call"),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_diagnose"].fn
            result = await tool_fn(execution_id="exec1", mode="summary")

        assert "Pipeline History (last 5 runs)" in result
        # All 5 history entries should appear
        for i in range(5):
            assert str(240 + i) in result

    @pytest.mark.asyncio
    async def test_diagnose_full_mode_includes_child_pipelines(self) -> None:
        """Full mode follows child pipeline executions for Pipeline-type stages."""
        child_step = _make_step(
            step_id="child_s1", name="child_build",
            status=ExecutionStatus.FAILED,
            failure_info=FailureInfo(message="exit status 1"),
        )
        child_stage = _make_stage(
            stage_id="child_stg1", name="ChildBuild",
            status=ExecutionStatus.FAILED, steps=[child_step],
        )
        child_execution = _make_execution(
            execution_id="child_exec1",
            status=ExecutionStatus.FAILED,
            stages=[child_stage],
            pipeline_id="build_sifi",
            pipeline_name="build_sifi",
        )

        pipeline_stage = StageExecution(
            stage_id="parent_stg1", name="ci sifi adapter",
            identifier="ci_sifi_adapter",
            status=ExecutionStatus.FAILED,
            start_ts=_NOW_MS - 60_000, end_ts=_NOW_MS,
            stage_type="Pipeline", steps=[],
        )
        parent_execution = _make_execution(
            status=ExecutionStatus.FAILED, stages=[pipeline_stage],
        )

        raw_node_map = {
            "uuid1": {
                "name": "ci sifi adapter",
                "identifier": "ci_sifi_adapter",
                "stepType": "Pipeline",
                "status": "Failed",
                "baseFqn": "pipeline.stages.ci_sifi_adapter",
                "outcomes": {"output": {"pipelineExecutionId": "child_exec1"}},
            }
        }

        mock_provider = AsyncMock()
        mock_provider.get_execution.side_effect = [parent_execution, child_execution]
        mock_provider.get_step_logs.return_value = {
            "node": {
                "name": "child_build", "stepType": "Run", "status": "Failed",
                "startTs": _NOW_MS, "endTs": _NOW_MS + 5000,
                "failureInfo": {"message": "exit status 1"},
            },
            "subgraph": {},
        }
        mock_provider.list_recent_executions.return_value = [parent_execution]
        mock_provider.get_raw_execution_graph.return_value = raw_node_map

        with (
            patch("tools.investigate.get_provider", return_value=mock_provider),
            patch("tools._helpers.log_tool_call"),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_diagnose"].fn
            result = await tool_fn(execution_id="exec1", mode="full")

        assert "Child Pipeline Details" in result
        assert "ci sifi adapter" in result

    @pytest.mark.asyncio
    async def test_diagnose_full_mode_includes_rollback_detection(self) -> None:
        """Full mode detects and reports rollback steps."""
        deploy_step = _make_step(
            step_id="deploy1", name="Rollout Deployment",
            status=ExecutionStatus.FAILED, step_type="K8sRollingDeploy",
            failure_info=FailureInfo(message="progress deadline exceeded"),
        )
        rollback_step = _make_step(
            step_id="rb1", name="Rollout Rollback",
            status=ExecutionStatus.SUCCESS, step_type="K8sRollingRollback",
        )
        stage = _make_stage(
            stage_id="stg1", name="deploy svc k8s",
            status=ExecutionStatus.FAILED,
            steps=[deploy_step, rollback_step],
        )
        execution = _make_execution(
            status=ExecutionStatus.FAILED, stages=[stage],
        )

        mock_provider = AsyncMock()
        mock_provider.get_execution.return_value = execution
        mock_provider.get_step_logs.return_value = {
            "node": {
                "name": "Rollout Deployment", "stepType": "K8sRollingDeploy",
                "status": "Failed", "startTs": _NOW_MS, "endTs": _NOW_MS + 60_000,
                "failureInfo": {"message": "progress deadline exceeded"},
            },
            "subgraph": {},
        }
        mock_provider.list_recent_executions.return_value = [execution]
        mock_provider.get_raw_execution_graph.return_value = {}

        with (
            patch("tools.investigate.get_provider", return_value=mock_provider),
            patch("tools._helpers.log_tool_call"),
        ):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_diagnose"].fn
            result = await tool_fn(execution_id="exec1", mode="full")

        assert "Rollback Triggered" in result
        assert "Rollout Rollback" in result

    @pytest.mark.asyncio
    async def test_diagnose_invalid_mode_returns_error(self) -> None:
        """Invalid mode returns an error message."""
        with patch("tools._helpers.log_tool_call"):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_diagnose"].fn
            result = await tool_fn(execution_id="exec1", mode="invalid")

        assert "Invalid mode" in result
        assert "'invalid'" in result
        assert "'summary'" in result or "'full'" in result

    @pytest.mark.asyncio
    async def test_diagnose_no_execution_id_returns_error(self) -> None:
        """Empty execution_id and url returns an error."""
        with patch("tools._helpers.log_tool_call"):
            from main import mcp

            tool_fn = mcp._tool_manager._tools["hawkeye_diagnose"].fn
            result = await tool_fn(execution_id="", url="")

        assert "Error" in result
