"""Tests for core.models — Pydantic data models."""

from __future__ import annotations

from core.models import (
    ExecutionGraph,
    ExecutionStatus,
    FailureInfo,
    GraphNode,
    HarnessProfile,
    InputSet,
    PipelineExecution,
    PipelineSummary,
    StageExecution,
    StepExecution,
)


class TestExecutionStatus:
    def test_all_enum_values_exist(self) -> None:
        assert len(ExecutionStatus) == 22

    def test_success_value(self) -> None:
        assert ExecutionStatus.SUCCESS.value == "Success"

    def test_from_str_valid(self) -> None:
        assert ExecutionStatus.from_str("Failed") == ExecutionStatus.FAILED

    def test_from_str_unknown(self) -> None:
        assert ExecutionStatus.from_str("SomethingNew") == ExecutionStatus.UNKNOWN

    def test_from_str_case_sensitive(self) -> None:
        # Enum is case-sensitive — "success" != "Success"
        assert ExecutionStatus.from_str("success") == ExecutionStatus.UNKNOWN

    def test_enum_is_str_subclass(self) -> None:
        assert isinstance(ExecutionStatus.RUNNING, str)


class TestFailureInfo:
    def test_create_with_defaults(self) -> None:
        fi = FailureInfo()
        assert fi.message == ""
        assert fi.failure_types == []
        assert fi.response_messages == []

    def test_create_with_values(self) -> None:
        fi = FailureInfo(
            message="Timeout exceeded",
            failure_types=["TIMEOUT_ERROR"],
            response_messages=["Delegate unresponsive"],
        )
        assert fi.message == "Timeout exceeded"
        assert fi.failure_types == ["TIMEOUT_ERROR"]

    def test_serialization_round_trip(self) -> None:
        fi = FailureInfo(message="err", failure_types=["APPLICATION_FAILURE"])
        data = fi.model_dump()
        fi2 = FailureInfo(**data)
        assert fi == fi2


class TestStepExecution:
    def test_create_minimal(self) -> None:
        step = StepExecution(
            step_id="s1", name="Compile", identifier="compile",
            status=ExecutionStatus.SUCCESS,
        )
        assert step.step_type == "Unknown"
        assert step.start_ts is None

    def test_create_full(self) -> None:
        step = StepExecution(
            step_id="s1", name="Deploy", identifier="deploy",
            status=ExecutionStatus.RUNNING, step_type="K8sRollingDeploy",
            start_ts=1000, end_ts=2000,
            failure_info=None, delegate_info="del-1", outcomes={"key": "val"},
        )
        assert step.delegate_info == "del-1"

    def test_serialization_round_trip(self) -> None:
        step = StepExecution(
            step_id="s1", name="Run", identifier="run",
            status=ExecutionStatus.FAILED,
            failure_info=FailureInfo(message="boom"),
        )
        data = step.model_dump()
        step2 = StepExecution(**data)
        assert step == step2


class TestStageExecution:
    def test_create_empty_steps(self) -> None:
        stage = StageExecution(
            stage_id="st1", name="Build", identifier="Build",
            status=ExecutionStatus.SUCCESS,
        )
        assert stage.steps == []
        assert stage.stage_type == "Unknown"

    def test_with_steps(self) -> None:
        step = StepExecution(
            step_id="s1", name="Compile", identifier="compile",
            status=ExecutionStatus.SUCCESS,
        )
        stage = StageExecution(
            stage_id="st1", name="Build", identifier="Build",
            status=ExecutionStatus.SUCCESS, steps=[step],
        )
        assert len(stage.steps) == 1


class TestPipelineExecution:
    def test_create_minimal(self) -> None:
        ex = PipelineExecution(
            plan_execution_id="ex1",
            pipeline_identifier="pipe1",
            pipeline_name="My Pipeline",
            status=ExecutionStatus.SUCCESS,
        )
        assert ex.stages == []
        assert ex.tags == {}
        assert ex.trigger_type == "MANUAL"

    def test_create_full(self) -> None:
        ex = PipelineExecution(
            plan_execution_id="ex1",
            pipeline_identifier="pipe1",
            pipeline_name="My Pipeline",
            status=ExecutionStatus.FAILED,
            start_ts=1000, end_ts=2000,
            trigger_type="WEBHOOK",
            triggered_by="github",
            branch="main",
            run_sequence=10,
            execution_url="https://app.harness.io/...",
            tags={"env": "prod"},
        )
        assert ex.branch == "main"

    def test_serialization_round_trip(self) -> None:
        ex = PipelineExecution(
            plan_execution_id="ex1",
            pipeline_identifier="pipe1",
            pipeline_name="P",
            status=ExecutionStatus.RUNNING,
        )
        data = ex.model_dump()
        ex2 = PipelineExecution(**data)
        assert ex == ex2


class TestPipelineSummary:
    def test_defaults(self) -> None:
        ps = PipelineSummary(identifier="p1", name="Pipeline")
        assert ps.store_type == "INLINE"
        assert ps.modules == []

    def test_full(self) -> None:
        ps = PipelineSummary(
            identifier="p1", name="Pipeline", description="desc",
            modules=["CI", "CD"], last_execution_status=ExecutionStatus.SUCCESS,
            last_execution_ts=1000, store_type="REMOTE", repo_name="repo",
            created=100, updated=200,
        )
        assert ps.repo_name == "repo"


class TestInputSet:
    def test_create(self) -> None:
        inp = InputSet(
            identifier="is1", name="Default", pipeline_identifier="pipe1",
            input_set_yaml="yaml_content",
        )
        assert inp.created is None


class TestGraphNode:
    def test_create(self) -> None:
        node = GraphNode(
            uuid="n1", name="Compile", identifier="compile",
            status=ExecutionStatus.SUCCESS,
        )
        assert node.step_type == "Unknown"
        assert node.base_fqn is None


class TestExecutionGraph:
    def test_create_empty(self) -> None:
        eg = ExecutionGraph(root_node_id="root")
        assert eg.nodes == {}

    def test_create_with_nodes(self) -> None:
        node = GraphNode(
            uuid="n1", name="N", identifier="n",
            status=ExecutionStatus.SUCCESS,
        )
        eg = ExecutionGraph(root_node_id="n1", nodes={"n1": node})
        assert "n1" in eg.nodes


class TestHarnessProfile:
    def test_create(self) -> None:
        p = HarnessProfile(
            name="prod", account_id="acc1",
            org_identifier="org1", project_identifier="proj1",
        )
        assert p.base_url == "https://app.harness.io"
        assert p.is_active is False

    def test_custom_base_url(self) -> None:
        p = HarnessProfile(
            name="onprem", account_id="acc1",
            org_identifier="org1", project_identifier="proj1",
            base_url="https://harness.company.com",
        )
        assert p.base_url == "https://harness.company.com"
